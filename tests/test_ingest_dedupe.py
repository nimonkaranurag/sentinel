from pathlib import Path

import pytest
import yaml

from sentinel import csv_import, db, ingest

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_CSV = FIXTURES / "aib_backfill_sample.csv"

CSV_ACCOUNT_LABEL = "93XXXX-11111111"
API_ACCOUNT_UID = "acc-uid-1"
ACCOUNT_MAP = {CSV_ACCOUNT_LABEL: API_ACCOUNT_UID}

# Known totals of the synthetic January 2026 statement (fixture CSV):
# debits 45.67+3.50+3.50+100.00+120.00+23.45, credits 3300.00+1000.00.
JAN_2026_SUM_CENTS = 400_388
JAN_2026_ROW_COUNT = 8

# Synthetic Enable Banking payload. One transaction (LIDL, no entry_reference)
# is byte-identical to a fixture CSV row → must dedupe across sources.
EB_PAYLOAD = [
    {
        "entry_reference": "EB-REF-001",
        "booking_date": "2026-02-02",
        "value_date": "2026-02-03",
        "transaction_amount": {"amount": "12.30", "currency": "EUR"},
        "credit_debit_indicator": "DBIT",
        "status": "BOOK",
        "creditor": {"name": "TESCO STORES 4368 DUBLIN"},
        "remittance_information": ["VDP-TESCO STORES 4368"],
    },
    {
        "entry_reference": "EB-REF-002",
        "booking_date": "2026-02-09",
        "transaction_amount": {"amount": "3300.00", "currency": "EUR"},
        "credit_debit_indicator": "CRDT",
        "status": "BOOK",
        "debtor": {"name": "ACME LTD"},
        "remittance_information": ["SALARY"],
    },
    {
        # no entry_reference → hash-fallback id; overlaps the CSV backfill
        "booking_date": "2026-01-28",
        "transaction_amount": {"amount": "23.45", "currency": "EUR"},
        "credit_debit_indicator": "DBIT",
        "status": "BOOK",
        "creditor": {"name": "LIDL DUBLIN"},
    },
    {
        "entry_reference": "EB-REF-PENDING",
        "booking_date": "2026-02-10",
        "transaction_amount": {"amount": "9.99", "currency": "EUR"},
        "credit_debit_indicator": "DBIT",
        "status": "PDNG",  # pending → must be skipped
        "creditor": {"name": "SOMEWHERE"},
    },
]
EB_BOOKED_COUNT = 3  # payload minus the pending row


class FakeClient:
    """Stands in for EnableBankingClient; replays the same payload every call,
    like a bank re-serving an overlapping date window."""

    def __init__(self, payload=EB_PAYLOAD):
        self.payload = payload
        self.calls = []

    def iter_transactions(self, account_uid, date_from):
        self.calls.append((account_uid, date_from))
        yield from self.payload


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "ledger.db")
    db.init_db(connection)
    yield connection
    connection.close()


def _row_count(conn):
    return conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]


def _total_sum(conn):
    return conn.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM transactions").fetchone()[0]


# ── The gate itself ───────────────────────────────────────────────────────


def test_gate_ingest_twice_changes_zero_rows(conn):
    client = FakeClient()
    inserted_1, submitted_1 = ingest.run_ingest(
        conn, client, [API_ACCOUNT_UID], default_from="2026-01-01"
    )
    assert inserted_1 == submitted_1 == EB_BOOKED_COUNT
    count_after_first, sum_after_first = _row_count(conn), _total_sum(conn)

    inserted_2, submitted_2 = ingest.run_ingest(
        conn, client, [API_ACCOUNT_UID], default_from="2026-01-01"
    )
    assert inserted_2 == 0, "second ingest must change zero rows"
    assert submitted_2 == EB_BOOKED_COUNT
    assert _row_count(conn) == count_after_first
    assert _total_sum(conn) == sum_after_first
    # cursor advanced to the max booked date and was reused on run 2
    assert db.get_state(conn, f"cursor:{API_ACCOUNT_UID}") == "2026-02-09"
    assert client.calls[1][1] == "2026-02-09"


def test_gate_sample_month_sum_matches_known_figure(conn):
    csv_import.import_file(conn, SAMPLE_CSV, ACCOUNT_MAP)
    conn.commit()
    total = conn.execute(
        "SELECT SUM(amount_cents) FROM transactions WHERE booking_date LIKE '2026-01-%'"
    ).fetchone()[0]
    assert total == JAN_2026_SUM_CENTS
    assert _row_count(conn) == JAN_2026_ROW_COUNT


# ── Supporting invariants behind the gate ─────────────────────────────────


def test_csv_reimport_is_idempotent(conn):
    inserted_1, submitted_1, _ = csv_import.import_file(conn, SAMPLE_CSV, ACCOUNT_MAP)
    conn.commit()
    assert inserted_1 == submitted_1 == JAN_2026_ROW_COUNT

    inserted_2, submitted_2, _ = csv_import.import_file(conn, SAMPLE_CSV, ACCOUNT_MAP)
    conn.commit()
    assert inserted_2 == 0, "re-importing the same statement must add nothing"
    assert submitted_2 == JAN_2026_ROW_COUNT
    assert _row_count(conn) == JAN_2026_ROW_COUNT


def test_identical_same_day_rows_both_kept(conn):
    """Two genuinely identical coffees must both survive (occurrence ids),
    otherwise the month sum silently loses money."""
    csv_import.import_file(conn, SAMPLE_CSV, ACCOUNT_MAP)
    conn.commit()
    count, total = conn.execute(
        "SELECT COUNT(*), SUM(amount_cents) FROM transactions WHERE merchant_raw = 'COFFEE ANGEL'"
    ).fetchone()
    assert count == 2
    assert total == -700


def test_api_csv_cross_source_dedupe(conn):
    """CSV backfill overlapping an API pull: the shared row lands once (SPEC §2)."""
    csv_import.import_file(conn, SAMPLE_CSV, ACCOUNT_MAP)
    conn.commit()
    inserted, submitted = ingest.run_ingest(
        conn, FakeClient(), [API_ACCOUNT_UID], default_from="2026-01-01"
    )
    assert submitted == EB_BOOKED_COUNT
    assert inserted == EB_BOOKED_COUNT - 1, "the overlapping LIDL row must be deduped"
    lidl = conn.execute(
        "SELECT COUNT(*), MIN(source) FROM transactions WHERE merchant_raw = 'LIDL DUBLIN'"
    ).fetchone()
    assert tuple(lidl) == (1, "csv")
    assert _row_count(conn) == JAN_2026_ROW_COUNT + EB_BOOKED_COUNT - 1


def test_pending_api_rows_are_skipped():
    assert ingest.map_api_transaction(EB_PAYLOAD[3], API_ACCOUNT_UID) is None


def test_api_mapping_signs_and_fields():
    debit = ingest.map_api_transaction(EB_PAYLOAD[0], API_ACCOUNT_UID)
    assert debit["amount_cents"] == -1230
    assert debit["merchant_raw"] == "TESCO STORES 4368 DUBLIN"
    assert debit["id"] == "EB-REF-001"
    assert debit["source"] == "api"
    credit = ingest.map_api_transaction(EB_PAYLOAD[1], API_ACCOUNT_UID)
    assert credit["amount_cents"] == 330_000
    assert credit["merchant_raw"] == "ACME LTD"


def test_csv_import_cli_dry_run_writes_nothing(tmp_path):
    db_path = tmp_path / "ledger.db"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({"db_path": str(db_path), "csv_import": {"account_map": {}}}),
        encoding="utf-8",
    )
    assert csv_import.main([str(SAMPLE_CSV), "--config", str(config_path), "--dry-run"]) == 0
    conn = db.connect(db_path)
    assert _row_count(conn) == 0, "--dry-run must not persist rows"
    conn.close()

    assert csv_import.main([str(SAMPLE_CSV), "--config", str(config_path)]) == 0
    conn = db.connect(db_path)
    assert _row_count(conn) == JAN_2026_ROW_COUNT
    conn.close()
