import json
import re
from datetime import date

import pytest

from sentinel import commands, controller, db, notify, render, telegram
from tests.test_reports import AS_OF, build_ledger

# /today and the daily push lead with the headline line, then a pool + payday
# block; days-left lives in the "N days to payday" clause, not the headline.
HEAD_RE = re.compile(r"^Safe to spend today: €[\d,]+\.\d{2}  [🟢🟡🔴]$", re.M)
DAYS_RE = re.compile(r"(\d+) days? to payday")


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.updates = []

    def __call__(self, token, method, payload):
        if method == "sendMessage":
            self.sent.append(payload["text"])
            return {"ok": True, "result": {}}
        if method == "getUpdates":
            batch, self.updates = self.updates, []
            return {"ok": True, "result": batch}
        raise AssertionError(f"unexpected telegram method {method}")


def make_cfg(tmp_path):
    return {
        "db_path": str(tmp_path / "ledger.db"),
        "budgets": {"pool_monthly_cents": 120_000},
        "controller": {"graduation_surplus_cents": 100_000},
        "thresholds": {"green_cents": 2_500, "red_cents": 1_000},
        "telegram": {"poll_timeout_seconds": 50},
        "categorize": {"merchant_map_path": str(tmp_path / "merchant_map.json"), "rules_path": None},
        "enable_banking": {"api_daily_call_limit": 4},
    }


@pytest.fixture()
def bot(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "ledger.db")
    db.init_db(conn)
    build_ledger(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "777")
    fake = FakeTelegram()
    monkeypatch.setattr(telegram, "_post_telegram", fake)
    yield conn, make_cfg(tmp_path), fake
    conn.close()


def _mk_spend(conn, category, name, txns):
    cur = conn.execute(
        "INSERT INTO merchants (name_normalized, category, categorized_by) VALUES (?, ?, 'dict')", (name, category)
    )
    rows = [
        {
            "account_id": "acc-uid-1",
            "booking_date": day,
            "amount_cents": cents,
            "merchant_raw": name,
            "merchant_id": cur.lastrowid,
            "source": "api",
        }
        for day, cents in txns
    ]
    db.insert_transactions(conn, rows)
    conn.commit()


# ── Daily push ─────────────────────────────────────────────────────────────


def test_daily_push_for_7_consecutive_days_live_and_idempotent(bot):
    conn, cfg, fake = bot
    for day in range(8, 15):  # 2026-07-08 … 2026-07-14
        assert notify.push_daily(conn, cfg, as_of=date(2026, 7, day)) is True
    pushes = [m for m in fake.sent if HEAD_RE.search(m)]
    assert len(pushes) == 7, fake.sent
    days_left = [int(DAYS_RE.search(m).group(1)) for m in pushes]
    assert days_left == [15, 14, 13, 12, 11, 10, 9], "days count down to payday (23rd), live"
    already = len(fake.sent)
    assert notify.push_daily(conn, cfg, as_of=date(2026, 7, 14)) is False
    assert len(fake.sent) == already


# ── Pool safe-to-spend ─────────────────────────────────────────────────────


def test_safe_to_spend_rolls_forward_and_punishes_overspend(tmp_path):
    conn = db.connect(tmp_path / "ledger.db")
    db.init_db(conn)
    cfg = make_cfg(tmp_path)  # €1,200 pool, payday 23 (default)
    # Underspend rolls forward: same (empty) ledger, one day closer to payday →
    # the daily figure rises.
    day1 = controller.safe_to_spend(conn, cfg, date(2026, 7, 1))
    day2 = controller.safe_to_spend(conn, cfg, date(2026, 7, 2))
    assert day2["safe_today_cents"] > day1["safe_today_cents"], "underspend rolls forward"
    assert day1["days_left"] == 22, "2026-07-01 → next payday 2026-07-23"
    assert day1["cycle_start"] == "2026-06-23" and day1["next_payday"] == "2026-07-23"
    # Spending in-cycle drags the SAME day's figure below its no-spend baseline.
    _mk_spend(conn, "Groceries", "TESCO", [("2026-07-02", -5_000)])  # discretionary bucket
    over2 = controller.safe_to_spend(conn, cfg, date(2026, 7, 2))
    assert over2["safe_today_cents"] < day2["safe_today_cents"], "overspend drags it down"
    _mk_spend(conn, "Groceries", "TESCO2", [("2026-07-03", -200_000)])  # blow the pool
    assert controller.safe_to_spend(conn, cfg, date(2026, 7, 3))["safe_today_cents"] == 0
    conn.close()


def test_uncategorized_spend_counts_against_the_pool(tmp_path):
    conn = db.connect(tmp_path / "ledger.db")
    db.init_db(conn)
    cfg = make_cfg(tmp_path)
    base = controller.safe_to_spend(conn, cfg, date(2026, 7, 1))
    _mk_spend(conn, "Uncategorized", "MYSTERY", [("2026-07-01", -10_000)])
    after = controller.safe_to_spend(conn, cfg, date(2026, 7, 1))
    assert after["remaining_cents"] == base["remaining_cents"] - 10_000
    assert after["by_bucket_cents"]["Other"] == 10_000  # Uncategorized → Other (discretionary)
    conn.close()


def test_large_unlabeled_inflow_does_not_inflate_the_pool(tmp_path):
    """A €1,000 transfer from a not-yet-mapped sender must NOT add to safe-to-spend
    as phantom negative spend; a small refund still nets normally."""
    conn = db.connect(tmp_path / "ledger.db")
    db.init_db(conn)
    cfg = make_cfg(tmp_path)
    cfg["controller"]["unlabeled_inflow_exclude_cents"] = 10_000  # €100
    base = controller.safe_to_spend(conn, cfg, date(2026, 7, 1))
    _mk_spend(conn, "Uncategorized", "MYSTERY SENDER", [("2026-07-01", 100_000)])  # €1,000 inflow
    after = controller.safe_to_spend(conn, cfg, date(2026, 7, 1))
    assert after["remaining_cents"] == base["remaining_cents"], "big inflow excluded from pool"
    assert after["unlabeled_inflow_cents"] == 100_000 and after["unlabeled_inflow_count"] == 1
    _mk_spend(conn, "Groceries", "TESCO", [("2026-07-01", 500)])  # €5 refund, small → still nets
    net = controller.safe_to_spend(conn, cfg, date(2026, 7, 1))
    assert net["remaining_cents"] == base["remaining_cents"] + 500
    conn.close()


def test_graduation_surplus_reads_last_completed_month(bot):
    conn, cfg, _ = bot
    grad = controller.graduation_surplus(conn, cfg, AS_OF)  # as_of 2026-07-14 → June
    assert grad["month"] == "2026-06"
    assert {"income_cents", "spend_cents", "surplus_cents", "target_cents", "met"} <= set(grad)


# ── Commands ───────────────────────────────────────────────────────────────


def test_recat_moves_category_and_teaches_merchant(bot):
    conn, cfg, _ = bot
    ref = conn.execute(
        "SELECT id FROM transactions WHERE merchant_raw = 'COFFEE ANGEL' AND booking_date = '2026-07-03'"
    ).fetchone()["id"][:8]
    reply = commands.handle_command(conn, cfg, f"/recat {ref} Dates", AS_OF)
    assert reply.startswith("✅") and "always Dates" in reply
    cats = {
        r[0]
        for r in conn.execute("SELECT category FROM v_transactions_categorized WHERE merchant_raw = 'COFFEE ANGEL'")
    }
    assert cats == {"Dates"}  # merchant taught → every COFFEE ANGEL is Dates
    row = conn.execute(
        "SELECT category, categorized_by FROM merchants WHERE name_normalized = 'COFFEE ANGEL'"
    ).fetchone()
    assert tuple(row) == ("Dates", "manual")
    saved = json.loads(open(cfg["categorize"]["merchant_map_path"]).read())
    assert saved["COFFEE ANGEL"] == {"category": "Dates", "by": "manual", "confidence": 1.0}


def test_date_flips_one_txn_without_touching_merchant(bot):
    conn, cfg, _ = bot
    txns = conn.execute(
        "SELECT id FROM transactions WHERE merchant_raw = 'LAUNDRETTE' "
        "AND booking_date IN ('2026-07-04','2026-07-11') "
        "ORDER BY booking_date"
    ).fetchall()
    reply = commands.do_date(conn, txns[1]["id"][:8])
    assert reply.startswith("✅") and "just this one" in reply
    cats = dict(
        conn.execute(
            "SELECT id, category FROM v_transactions_categorized WHERE id IN (?, ?)", (txns[0]["id"], txns[1]["id"])
        ).fetchall()
    )
    assert cats[txns[1]["id"]] == "Dates"
    assert cats[txns[0]["id"]] == "Other"  # sibling untouched
    assert (
        conn.execute("SELECT category FROM merchants WHERE name_normalized = 'LAUNDRETTE'").fetchone()["category"]
        == "Other"
    )


def test_owner_id_authorizes_independently_of_chat_id(bot, monkeypatch):
    """Delivery goes to TELEGRAM_CHAT_ID; authority follows TELEGRAM_OWNER_ID. Point
    the bot at a group (chat 777) but let user 555 be the owner: only 555 drives it."""
    conn, cfg, fake = bot
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "555")
    fake.updates = [
        {"update_id": 21, "message": {"chat": {"id": 777}, "from": {"id": 555}, "text": "/today"}},  # owner
        {"update_id": 22, "message": {"chat": {"id": 777}, "from": {"id": 777}, "text": "/status"}},  # not owner
    ]
    handled = commands.process_updates(conn, cfg, as_of=AS_OF)
    assert handled == 1
    assert len(fake.sent) == 1 and fake.sent[0].startswith("Safe to spend today:")


def test_status_surfaces_quarantined_rows(bot):
    conn, cfg, _ = bot
    db.quarantine_row(conn, "api", "non-EUR booked currency 'USD'", {"ref": "x"}, "acc-uid-1")
    conn.commit()
    reply = commands.handle_command(conn, cfg, "/status", AS_OF)
    assert "1 row quarantined" in reply.text


def test_labeled_refund_nets_but_large_unlabeled_inflow_does_not(tmp_path):
    """N5: a €150 labeled Shopping refund nets against its purchase; a €1,000
    Uncategorized inflow is held out of the pool and surfaced for labeling."""
    conn = db.connect(tmp_path / "ledger.db")
    db.init_db(conn)
    cfg = make_cfg(tmp_path)
    cfg["controller"]["unlabeled_inflow_exclude_cents"] = 10_000  # €100
    base = controller.safe_to_spend(conn, cfg, date(2026, 7, 1))
    _mk_spend(conn, "Shopping", "ZARA", [("2026-07-01", -20_000), ("2026-07-01", 15_000)])
    after = controller.safe_to_spend(conn, cfg, date(2026, 7, 1))
    assert base["remaining_cents"] - after["remaining_cents"] == 5_000, "refund nets (€200 − €150)"
    assert after["by_bucket_cents"]["Other"] == 5_000  # Shopping → Other
    assert after["unlabeled_inflow_count"] == 0, "a labeled refund is not an unlabeled inflow"
    _mk_spend(conn, "Uncategorized", "MYSTERY", [("2026-07-01", 100_000)])
    held = controller.safe_to_spend(conn, cfg, date(2026, 7, 1))
    assert held["remaining_cents"] == after["remaining_cents"], "big unlabeled inflow excluded"
    assert held["unlabeled_inflow_count"] == 1 and held["unlabeled_inflow_cents"] == 100_000
    conn.close()


def test_month_surplus_nets_a_labeled_refund(tmp_path):
    conn = db.connect(tmp_path / "ledger.db")
    db.init_db(conn)
    _mk_spend(conn, "Income", "SALARY", [("2026-06-01", 300_000)])
    _mk_spend(conn, "Shopping", "ZARA", [("2026-06-10", -20_000), ("2026-06-12", 15_000)])
    s = controller.month_surplus(conn, "2026-06", 10_000)
    assert s["spend_cents"] == 5_000, "the €150 refund nets against the €200 purchase"
    assert s["surplus_cents"] == 300_000 - 5_000
    conn.close()


def test_process_updates_only_honors_owner_sender(bot):
    """Authorize by the SENDER's id, not the chat id: a stranger who is a member
    of the owner's chat/group must be rejected."""
    conn, cfg, fake = bot
    fake.updates = [
        {"update_id": 11, "message": {"chat": {"id": 777}, "from": {"id": 777}, "text": "/today"}},
        # stranger posting INTO the owner chat (same chat.id, different from.id):
        {"update_id": 12, "message": {"chat": {"id": 777}, "from": {"id": 666}, "text": "/status"}},
        {"update_id": 13, "message": {"chat": {"id": 777}, "from": {"id": 777}, "text": "hello"}},
    ]
    handled = commands.process_updates(conn, cfg, as_of=AS_OF)
    assert handled == 1
    assert len(fake.sent) == 1 and fake.sent[0].startswith("Safe to spend today:")
    assert db.get_state(conn, "tg_update_offset") == "14"


def test_unknown_command_returns_help(bot):
    conn, cfg, _ = bot
    reply = commands.handle_command(conn, cfg, "/frobnicate", AS_OF)
    for cmd in ("/today", "/status", "/cat", "/paidtoday", "/sync", "/recat", "/date", "/help"):
        assert cmd in reply


def test_help_and_start_and_group_suffix_route_to_help(bot):
    conn, cfg, _ = bot
    for text in ("/help", "/start", "/help@sentinelbot"):
        assert commands.handle_command(conn, cfg, text, AS_OF) == render.HELP_TEXT


def test_paidtoday_aliases_all_route(bot):
    conn, cfg, _ = bot
    for text in ("/paidtoday", "/paid-today", "/paid_today", "/paidtoday@sentinelbot"):
        reply = commands.handle_command(conn, cfg, text, AS_OF)
        assert reply.startswith("✅ Logged payday"), (text, reply)


def test_bot_command_menu_is_telegram_valid():
    """setMyCommands names must be [a-z0-9_]{1,32}; the menu and /help must not drift."""
    menu = render.bot_command_menu()
    names = {c["command"] for c in menu}
    assert {"today", "status", "cat", "paidtoday", "sync", "recat", "date", "help"} <= names
    for c in menu:
        assert re.fullmatch(r"[a-z0-9_]{1,32}", c["command"]), c
        assert 1 <= len(c["description"]) <= 256
        assert f"/{c['command']}" in render.HELP_TEXT  # menu ⇄ help stay in sync


def test_set_my_commands_registers_via_transport(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "777")
    calls = []
    monkeypatch.setattr(
        telegram,
        "_post_telegram",
        lambda tok, method, payload: calls.append((method, payload)) or {"ok": True, "result": True},
    )
    telegram.set_my_commands(render.bot_command_menu())
    assert len(calls) == 1 and calls[0][0] == "setMyCommands"
    assert calls[0][1]["commands"][0] == {"command": "today", "description": "What's safe to spend today"}


def test_cat_shows_refs(bot):
    conn, cfg, _ = bot
    reply = commands.handle_command(conn, cfg, "/cat coffee/snacks", AS_OF)
    assert reply.parse_mode == "HTML"  # rendered as a monospace table
    assert "Coffee/Snacks" in reply.text
    assert re.search(r"[0-9a-f]{8}", reply.text)  # an 8-char ref for /recat
    assert "COFFEE ANGEL" in reply.text and "€3.50" in reply.text


def test_cat_no_arg_and_unknown_offer_a_button_picker(bot):
    conn, cfg, _ = bot
    for text in ("/cat", "/cat nonsense"):
        reply = commands.handle_command(conn, cfg, text, AS_OF)
        buttons = [b for row in reply.reply_markup["inline_keyboard"] for b in row]
        assert any(b["callback_data"] == "cat:Dining" for b in buttons), text


def test_cat_bucket_name_drills_into_the_rollup(bot):
    """/cat misc (the Other rollup shown in /status) lists its sub-labels, not 0.00."""
    conn, cfg, _ = bot
    reply = commands.handle_command(conn, cfg, "/cat misc", AS_OF)
    assert reply.parse_mode == "HTML" and "Misc." in reply.text


def test_sync_replies_when_unconfigured_and_is_allowance_exempt(bot, monkeypatch, tmp_path):
    conn, cfg, _ = bot
    for var in ("ENABLE_BANKING_APP_ID", "ENABLE_BANKING_PRIVATE_KEY_PATH"):
        monkeypatch.delenv(var, raising=False)
    assert "Phase 0" in commands.do_sync(conn, cfg)
    key_file = tmp_path / "key.pem"
    key_file.write_text("not-a-real-key")
    monkeypatch.setenv("ENABLE_BANKING_APP_ID", "app")
    monkeypatch.setenv("ENABLE_BANKING_PRIVATE_KEY_PATH", str(key_file))
    db.set_state(conn, "eb_account_uids", '["acc-uid-1"]')
    from datetime import datetime

    today = datetime.now(db.TZ).date().isoformat()
    db.set_state(conn, f"api_calls:{today}", "4")  # allowance spent — but /sync is attended
    conn.commit()
    # Attended (PSU headers) → exempt from the allowance (RTS Art. 36(5)). Assert
    # the SPECIFIC behavior, not a "failed" substring three paths could produce:
    # signing the JWT fails on the fake key (the sync-failed path), and the
    # allowance counter stays untouched because attended access never consumes it.
    reply = commands.do_sync(conn, cfg)
    assert "Sync failed" in reply
    assert db.get_state(conn, f"api_calls:{today}") == "4", "attended /sync must not consume allowance"


# ── Weekly digest (deterministic template) ─────────────────────────────────


def test_digest_is_deterministic_template(bot):
    conn, cfg, fake_tg = bot
    text = notify.run_digest(conn, cfg, as_of=AS_OF)
    assert text.startswith("📊 Week")
    assert "Safe to spend today:" in text
    assert "surplus" in text.lower()  # deterministic graduation line
    assert fake_tg.sent == [text]
    assert "acc-uid-1" not in text, "no account id can ever reach the push"


def test_digest_dry_run_sends_nothing(bot):
    conn, cfg, fake_tg = bot
    assert notify.run_digest(conn, cfg, as_of=AS_OF, dry_run=True) is None
    assert fake_tg.sent == []
