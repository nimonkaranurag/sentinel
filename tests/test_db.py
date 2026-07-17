import hashlib
import sqlite3

import pytest

from sentinel import db


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "ledger.db")
    db.init_db(connection)
    yield connection
    connection.close()


# ── to_cents: money is integer cents, no floats, ever ────────────────────


@pytest.mark.parametrize(
    ("text", "cents"),
    [
        ("12.34", 1234),
        ("-12.34", -1234),
        ("12.3", 1230),
        ("5", 500),
        ("+3.50", 350),
        ("0.05", 5),
        ("1,368.00", 136800),
        ("1,000,000.99", 100000099),
        (" 45.67 ", 4567),
        ("€120.00", 12000),
    ],
)
def test_to_cents_valid(text, cents):
    assert db.to_cents(text) == cents


@pytest.mark.parametrize("bad", [12.34, 12, True, None])
def test_to_cents_rejects_non_strings(bad):
    with pytest.raises(TypeError):
        db.to_cents(bad)


@pytest.mark.parametrize("bad", ["", "  ", "abc", "12,34", "12.345", "1,23.45", "12.34.56", "--5"])
def test_to_cents_rejects_garbage(bad):
    with pytest.raises(ValueError):
        db.to_cents(bad)


# ── Hash-fallback id (SPEC §2) ────────────────────────────────────────────


def test_hash_id_matches_spec_formula():
    expected = hashlib.sha256(b"2026-01-28|-2345|LIDL DUBLIN|acc-uid-1").hexdigest()
    assert db.txn_hash_id("2026-01-28", -2345, "LIDL DUBLIN", "acc-uid-1") == expected


def test_hash_id_occurrence_disambiguates():
    base = db.txn_hash_id("2026-01-03", -350, "COFFEE ANGEL", "a1")
    second = db.txn_hash_id("2026-01-03", -350, "COFFEE ANGEL", "a1", occurrence=1)
    assert base != second


# ── Schema / migrations ───────────────────────────────────────────────────


def test_init_db_idempotent(conn):
    assert db.init_db(conn) == db.schema_version(conn)
    assert db.init_db(conn) == db.schema_version(conn)  # second run: no-op, no error


def test_strict_schema_rejects_float_amounts(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO transactions (id, account_id, booking_date, amount_cents, source, inserted_at) "
            "VALUES ('x', 'a1', '2026-01-01', 12.5, 'csv', '2026-01-01T00:00:00')"
        )


def test_stored_amounts_are_integers(conn):
    db.insert_transactions(
        conn,
        [{"account_id": "a1", "booking_date": "2026-01-01", "amount_cents": -1234, "source": "csv"}],
    )
    assert conn.execute("SELECT typeof(amount_cents) FROM transactions").fetchone()[0] == "integer"


# ── prepare_transactions validation ───────────────────────────────────────


def test_prepare_rejects_float_amounts():
    with pytest.raises(TypeError):
        db.prepare_transactions(
            [{"account_id": "a1", "booking_date": "2026-01-01", "amount_cents": 12.5, "source": "csv"}]
        )


def test_prepare_rejects_bad_source_and_dates():
    with pytest.raises(ValueError):
        db.prepare_transactions(
            [{"account_id": "a1", "booking_date": "2026-01-01", "amount_cents": 1, "source": "manual"}]
        )
    with pytest.raises(ValueError):
        db.prepare_transactions(
            [{"account_id": "a1", "booking_date": "01/02/2026", "amount_cents": 1, "source": "csv"}]
        )


def test_state_roundtrip(conn):
    assert db.get_state(conn, "missing", "fallback") == "fallback"
    db.set_state(conn, "cursor:a1", "2026-01-31")
    db.set_state(conn, "cursor:a1", "2026-02-28")  # upsert
    assert db.get_state(conn, "cursor:a1") == "2026-02-28"
