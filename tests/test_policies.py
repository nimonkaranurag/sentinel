from datetime import date

import pytest

from sentinel import db, policies


def _ledger(tmp_path, category, name, day_amounts):
    conn = db.connect(tmp_path / "ledger.db")
    db.init_db(conn)
    cur = conn.execute(
        "INSERT INTO merchants (name_normalized, category, categorized_by) VALUES (?, ?, 'dict')",
        (name, category))
    mid = cur.lastrowid
    rows = [{"account_id": "a", "booking_date": d, "amount_cents": c,
             "merchant_raw": name, "merchant_id": mid, "source": "api"}
            for d, c in day_amounts]
    db.insert_transactions(conn, rows)
    conn.commit()
    ids = [r[0] for r in conn.execute("SELECT id FROM transactions ORDER BY booking_date")]
    return conn, ids


def _policies_file(tmp_path, text):
    p = tmp_path / "policies.yaml"
    p.write_text(text)
    return p


FOOD = "policies:\n  - name: food-delivery\n    bucket: FoodDelivery\n    cap_monthly_cents: 15000\n"


# ── schema (a typo is a crash, not a silent default) ──────────────────────


def test_missing_matcher_is_a_crash(tmp_path):
    p = _policies_file(tmp_path, "policies:\n  - name: x\n    cap_monthly_cents: 100\n")
    with pytest.raises(ValueError, match="matcher"):
        policies.load_policies(p)


def test_unknown_key_is_a_crash(tmp_path):
    p = _policies_file(tmp_path,
        "policies:\n  - name: x\n    cap_monthly_cents: 100\n    bucket: Other\n    typo: 1\n")
    with pytest.raises(ValueError, match="unknown key"):
        policies.load_policies(p)


def test_bad_bucket_is_a_crash(tmp_path):
    p = _policies_file(tmp_path,
        "policies:\n  - name: x\n    cap_monthly_cents: 100\n    bucket: Nonsense\n")
    with pytest.raises(ValueError, match="bucket"):
        policies.load_policies(p)


# ── evaluation ────────────────────────────────────────────────────────────


def test_alert_fires_only_once_cap_is_crossed(tmp_path):
    # €60 + €60 + €60 FoodDelivery vs a €150 cap → only the 3rd (€180 MTD) alerts.
    conn, ids = _ledger(tmp_path, "FoodDelivery", "DELIVEROO",
                        [("2026-07-01", -6000), ("2026-07-02", -6000), ("2026-07-03", -6000)])
    alerts = policies.evaluate(conn, {}, date(2026, 7, 3), ids, path=_policies_file(tmp_path, FOOD))
    assert len(alerts) == 1
    a = alerts[0]
    assert a["txn_id"] == ids[2] and a["policy"] == "food-delivery"
    assert "3rd this month" in a["text"]
    assert "€180.00 of your €150.00" in a["text"]
    assert "€2,160.00/yr" in a["text"]  # 180 × 12
    conn.close()


def test_only_new_txns_alert(tmp_path):
    conn, ids = _ledger(tmp_path, "FoodDelivery", "DELIVEROO", [("2026-07-01", -20000)])
    pol = _policies_file(tmp_path, FOOD)
    assert policies.evaluate(conn, {}, date(2026, 7, 1), [], path=pol) == []       # nothing new
    assert len(policies.evaluate(conn, {}, date(2026, 7, 1), ids, path=pol)) == 1  # this txn is new
    conn.close()


def test_under_cap_is_silent(tmp_path):
    conn, ids = _ledger(tmp_path, "FoodDelivery", "DELIVEROO", [("2026-07-01", -5000)])
    assert policies.evaluate(conn, {}, date(2026, 7, 1), ids, path=_policies_file(tmp_path, FOOD)) == []
    conn.close()


def test_bundled_policies_yaml_loads_and_is_valid():
    pols = policies.load_policies()
    assert {p["name"] for p in pols} >= {"food-delivery"}
