from pathlib import Path

import pytest

from sentinel import csv_import, db, state_keys
from sentinel.csv_import import parse_aib_date

FIXTURES = Path(__file__).parent / "fixtures"
MESSY_CSV = FIXTURES / "aib_real_format_messy.csv"

_HEADER = ("Posted Account, Posted Transactions Date, Description, Debit Amount, "
           "Credit Amount, Posted Currency,Transaction Type\n")


@pytest.mark.parametrize(
    ("raw", "iso"),
    [
        ("13/07/26", "2026-07-13"),   # AIB real export: DD/MM/YY
        ("01/12/25", "2025-12-01"),
        ("13/07/2026", "2026-07-13"),  # DD/MM/YYYY still supported
        ("2026-07-13", "2026-07-13"),  # ISO passthrough
        (" 13/07/26 ", "2026-07-13"),  # surrounding whitespace tolerated
    ],
)
def test_parse_aib_date(raw, iso):
    assert parse_aib_date(raw) == iso


@pytest.mark.parametrize("bad", ["", "not-a-date", "13-07-26", "2026/07/13", "13/07/"])
def test_parse_aib_date_rejects_garbage(bad):
    with pytest.raises(ValueError):
        parse_aib_date(bad)


def test_real_format_quarantines_and_skips_per_row(tmp_path):
    """Real 7-col export (single Description, no Posted Currency): a bad date is
    quarantined (not a whole-file abort), a €0 FX line and a Pending row are
    skipped, and the good rows still land."""
    conn = db.connect(tmp_path / "ledger.db")
    db.init_db(conn)
    inserted, submitted, skipped = csv_import.import_file(conn, MESSY_CSV, {})
    conn.commit()
    assert inserted == submitted == 2          # NIMBUSPAY credit + WIDGETWORKS debit
    assert skipped == 2                         # €0 FX annotation + Pending row
    got = {r["merchant_raw"]: r["amount_cents"] for r in
           conn.execute("SELECT merchant_raw, amount_cents FROM transactions")}
    assert got == {"VDP-NIMBUSPAY*NIMBUSPAY": 93, "VDP-WIDGETWORKS.COM": -799}
    conn.close()


def test_cp1252_accented_merchant_imports_and_keeps_valid_rows(tmp_path):
    """A genuinely cp1252 export (an accented merchant as a lone 0xC9) must import,
    valid rows and all — the old open()-wrapped fallback never caught the decode,
    which happens lazily during iteration, so one 'CAFÉ' aborted the whole file."""
    p = tmp_path / "cp1252.csv"
    body = (_HEADER
            + "SYNTH01,01/07/26,PLAIN SHOP,10.00,,EUR,Debit\n"
            + "SYNTH01,02/07/26,CAF\xc9 CENTRAL,4.20,,EUR,Debit\n")  # É = 0xC9 in cp1252
    p.write_bytes(body.encode("cp1252"))
    conn = db.connect(tmp_path / "ledger.db")
    db.init_db(conn)
    inserted, submitted, _ = csv_import.import_file(conn, p, {})
    conn.commit()
    names = {r["merchant_raw"] for r in conn.execute("SELECT merchant_raw FROM transactions")}
    assert inserted == 2, "both rows must land, including the one after the accented byte"
    assert names == {"PLAIN SHOP", "CAFÉ CENTRAL"}
    conn.close()


def test_clip_boundary_survives_a_backdated_api_row(tmp_path):
    """A backdated reversal must not drag the clip boundary back and amputate the
    backfill: the boundary is anchored to the recorded coverage start, not MIN()."""
    conn = db.connect(tmp_path / "ledger.db")
    db.init_db(conn)
    db.set_state(conn, state_keys.API_COVERAGE_START, "2026-04-15")
    db.insert_transactions(conn, [
        {"account_id": "a", "booking_date": "2026-04-20", "amount_cents": -100,
         "merchant_raw": "NORMAL", "source": "api"},
        {"account_id": "a", "booking_date": "2025-11-02", "amount_cents": 50,
         "merchant_raw": "BACKDATED REVERSAL", "source": "api"},  # months before the window
    ])
    conn.commit()
    # MIN(booking_date) is the Nov outlier; the clip falls back to the coverage start.
    assert csv_import.compute_clip_before(conn) == "2026-04-15"
    conn.close()


def test_non_eur_csv_row_is_quarantined_and_recorded_once(tmp_path):
    """A non-EUR row is retained in the quarantine table (not vaporized), and a
    re-import quarantines it once — the fingerprint dedupes re-fetches."""
    p = tmp_path / "fx.csv"
    p.write_text(_HEADER
                 + "SYNTH01,01/07/26,USD SHOP,10.00,,USD,Debit\n"
                 + "SYNTH01,02/07/26,EUR SHOP,5.00,,EUR,Debit\n")
    conn = db.connect(tmp_path / "ledger.db")
    db.init_db(conn)
    inserted, _, _ = csv_import.import_file(conn, p, {})
    conn.commit()
    assert inserted == 1  # only the EUR row books
    assert db.quarantine_count(conn) == 1  # the USD row is retained, not dropped
    q = conn.execute("SELECT source, reason FROM quarantine").fetchone()
    assert q["source"] == "csv" and "USD" in q["reason"]
    csv_import.import_file(conn, p, {})  # re-import the same file
    conn.commit()
    assert db.quarantine_count(conn) == 1, "same row must quarantine once, not once per import"
    conn.close()


def test_clip_before_prevents_cross_source_double_count(tmp_path):
    """API-first, then a backfill overlapping the API window: rows on/after the
    earliest API date are clipped, so the overlap can't be double-counted even
    when the two sources derive different merchant_raw."""
    conn = db.connect(tmp_path / "ledger.db")
    db.init_db(conn)
    # An API row on 2026-07-13 (different description than the CSV's for that day).
    db.insert_transactions(conn, [{"account_id": "SYNTH01-00000000", "booking_date": "2026-07-13",
                                   "amount_cents": -799, "merchant_raw": "WIDGETWORKS LTD",
                                   "source": "api"}])
    conn.commit()
    clip = csv_import.compute_clip_before(conn)
    assert clip == "2026-07-13"
    inserted, submitted, _ = csv_import.import_file(conn, MESSY_CSV, {}, clip_before=clip)
    conn.commit()
    # Both CSV rows are dated 2026-07-13 >= clip → clipped; nothing new imported.
    assert inserted == 0 and submitted == 0
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
    conn.close()
