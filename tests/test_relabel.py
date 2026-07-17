import pytest

from sentinel import alerts, commands, db, telegram


class FakeTG:
    def __init__(self):
        self.sent, self.edits, self.answers = [], [], []
        self._mid = 100

    def __call__(self, token, method, payload):
        if method == "sendMessage":
            self._mid += 1
            self.sent.append(payload)
            return {"ok": True, "result": {"message_id": self._mid}}
        if method == "editMessageText":
            self.edits.append(payload)
            return {"ok": True, "result": {}}
        if method == "answerCallbackQuery":
            self.answers.append(payload)
            return {"ok": True, "result": {}}
        raise AssertionError(f"unexpected method {method}")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "ledger.db")
    db.init_db(conn)
    cur = conn.execute("INSERT INTO merchants (name_normalized, category, categorized_by) "
                       "VALUES ('DELIVEROO', 'FoodDelivery', 'dict')")
    db.insert_transactions(conn, [{"account_id": "a", "booking_date": "2026-07-03",
                                   "amount_cents": -2490, "merchant_raw": "DELIVEROO",
                                   "merchant_id": cur.lastrowid, "source": "api"}])
    conn.commit()
    tid = conn.execute("SELECT id FROM transactions").fetchone()[0]
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "777")
    fake = FakeTG()
    monkeypatch.setattr(telegram, "_post_telegram", fake)
    cfg = {"db_path": str(tmp_path / "ledger.db"),
           "categorize": {"merchant_map_path": str(tmp_path / "merchant_map.json"),
                          "rules_path": None}}
    return conn, cfg, fake, tid


def test_alert_carries_keyboard_and_records_event(env):
    conn, cfg, fake, tid = env
    mid = alerts.send_policy_alert(conn, {"txn_id": tid, "policy": "food-delivery",
                                          "text": "🔴 Deliveroo over cap"})
    kb = fake.sent[0]["reply_markup"]["inline_keyboard"]
    assert kb[0][0]["text"] == "✓ fine" and kb[0][1]["text"] == "Reclassify…"
    assert tuple(conn.execute("SELECT kind, txn_id, status FROM events").fetchone()) == \
        ("policy_alert", tid, "sent")
    assert mid == 101


def test_reclassify_round_trip_edits_and_teaches(env):
    conn, cfg, fake, tid = env
    mid = alerts.send_policy_alert(conn, {"txn_id": tid, "policy": "food-delivery", "text": "🔴 over cap"})
    commands.handle_callback(conn, cfg, {"id": "cb1", "data": f"rc:{tid[:12]}",
                                       "message": {"message_id": mid}})
    assert fake.edits[-1]["text"] == "Pick a category:"
    assert any(b["callback_data"].startswith("set:")
               for row in fake.edits[-1]["reply_markup"]["inline_keyboard"] for b in row)
    commands.handle_callback(conn, cfg, {"id": "cb2", "data": f"set:{tid[:12]}:Groceries",
                                       "message": {"message_id": mid}})
    assert "Got it — Groceries" in fake.edits[-1]["text"]
    assert conn.execute("SELECT category, categorized_by FROM merchants "
                        "WHERE name_normalized = 'DELIVEROO'").fetchone()[0] == "Groceries"
    assert conn.execute("SELECT status FROM events WHERE message_id = ?", (mid,)).fetchone()[0] \
        == "reclassified"


def test_duplicate_callback_is_exactly_once(env):
    conn, cfg, fake, tid = env
    mid = alerts.send_policy_alert(conn, {"txn_id": tid, "policy": "p", "text": "x"})
    cb = {"id": "dup", "data": f"ok:{tid[:12]}", "message": {"message_id": mid}}
    commands.handle_callback(conn, cfg, cb)
    commands.handle_callback(conn, cfg, cb)  # retried tap
    assert len([e for e in fake.edits if e["text"] == "✓ noted."]) == 1
    assert conn.execute("SELECT COUNT(*) FROM processed_callbacks").fetchone()[0] == 1
