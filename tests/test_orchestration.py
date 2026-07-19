from datetime import date, datetime, timedelta

import pytest

from sentinel import alerts, commands, db, notify, state_keys, telegram

POLICIES = "policies:\n  - name: food-delivery\n    bucket: FoodDelivery\n    cap_monthly_cents: 15000\n"


class FakeTG:
    def __init__(self):
        self.sent, self.edits = [], []
        self._mid = 500

    def __call__(self, token, method, payload):
        if method == "sendMessage":
            self._mid += 1
            self.sent.append(payload)
            return {"ok": True, "result": {"message_id": self._mid}}
        if method in ("editMessageText", "answerCallbackQuery"):
            self.edits.append(payload)
            return {"ok": True, "result": {}}
        if method == "getUpdates":
            return {"ok": True, "result": []}
        raise AssertionError(f"unexpected method {method}")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "ledger.db")
    db.init_db(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "777")
    fake = FakeTG()
    monkeypatch.setattr(telegram, "_post_telegram", fake)
    pol = tmp_path / "policies.yaml"
    pol.write_text(POLICIES)
    cfg = {
        "db_path": str(tmp_path / "ledger.db"),
        "policies": {"path": str(pol)},
        "categorize": {"merchant_map_path": str(tmp_path / "merchant_map.json"), "rules_path": None},
        # Hermetic: keep the developer machine's real bills.local.yaml out of
        # run_poll/do_sync alert counts (the tmp path loads as an empty registry).
        "bills": {"path": str(tmp_path / "bills.yaml")},
        "enable_banking": {"api_daily_call_limit": 4, "first_pull_days": 90},
    }
    return conn, cfg, fake


def _deliveroo(conn, day, cents):
    conn.execute(
        "INSERT OR IGNORE INTO merchants (name_normalized, category, categorized_by) "
        "VALUES ('DELIVEROO', 'FoodDelivery', 'dict')"
    )
    mid = conn.execute("SELECT id FROM merchants WHERE name_normalized='DELIVEROO'").fetchone()[0]
    db.insert_transactions(
        conn,
        [
            {
                "account_id": "a",
                "booking_date": day,
                "amount_cents": cents,
                "merchant_raw": "DELIVEROO",
                "merchant_id": mid,
                "source": "api",
            }
        ],
    )
    conn.commit()


# ── Durable watermark ───────────────────────────────────────────────────


def test_poll_alerts_uses_durable_watermark(env):
    conn, cfg, fake = env
    alerts.ensure_baseline(conn)  # no rows yet → baseline 0
    _deliveroo(conn, "2026-07-01", -6000)
    _deliveroo(conn, "2026-07-02", -6000)
    _deliveroo(conn, "2026-07-03", -6000)  # €180 MTD > €150 cap → only the 3rd alerts
    assert alerts.poll_alerts(conn, cfg, date(2026, 7, 3)) == 1
    assert len(fake.sent) == 1
    assert int(db.get_state(conn, state_keys.ALERTS_CHECKED_THROUGH)) > 0
    # re-run: nothing new past the watermark → no double alert
    assert alerts.poll_alerts(conn, cfg, date(2026, 7, 3)) == 0
    assert len(fake.sent) == 1


def test_watermark_loss_does_not_duplicate_the_alert(env):
    """Even if the watermark is lost (a crash before it advanced), the per-txn
    events guard makes the replay send nothing twice."""
    conn, cfg, fake = env
    alerts.ensure_baseline(conn)
    _deliveroo(conn, "2026-07-01", -20000)  # over cap immediately
    assert alerts.poll_alerts(conn, cfg, date(2026, 7, 1)) == 1
    db.set_state(conn, state_keys.ALERTS_CHECKED_THROUGH, "0")  # simulate watermark loss
    conn.commit()
    assert alerts.poll_alerts(conn, cfg, date(2026, 7, 1)) == 0
    assert len(fake.sent) == 1


def test_ensure_baseline_does_not_alert_the_backfill(env):
    conn, cfg, fake = env
    _deliveroo(conn, "2026-07-01", -20000)  # a pre-existing over-cap charge
    alerts.ensure_baseline(conn)  # baseline AFTER the backfill → it must not alert
    assert alerts.poll_alerts(conn, cfg, date(2026, 7, 1)) == 0
    assert fake.sent == []


# ── Dry-run is read-only ───────────────────────────────────────────────


def test_run_poll_dry_run_sends_nothing_and_advances_no_watermark(env):
    conn, cfg, fake = env
    _deliveroo(conn, "2026-07-01", -20000)
    notify.run_poll(conn, cfg, as_of=date(2026, 7, 1), dry_run=True)
    assert fake.sent == [], "dry-run must not send"
    assert db.get_state(conn, state_keys.ALERTS_CHECKED_THROUGH) is None, "dry-run must not touch state"


# ── Attended /sync alerts too ───────────────────────────────────────────


def test_sync_categorizes_and_alerts(env, monkeypatch, tmp_path):
    conn, cfg, fake = env
    key = tmp_path / "key.pem"
    key.write_text("x")
    monkeypatch.setenv("ENABLE_BANKING_APP_ID", "app")
    monkeypatch.setenv("ENABLE_BANKING_PRIVATE_KEY_PATH", str(key))
    db.set_state(conn, state_keys.EB_ACCOUNT_UIDS, '["a"]')
    conn.commit()
    today = datetime.now(db.TZ).date()  # a charge "now" is always in the current policy month

    def fake_run_ingest(conn_, client, uids, **kw):
        _deliveroo(conn_, today.isoformat(), -20000)  # over-cap
        return 1, 1

    monkeypatch.setattr(commands.ingest, "run_ingest", fake_run_ingest)
    monkeypatch.setattr(commands.ingest, "build_client", lambda *a, **k: object())
    reply = commands.do_sync(conn, cfg)
    assert "1 new alert" in reply and "Bank sync done" in reply
    assert len(fake.sent) == 1


def test_sync_is_allowance_exempt(env, monkeypatch, tmp_path):
    """/sync is attended (PSU headers) → it must NOT consume the unattended
    daily allowance, even when the allowance is already spent."""
    conn, cfg, fake = env
    key = tmp_path / "key.pem"
    key.write_text("x")
    monkeypatch.setenv("ENABLE_BANKING_APP_ID", "app")
    monkeypatch.setenv("ENABLE_BANKING_PRIVATE_KEY_PATH", str(key))
    db.set_state(conn, state_keys.EB_ACCOUNT_UIDS, '["a"]')
    today = datetime.now(db.TZ).date().isoformat()
    db.set_state(conn, state_keys.api_calls(today), "4")  # allowance fully spent
    conn.commit()
    monkeypatch.setattr(commands.ingest, "run_ingest", lambda *a, **k: (0, 0))
    monkeypatch.setattr(commands.ingest, "build_client", lambda *a, **k: object())
    commands.do_sync(conn, cfg)
    assert db.get_state(conn, state_keys.api_calls(today)) == "4", "attended /sync must not consume allowance"


# ── Listen-loop resilience ─────────────────────────────────────────────


class _Stop(Exception):
    pass


def test_listen_loop_backs_off_and_recovers_from_transient_error(env, monkeypatch):
    conn, cfg, fake = env
    monkeypatch.setattr(commands.time, "sleep", lambda s: None)  # don't actually sleep
    seq = [
        telegram.NotifyError("502 from a flaky proxy"),
        [{"update_id": 1, "message": {"chat": {"id": 777}, "from": {"id": 777}, "text": "/today"}}],
        _Stop(),  # break out of the otherwise-infinite listen loop
    ]

    def fake_get_updates(offset, timeout):
        item = seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(telegram, "get_updates", fake_get_updates)
    with pytest.raises(_Stop):
        commands.process_updates(conn, cfg, listen=True)
    # despite the first 502, the /today command was handled after the backoff
    assert any(p["text"].startswith("Safe to spend") for p in fake.sent)


def test_post_telegram_non_json_reply_is_notifyerror(monkeypatch):
    class FakeResp:
        status_code = 502

        def json(self):
            raise ValueError("Expecting value")  # HTML error page, not JSON

    class FakeSession:
        def post(self, url, json, timeout):
            return FakeResp()

    monkeypatch.setattr(telegram, "_http", FakeSession)  # code posts via _http(), not requests.post
    with pytest.raises(telegram.NotifyError, match="non-JSON"):
        telegram._post_telegram("secret-token", "getUpdates", {})


def test_getupdates_read_timeout_tracks_poll_timeout_short_calls_stay_snappy(monkeypatch):
    """The long-poll read timeout must outlast the server-side poll wait (else a
    healthy poll is cut off) and scale with it — so lowering poll_timeout_seconds
    shrinks the deaf window. Every other call keeps the short read budget: a stuck
    reply fails in seconds, not the ~65s that used to wedge the listener."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "777")

    class OkResp:
        status_code = 200

        def json(self):
            return {"ok": True, "result": []}

    seen: dict[str, tuple[int, int]] = {}

    class RecordingSession:
        def post(self, url, json, timeout):
            seen[url.rsplit("/", 1)[-1]] = timeout
            return OkResp()

    monkeypatch.setattr(telegram, "_http", RecordingSession)
    telegram.get_updates(0, 25)  # server-side poll wait = 25s
    telegram.send_message("hi")  # a prompt call
    assert seen["getUpdates"] == (
        telegram.CONNECT_TIMEOUT_SECONDS,
        25 + telegram.LONG_POLL_READ_MARGIN_SECONDS,
    ), "getUpdates read must be poll timeout + margin"
    assert seen["sendMessage"] == (telegram.CONNECT_TIMEOUT_SECONDS, telegram.SHORT_READ_TIMEOUT_SECONDS)
    assert seen["sendMessage"][1] < 65, "a short call must not inherit the old 65s long-poll patience"


def test_transport_reuses_one_keepalive_session(monkeypatch):
    """One pooled session is reused (no fresh DNS+TCP+TLS per reply) and its socket
    carries SO_KEEPALIVE so a silently-dropped long-poll is detected by the kernel,
    not only when the read timeout fires."""
    import socket

    monkeypatch.setattr(telegram, "_session", None)  # force a clean build; auto-restored
    session = telegram._http()
    assert telegram._http() is session, "the session is a reused singleton, not per-call"
    adapter = session.get_adapter("https://api.telegram.org")
    assert isinstance(adapter, telegram._KeepAliveAdapter)
    sockopts = adapter.poolmanager.connection_pool_kw.get("socket_options") or []
    assert (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1) in sockopts, "SO_KEEPALIVE must be set on pooled sockets"


# ── Consent-expiry nag fires from the poll (N21) ────────────────────────────


def test_run_poll_warns_on_consent_expiry_once_per_day(env):
    conn, cfg, fake = env
    today = datetime.now(db.TZ).date()
    db.set_state(conn, state_keys.CONSENT_EXPIRY, (today + timedelta(days=10)).isoformat())
    conn.commit()
    notify.run_poll(conn, cfg, as_of=today)
    warns = [m for m in fake.sent if "consent expires" in m["text"]]
    assert len(warns) == 1, "the T−14d nag must fire from the scheduled poll, not only the CLI"
    notify.run_poll(conn, cfg, as_of=today)  # same day → idempotent
    assert len([m for m in fake.sent if "consent expires" in m["text"]]) == 1


def test_run_poll_warns_that_expired_consent_blocks_polls(env):
    conn, cfg, fake = env
    today = datetime.now(db.TZ).date()
    db.set_state(conn, state_keys.CONSENT_EXPIRY, (today - timedelta(days=2)).isoformat())
    conn.commit()
    notify.run_poll(conn, cfg, as_of=today)
    assert any("EXPIRED" in m["text"] and "re-auth" in m["text"].lower() for m in fake.sent)


def test_dry_run_poll_never_persists_the_consent_nag(env):
    conn, cfg, fake = env
    today = datetime.now(db.TZ).date()
    db.set_state(conn, state_keys.CONSENT_EXPIRY, (today + timedelta(days=5)).isoformat())
    conn.commit()
    notify.run_poll(conn, cfg, as_of=today, dry_run=True)
    assert fake.sent == [], "dry-run must not send"
    assert db.get_state(conn, state_keys.CONSENT_WARNED_ON) is None


# ── First dry-run poll must not alert the whole backfill (N25) ──────────────


def test_first_dry_run_poll_does_not_alert_the_backfill(env):
    conn, cfg, fake = env
    for day in range(1, 6):
        _deliveroo(conn, f"2026-07-0{day}", -20000)  # 5 over-cap charges, no watermark yet
    fired = notify.run_poll(conn, cfg, as_of=date(2026, 7, 6), dry_run=True)
    assert fired == 0, "a first-ever dry-run poll must not 'alert' the entire backfill"
    assert fake.sent == []
    assert db.get_state(conn, state_keys.ALERTS_CHECKED_THROUGH) is None


# ── Listener resilience (N8, N9) ────────────────────────────────────────────


def test_listen_dry_run_advances_offset_in_memory_without_persisting(env, monkeypatch):
    """listen+dry-run must not busy-spin re-reading the same batch: the offset is
    tracked in memory (never persisted), so the next getUpdates asks past it."""
    conn, cfg, fake = env
    offsets = []
    seq = [
        [{"update_id": 5, "message": {"chat": {"id": 777}, "from": {"id": 777}, "text": "/today"}}],
        [],  # nothing pending now
        _Stop(),
    ]

    def fake_get_updates(offset, timeout):
        offsets.append(offset)
        item = seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(telegram, "get_updates", fake_get_updates)
    with pytest.raises(_Stop):
        commands.process_updates(conn, cfg, listen=True, dry_run=True)
    assert offsets[:2] == [0, 6], "second poll used the in-memory-advanced offset, not 0 again"
    assert db.get_state(conn, state_keys.TG_UPDATE_OFFSET) is None, "dry-run persists no offset"


def test_batch_survives_a_poison_update_and_advances_past_it(env, monkeypatch):
    """A handler fault that is NOT a NotifyError (a WAL lock, a malformed payload)
    must not crash the batch or wedge on the poison update — advance past it."""
    conn, cfg, fake = env
    real = commands._handle_update

    def flaky(conn_, cfg_, update, owner, as_of, dry_run):
        if update.get("update_id") == 1:
            raise RuntimeError("poison payload")
        return real(conn_, cfg_, update, owner, as_of, dry_run)

    monkeypatch.setattr(commands, "_handle_update", flaky)
    monkeypatch.setattr(
        telegram,
        "get_updates",
        lambda o, t: [
            {"update_id": 1, "message": {"chat": {"id": 777}, "from": {"id": 777}, "text": "/today"}},
            {"update_id": 2, "message": {"chat": {"id": 777}, "from": {"id": 777}, "text": "/today"}},
        ],
    )
    handled = commands.process_updates(conn, cfg, listen=False)  # one-shot
    assert handled == 1, "the good update was handled; the poison one didn't crash the batch"
    assert any(p["text"].startswith("Safe to spend") for p in fake.sent)
    assert db.get_state(conn, state_keys.TG_UPDATE_OFFSET) == "3", "offset advanced past BOTH updates"
