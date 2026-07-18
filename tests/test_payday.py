"""
Payday-anchored safe-to-spend and the /paid-today override (SPEC §4, ADR 004).

The pool resets on payday and the daily number counts down to the *next* payday;
/paid-today lets the owner log an early (weekend/bank-holiday) or late payday so
the cycle rolls to the real date without Sentinel keeping a holiday calendar.
"""

from datetime import date

from sentinel import commands, controller, db, render, state_keys


def make_cfg(tmp_path):
    return {
        "db_path": str(tmp_path / "ledger.db"),
        "payday": {"day_of_month": 23},
        "budgets": {"pool_monthly_cents": 120_000},   # €1,200 / cycle
        "controller": {"unlabeled_inflow_exclude_cents": 10_000},
        "thresholds": {"green_cents": 2_500, "red_cents": 1_000},
        "categorize": {"merchant_map_path": str(tmp_path / "merchant_map.json"),
                       "rules_path": None},
    }


def _conn(tmp_path):
    conn = db.connect(tmp_path / "ledger.db")
    db.init_db(conn)
    return conn


def _spend(conn, category, name, txns):
    cur = conn.execute(
        "INSERT INTO merchants (name_normalized, category, categorized_by) VALUES (?, ?, 'dict')",
        (name, category))
    db.insert_transactions(conn, [
        {"account_id": "acc-uid-1", "booking_date": day, "amount_cents": cents,
         "merchant_raw": name, "merchant_id": cur.lastrowid, "source": "api"}
        for day, cents in txns])
    conn.commit()


# ── Scheduled cycle (no override) ───────────────────────────────────────────


def test_cycle_counts_down_to_the_scheduled_payday(tmp_path):
    conn = _conn(tmp_path)
    cfg = make_cfg(tmp_path)
    before = controller.safe_to_spend(conn, cfg, date(2026, 7, 18))
    assert before["cycle_start"] == "2026-06-23"
    assert before["next_payday"] == "2026-07-23"
    assert before["days_left"] == 5              # 18 → 23 Jul
    assert before["cycle_start_source"] == "scheduled"
    # On payday itself a fresh cycle begins.
    on_payday = controller.safe_to_spend(conn, cfg, date(2026, 7, 23))
    assert on_payday["cycle_start"] == "2026-07-23"
    assert on_payday["next_payday"] == "2026-08-23"
    assert on_payday["days_left"] == 31          # 23 Jul → 23 Aug
    conn.close()


def test_pool_resets_on_payday(tmp_path):
    conn = _conn(tmp_path)
    cfg = make_cfg(tmp_path)
    _spend(conn, "Groceries", "TESCO", [("2026-07-10", -90_000)])  # €900, this cycle
    before = controller.safe_to_spend(conn, cfg, date(2026, 7, 20))
    assert before["discretionary_spent_cents"] == 90_000
    assert before["remaining_cents"] == 30_000
    # The 23rd starts a new cycle: the 10 Jul spend is now last cycle's, excluded.
    after = controller.safe_to_spend(conn, cfg, date(2026, 7, 23))
    assert after["discretionary_spent_cents"] == 0
    assert after["remaining_cents"] == 120_000, "pool resets on payday"
    conn.close()


# ── /paid-today override ────────────────────────────────────────────────────


def test_paid_today_rolls_the_cycle_when_paid_early(tmp_path):
    """The invariant the command exists for: salary lands early (23rd on a
    weekend), the owner logs it, and the cycle + pool roll to that day."""
    conn = _conn(tmp_path)
    cfg = make_cfg(tmp_path)
    _spend(conn, "Groceries", "TESCO", [("2026-07-10", -60_000)])  # last cycle
    _spend(conn, "Groceries", "SPAR", [("2026-07-22", -10_000)])   # new cycle
    # Before logging, the 22nd is still the tail of the June-23 cycle.
    before = controller.safe_to_spend(conn, cfg, date(2026, 7, 22))
    assert before["cycle_start"] == "2026-06-23" and before["days_left"] == 1
    assert before["discretionary_spent_cents"] == 70_000

    reply = commands.do_paid_today(conn, cfg, "", date(2026, 7, 21))  # "paid today", the 21st
    assert "21 Jul" in reply and "2 days early" in reply and "22 Aug" in reply
    assert db.get_state(conn, state_keys.payday_actual(2026, 7)) == "2026-07-21"

    after = controller.safe_to_spend(conn, cfg, date(2026, 7, 22))
    assert after["cycle_start"] == "2026-07-21"
    assert after["cycle_start_source"] == "logged"
    assert after["next_payday"] == "2026-08-23" and after["days_left"] == 32
    assert after["discretionary_spent_cents"] == 10_000, "only the 22 Jul spend is this cycle"
    assert after["remaining_cents"] == 110_000
    conn.close()


def test_paid_today_is_idempotent(tmp_path):
    conn = _conn(tmp_path)
    cfg = make_cfg(tmp_path)
    first = commands.do_paid_today(conn, cfg, "", date(2026, 7, 21))
    second = commands.do_paid_today(conn, cfg, "", date(2026, 7, 21))
    assert first == second
    assert db.get_state(conn, state_keys.payday_actual(2026, 7)) == "2026-07-21"
    # A re-log leaves the computed cycle unchanged.
    a = controller.safe_to_spend(conn, cfg, date(2026, 7, 25))
    b = controller.safe_to_spend(conn, cfg, date(2026, 7, 25))
    assert a == b and a["cycle_start"] == "2026-07-21"
    conn.close()


def test_paid_today_explicit_date_maps_to_nearest_cycle(tmp_path):
    conn = _conn(tmp_path)
    cfg = make_cfg(tmp_path)
    reply = commands.do_paid_today(conn, cfg, "2026-05-24", date(2026, 7, 10))
    assert "24 May" in reply and "1 day late" in reply
    assert db.get_state(conn, state_keys.payday_actual(2026, 5)) == "2026-05-24"
    assert db.get_state(conn, state_keys.payday_actual(2026, 7)) is None
    conn.close()


def test_paid_today_rejects_a_bad_date_and_writes_nothing(tmp_path):
    conn = _conn(tmp_path)
    cfg = make_cfg(tmp_path)
    reply = commands.do_paid_today(conn, cfg, "not-a-date", date(2026, 7, 10))
    assert "Couldn't read" in reply and "YYYY-MM-DD" in reply
    assert db.get_state(conn, state_keys.payday_actual(2026, 7)) is None
    conn.close()


def test_cycle_month_for_maps_to_the_nearest_scheduled_payday(tmp_path):
    cfg = make_cfg(tmp_path)
    assert controller.cycle_month_for(cfg, date(2026, 7, 21)) == (2026, 7)
    assert controller.cycle_month_for(cfg, date(2026, 7, 24)) == (2026, 7)
    # Early January is nearer December's 23rd than January's — crosses the year.
    assert controller.cycle_month_for(cfg, date(2026, 1, 2)) == (2025, 12)


# ── Rendered surface (the UX the owner sees) ────────────────────────────────


def test_today_and_status_read_cleanly(tmp_path):
    conn = _conn(tmp_path)
    cfg = make_cfg(tmp_path)
    _spend(conn, "Groceries", "TESCO", [("2026-07-15", -40_000)])
    today = render.compose_daily(conn, cfg, date(2026, 7, 18))
    assert today.startswith("Safe to spend today: €")
    assert "to payday" in today and "Pool €1,200.00" in today
    status = render.status_text(conn, cfg, date(2026, 7, 18))
    assert status.startswith("📊 This pay cycle")
    assert "Groceries" in status and "Safe to spend today: €" in status
    assert "acc-uid" not in status, "no account id can reach the chat"
    conn.close()


def test_sync_reply_is_plain_english(tmp_path):
    assert render.sync_reply(0, 249, 0) == (
        "✅ Bank sync done — you're already up to date.\n"
        "• No new transactions (checked 249)\n• No new alerts.")
    busy = render.sync_reply(12, 249, 1)
    assert "12 new transactions (checked 249)" in busy
    assert "1 new alert" in busy
