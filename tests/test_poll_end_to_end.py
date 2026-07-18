"""
End-to-end orchestration: one poll from cron entry to the complete Telegram
payload.
"""

from datetime import date

import pytest

from sentinel import db, ingest, notify, state_keys, telegram

POLICIES = "policies:\n  - name: food-delivery\n    bucket: FoodDelivery\n    cap_monthly_cents: 15000\n"
BILLS = (
    "grace_days: 3\nbills:\n"
    "  - name: Rent\n    pattern: 'LANDLORD'\n    due_day: 15\n"
    "    expected_cents: 120000\n    tolerance_pct: 5\n"
)
AS_OF = date(2026, 7, 20)  # Rent due 2026-07-15 + 3 grace → 5 days late


class FakeTG:
    def __init__(self):
        self.sent = []

    def __call__(self, token, method, payload):
        if method == "sendMessage":
            self.sent.append(payload["text"])
            return {"ok": True, "result": {"message_id": len(self.sent)}}
        if method in ("editMessageText", "answerCallbackQuery"):
            return {"ok": True, "result": {}}
        raise AssertionError(f"unexpected telegram method {method}")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "ledger.db")
    db.init_db(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "777")
    fake = FakeTG()
    monkeypatch.setattr(telegram, "_post_telegram", fake)
    # Make run_poll take its ingest path, and have the "bank" book one over-cap
    # FoodDelivery charge on this poll — the same shape as a real arriving charge.
    key = tmp_path / "key.pem"
    key.write_text("x")
    monkeypatch.setenv("ENABLE_BANKING_APP_ID", "app")
    monkeypatch.setenv("ENABLE_BANKING_PRIVATE_KEY_PATH", str(key))
    db.set_state(conn, state_keys.EB_ACCOUNT_UIDS, '["a"]')
    conn.commit()

    def fake_run_ingest(conn_, client, uids, **kw):
        conn_.execute(
            "INSERT OR IGNORE INTO merchants (name_normalized, category, categorized_by) "
            "VALUES ('DELIVEROO', 'FoodDelivery', 'dict')"
        )
        mid = conn_.execute("SELECT id FROM merchants WHERE name_normalized='DELIVEROO'").fetchone()[0]
        db.insert_transactions(
            conn_,
            [
                {
                    "account_id": "a",
                    "booking_date": AS_OF.isoformat(),
                    "amount_cents": -20000,
                    "merchant_raw": "DELIVEROO",
                    "merchant_id": mid,
                    "source": "api",
                }
            ],
        )
        return 1, 1

    monkeypatch.setattr(ingest, "run_ingest", fake_run_ingest)
    monkeypatch.setattr(ingest, "build_client", lambda *a, **k: object())
    monkeypatch.setattr(ingest, "check_and_consume_allowance", lambda *a, **k: True)
    pol = tmp_path / "policies.yaml"
    pol.write_text(POLICIES)
    bills_file = tmp_path / "bills.yaml"
    bills_file.write_text(BILLS)
    cfg = {
        "db_path": str(tmp_path / "ledger.db"),
        "policies": {"path": str(pol)},
        "bills": {"path": str(bills_file)},
        "categorize": {"merchant_map_path": str(tmp_path / "merchant_map.json"), "rules_path": None},
        "enable_banking": {"api_daily_call_limit": 4, "first_pull_days": 90},
    }
    return conn, cfg, fake


def test_poll_sends_exactly_one_policy_and_one_bill_alert(env):
    conn, cfg, fake = env
    fired = notify.run_poll(conn, cfg, as_of=AS_OF)
    assert fired == 2, fake.sent
    assert len(fake.sent) == 2, "exactly a policy alert and a bill alert — nothing else"
    policy = next(m for m in fake.sent if "cap" in m)
    late = next(m for m in fake.sent if "unpaid" in m)
    assert "DELIVEROO" in policy and "food-delivery" in policy
    assert "Rent" in late and "days past due" in late


def test_second_poll_is_silent(env):
    conn, cfg, fake = env
    notify.run_poll(conn, cfg, as_of=AS_OF)
    assert len(fake.sent) == 2
    fired = notify.run_poll(conn, cfg, as_of=AS_OF)  # nothing new booked, bill still late
    assert fired == 0
    assert len(fake.sent) == 2, "no policy re-alert (watermark) and no bill re-alert (per-cycle key)"
