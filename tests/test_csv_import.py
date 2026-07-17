from pathlib import Path

import pytest

from sentinel import csv_import, db
from sentinel.csv_import import parse_aib_date

FIXTURES = Path(__file__).parent / "fixtures"
MESSY_CSV = FIXTURES / "aib_real_format_messy.csv"


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
    assert inserted == submitted == 2          # VDP-GOOGLE credit + VDP-KLINGAI debit
    assert skipped == 2                         # €0 FX annotation + Pending row
    got = {r["merchant_raw"]: r["amount_cents"] for r in
           conn.execute("SELECT merchant_raw, amount_cents FROM transactions")}
    assert got == {"VDP-GOOGLE*GOOGLE": 93, "VDP-KLINGAI.COM": -799}
    conn.close()


def test_clip_before_prevents_cross_source_double_count(tmp_path):
    """API-first, then a backfill overlapping the API window: rows on/after the
    earliest API date are clipped, so the overlap can't be double-counted even
    when the two sources derive different merchant_raw."""
    conn = db.connect(tmp_path / "ledger.db")
    db.init_db(conn)
    # An API row on 2026-07-13 (different description than the CSV's for that day).
    db.insert_transactions(conn, [{"account_id": "93XXXX-99999999", "booking_date": "2026-07-13",
                                   "amount_cents": -799, "merchant_raw": "KLINGAI LTD",
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
