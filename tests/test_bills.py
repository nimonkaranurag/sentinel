from datetime import date

import pytest

from sentinel import bills, db

BILL = ("grace_days: 3\nbills:\n"
        "  - name: Broadband\n    pattern: 'VIRGIN MEDIA'\n    due_day: 15\n"
        "    expected_cents: 8000\n    tolerance_pct: 10\n")


def _conn_with(tmp_path, rows):
    conn = db.connect(tmp_path / "ledger.db")
    db.init_db(conn)
    if rows:
        db.insert_transactions(conn, rows)
    conn.commit()
    return conn


def _bills_file(tmp_path, text=BILL):
    p = tmp_path / "bills.yaml"
    p.write_text(text)
    return p


def _paid(day="2026-07-15", cents=-8000):
    return [{"account_id": "a", "booking_date": day, "amount_cents": cents,
             "merchant_raw": "VIRGIN MEDIA IRELAND", "source": "api"}]


def test_schema_missing_key_crashes(tmp_path):
    p = _bills_file(tmp_path, "bills:\n  - name: X\n    pattern: 'X'\n")
    with pytest.raises(ValueError, match="missing"):
        bills.load_bills(p)


EOM_BILL = ("grace_days: 3\nbills:\n"
            "  - name: Rent\n    pattern: 'LANDLORD'\n    due_day: 28\n"
            "    expected_cents: 120000\n    tolerance_pct: 5\n")


def test_end_of_month_bill_is_late_across_the_month_boundary(tmp_path):
    """due_day 28, nothing paid, now 4 Mar → 5 days past due. The old
    `as_of.day > 28 + 3` test needed day > 31 and could NEVER fire."""
    conn = _conn_with(tmp_path, [])  # nothing paid
    p = _bills_file(tmp_path, EOM_BILL)
    # Feb 28 + 3 grace = Mar 3; as_of within grace (Mar 2) is silent…
    assert bills.check(conn, {}, date(2026, 3, 2), path=p) == []
    late = bills.check(conn, {}, date(2026, 3, 4), path=p)  # …but Mar 4 is late
    assert len(late) == 1 and late[0]["kind"] == "late"
    assert "days past due (2026-02-28)" in late[0]["text"]
    conn.close()


def test_end_of_month_bill_paid_last_month_is_silent(tmp_path):
    conn = _conn_with(tmp_path, [{"account_id": "a", "booking_date": "2026-02-28",
                                  "amount_cents": -120000, "merchant_raw": "LANDLORD SO",
                                  "source": "api"}])
    assert bills.check(conn, {}, date(2026, 3, 4), path=_bills_file(tmp_path, EOM_BILL)) == []
    conn.close()


@pytest.mark.parametrize("bad_field", [
    "due_day: 32\n    expected_cents: 8000\n    tolerance_pct: 10",
    "due_day: 15\n    expected_cents: -100\n    tolerance_pct: 10",
    "due_day: 15\n    expected_cents: 8000\n    tolerance_pct: -5",
    "due_day: 15\n    expected_cents: 8000\n    tolerance_pct: 200",
])
def test_out_of_range_values_crash(tmp_path, bad_field):
    p = _bills_file(tmp_path, f"bills:\n  - name: X\n    pattern: 'X'\n    {bad_field}\n")
    with pytest.raises(ValueError):
        bills.load_bills(p)


def test_local_grace_days_wins_over_shared(tmp_path):
    """locals-win is the contract; the one scalar in the file must not invert it."""
    (tmp_path / "bills.yaml").write_text("grace_days: 3\nbills: []\n")
    (tmp_path / "bills.local.yaml").write_text("grace_days: 10\nbills: []\n")
    _, grace = bills.load_bills(tmp_path / "bills.yaml")
    assert grace == 10


def test_paid_within_tolerance_is_silent(tmp_path):
    conn = _conn_with(tmp_path, _paid())
    assert bills.check(conn, {}, date(2026, 7, 20), path=_bills_file(tmp_path)) == []
    conn.close()


def test_drift_alerts_on_price_hike(tmp_path):
    conn = _conn_with(tmp_path, _paid(cents=-9500))  # +18.75% > 10% tolerance
    alerts = bills.check(conn, {}, date(2026, 7, 20), path=_bills_file(tmp_path))
    assert len(alerts) == 1 and alerts[0]["kind"] == "drift"
    conn.close()


def test_late_alerts_only_past_due_plus_grace(tmp_path):
    conn = _conn_with(tmp_path, [])  # nothing paid
    assert bills.check(conn, {}, date(2026, 7, 16), path=_bills_file(tmp_path)) == []  # within grace
    late = bills.check(conn, {}, date(2026, 7, 20), path=_bills_file(tmp_path))        # day 20 > 15+3
    assert len(late) == 1 and late[0]["kind"] == "late"
    conn.close()


def test_checklist_renders_paid(tmp_path):
    conn = _conn_with(tmp_path, _paid())
    text = bills.render_checklist(conn, {}, date(2026, 7, 20), path=_bills_file(tmp_path))
    assert "✅ Broadband €80.00" in text
    conn.close()


def test_db_backup_roundtrip(tmp_path):
    conn = _conn_with(tmp_path, [{"account_id": "a", "booking_date": "2026-07-01",
                                  "amount_cents": -100, "merchant_raw": "X", "source": "api"}])
    dest = tmp_path / "backup.db"
    db.backup(conn, dest)
    conn.close()
    restored = db.connect(dest)
    assert restored.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
    restored.close()
