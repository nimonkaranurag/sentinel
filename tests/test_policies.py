from datetime import date

import pytest

from sentinel import db, policies


def _ledger(tmp_path, category, name, day_amounts):
    conn = db.connect(tmp_path / "ledger.db")
    db.init_db(conn)
    cur = conn.execute(
        "INSERT INTO merchants (name_normalized, category, categorized_by) VALUES (?, ?, 'dict')", (name, category)
    )
    mid = cur.lastrowid
    rows = [
        {
            "account_id": "a",
            "booking_date": d,
            "amount_cents": c,
            "merchant_raw": name,
            "merchant_id": mid,
            "source": "api",
        }
        for d, c in day_amounts
    ]
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
    p = _policies_file(tmp_path, "policies:\n  - name: x\n    cap_monthly_cents: 100\n    bucket: Other\n    typo: 1\n")
    with pytest.raises(ValueError, match="unknown key"):
        policies.load_policies(p)


def test_bad_bucket_is_a_crash(tmp_path):
    p = _policies_file(tmp_path, "policies:\n  - name: x\n    cap_monthly_cents: 100\n    bucket: Nonsense\n")
    with pytest.raises(ValueError, match="bucket"):
        policies.load_policies(p)


# ── evaluation ────────────────────────────────────────────────────────────


def test_alert_fires_only_once_cap_is_crossed(tmp_path):
    # €60 + €60 + €60 FoodDelivery vs a €150 cap → only the 3rd (€180 MTD) alerts.
    conn, ids = _ledger(
        tmp_path, "FoodDelivery", "DELIVEROO", [("2026-07-01", -6000), ("2026-07-02", -6000), ("2026-07-03", -6000)]
    )
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
    assert policies.evaluate(conn, {}, date(2026, 7, 1), [], path=pol) == []  # nothing new
    assert len(policies.evaluate(conn, {}, date(2026, 7, 1), ids, path=pol)) == 1  # this txn is new
    conn.close()


def test_under_cap_is_silent(tmp_path):
    conn, ids = _ledger(tmp_path, "FoodDelivery", "DELIVEROO", [("2026-07-01", -5000)])
    assert policies.evaluate(conn, {}, date(2026, 7, 1), ids, path=_policies_file(tmp_path, FOOD)) == []
    conn.close()


# ── Refund netting (SPEC §4: refunds never fire false alerts) ─────────────


def test_refund_nets_against_spend_and_suppresses_false_alert(tmp_path):
    # €60 + €60, refund +€60, €60 → gross €180 but NETTED €120 ≤ €150 cap → silent.
    conn, ids = _ledger(
        tmp_path,
        "FoodDelivery",
        "DELIVEROO",
        [("2026-07-01", -6000), ("2026-07-02", -6000), ("2026-07-03", 6000), ("2026-07-04", -6000)],
    )
    assert policies.evaluate(conn, {}, date(2026, 7, 4), ids, path=_policies_file(tmp_path, FOOD)) == []
    conn.close()


def test_netted_mtd_crossing_cap_counts_charges_only(tmp_path):
    # 60 + 60, refund 60, 60, 60 → netted €180 > €150 on the 4th CHARGE (the
    # refund is neither an occurrence nor an alert subject).
    conn, ids = _ledger(
        tmp_path,
        "FoodDelivery",
        "DELIVEROO",
        [
            ("2026-07-01", -6000),
            ("2026-07-02", -6000),
            ("2026-07-03", 6000),
            ("2026-07-04", -6000),
            ("2026-07-05", -6000),
        ],
    )
    alerts = policies.evaluate(conn, {}, date(2026, 7, 5), ids, path=_policies_file(tmp_path, FOOD))
    assert len(alerts) == 1
    a = alerts[0]
    assert a["txn_id"] == ids[4]
    assert "4th this month" in a["text"]
    assert "€180.00 of your €150.00" in a["text"]
    conn.close()


def test_a_refund_is_never_an_alert_subject(tmp_path):
    # The only NEW row is a refund while gross MTD is over cap: nothing may fire.
    conn, ids = _ledger(tmp_path, "FoodDelivery", "DELIVEROO", [("2026-07-01", -20000), ("2026-07-02", 1000)])
    assert policies.evaluate(conn, {}, date(2026, 7, 2), [ids[1]], path=_policies_file(tmp_path, FOOD)) == []
    conn.close()


def test_large_unlabeled_inflow_does_not_suppress_alerts(tmp_path):
    # A €500 Uncategorized inflow (an unmapped transfer ≥ the €100 default
    # exclude threshold) must be held OUT of the netting — €160 spend still
    # breaches the €150 Other-bucket cap.
    other = "policies:\n  - name: misc\n    bucket: Other\n    cap_monthly_cents: 15000\n"
    conn, ids = _ledger(tmp_path, "Uncategorized", "MYSTERY POS", [("2026-07-01", 50000), ("2026-07-02", -16000)])
    alerts = policies.evaluate(conn, {}, date(2026, 7, 2), ids, path=_policies_file(tmp_path, other))
    assert len(alerts) == 1 and alerts[0]["policy"] == "misc"
    conn.close()


def test_small_uncategorized_inflow_still_nets(tmp_path):
    # Below the threshold it is treated as a refund, not a transfer → it nets:
    # €160 − €50 = €110 ≤ €150 → silent.
    other = "policies:\n  - name: misc\n    bucket: Other\n    cap_monthly_cents: 15000\n"
    conn, ids = _ledger(tmp_path, "Uncategorized", "MYSTERY POS", [("2026-07-01", 5000), ("2026-07-02", -16000)])
    assert policies.evaluate(conn, {}, date(2026, 7, 2), ids, path=_policies_file(tmp_path, other)) == []
    conn.close()


def test_future_dated_same_month_row_is_evaluable_when_first_seen(tmp_path):
    # A booking stamped one day ahead of as_of (bank calendar ahead of Dublin
    # near midnight). The watermark passes each row exactly once, so it must be
    # evaluable NOW — the old as_of-clamped window dropped its alert forever.
    conn, ids = _ledger(tmp_path, "FoodDelivery", "DELIVEROO", [("2026-07-31", -20000)])
    alerts = policies.evaluate(conn, {}, date(2026, 7, 30), ids, path=_policies_file(tmp_path, FOOD))
    assert len(alerts) == 1
    conn.close()


def test_bundled_policies_yaml_loads_and_is_valid():
    pols = policies.load_policies()
    assert {p["name"] for p in pols} >= {"food-delivery"}
