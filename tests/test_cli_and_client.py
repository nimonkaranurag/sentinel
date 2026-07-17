import textwrap

import pytest
import requests

from sentinel import authorize, controller, db, ingest, notify, telegram

# ── Enable Banking client: retry + pagination ──────────────────────────


class _Resp:
    def __init__(self, body, ok=True):
        self._body, self.ok = body, ok

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError("500 server error")

    def json(self):
        return self._body


@pytest.fixture()
def no_jwt(monkeypatch):
    monkeypatch.setattr(ingest, "make_jwt", lambda *a, **k: "fake-jwt")  # no openssl
    monkeypatch.setattr(ingest.time, "sleep", lambda s: None)           # no real backoff


def test_client_retries_a_transient_5xx_then_succeeds(monkeypatch, no_jwt):
    calls = {"n": 0}

    def fake_get(url, **kw):
        calls["n"] += 1
        return _Resp({"transactions": [{"entry_reference": "a"}]}, ok=calls["n"] >= 2)

    monkeypatch.setattr(ingest.requests, "get", fake_get)
    client = ingest.EnableBankingClient("app", "/k", max_retries=2, retry_backoff=0.0)
    got = list(client.iter_transactions("uid", "2026-01-01"))
    assert got == [{"entry_reference": "a"}]
    assert calls["n"] == 2  # failed once, retried once


def test_client_gives_up_after_max_retries(monkeypatch, no_jwt):
    monkeypatch.setattr(ingest.requests, "get", lambda url, **kw: _Resp({}, ok=False))
    client = ingest.EnableBankingClient("app", "/k", max_retries=1, retry_backoff=0.0)
    with pytest.raises(requests.HTTPError):
        list(client.iter_transactions("uid", "2026-01-01"))


def test_client_follows_pagination(monkeypatch, no_jwt):
    def fake_get(url, **kw):
        if kw["params"].get("continuation_key"):
            return _Resp({"transactions": [{"id": 3}]})
        return _Resp({"transactions": [{"id": 1}, {"id": 2}], "continuation_key": "k"})

    monkeypatch.setattr(ingest.requests, "get", fake_get)
    client = ingest.EnableBankingClient("app", "/k")
    assert list(client.iter_transactions("uid", "2026-01-01")) == [{"id": 1}, {"id": 2}, {"id": 3}]


def test_client_page_cap_stops_a_runaway_continuation(monkeypatch, no_jwt):
    # every page returns a continuation_key → must stop at max_pages, not loop
    monkeypatch.setattr(ingest.requests, "get",
                        lambda url, **kw: _Resp({"transactions": [{"id": 1}], "continuation_key": "k"}))
    client = ingest.EnableBankingClient("app", "/k", max_pages=4)
    assert len(list(client.iter_transactions("uid", "2026-01-01"))) == 4


def test_local_ip_returns_an_address():
    ip = ingest.local_ip()
    assert isinstance(ip, str) and ip.count(".") == 3


# ── CLI entrypoints ──────────────────────────────────────────────────────────


def _cfg(tmp_path, extra=""):
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(f"""
        db_path: {tmp_path / 'ledger.db'}
        currency: EUR
        budgets: {{pool_monthly_cents: 120000}}
        controller: {{graduation_surplus_cents: 100000}}
        thresholds: {{green_cents: 2500, red_cents: 1000}}
        categorize: {{merchant_map_path: {tmp_path / 'merchant_map.json'}, rules_path: null}}
        {extra}
    """))
    return p


@pytest.fixture()
def fake_tg(monkeypatch):
    sent = []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "777")

    def fake(token, method, payload):
        if method == "getUpdates":
            return {"ok": True, "result": []}
        sent.append(payload)
        return {"ok": True, "result": {"message_id": 1}}

    monkeypatch.setattr(telegram, "_post_telegram", fake)
    return sent


def test_db_cli_init(tmp_path):
    assert db.main(["--init", "--config", str(_cfg(tmp_path))]) == 0
    conn = db.connect(tmp_path / "ledger.db")
    assert db.schema_version(conn) == 5
    conn.close()


def test_notify_main_push_and_plan_and_digest(tmp_path, fake_tg):
    cfg = _cfg(tmp_path)
    for flag in ("--push", "--plan", "--digest"):
        assert notify.main([flag, "--as-of", "2026-07-14", "--config", str(cfg)]) == 0
    assert len(fake_tg) == 3  # each scheduled push sent exactly one message


def test_notify_main_poll_dry_run_is_silent(tmp_path, fake_tg):
    assert notify.main(["--poll", "--dry-run", "--as-of", "2026-07-14", "--config", str(_cfg(tmp_path))]) == 0
    assert fake_tg == []  # dry-run poll sends nothing


def test_notify_main_returns_2_when_unconfigured(tmp_path, monkeypatch):
    monkeypatch.setattr(notify, "load_dotenv", lambda *a, **k: None)  # ignore the repo's .env
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    # a push with no telegram creds → NotifyError → exit 2
    assert notify.main(["--push", "--as-of", "2026-07-14", "--config", str(_cfg(tmp_path))]) == 2


def test_controller_cli_logs_the_number(tmp_path):
    db.main(["--init", "--config", str(_cfg(tmp_path))])
    assert controller.main(["--as-of", "2026-07-14", "--config", str(_cfg(tmp_path))]) == 0


def test_authorize_dry_run_previews_without_writing(tmp_path, monkeypatch):
    monkeypatch.setattr(authorize, "get_aspsps",
                        lambda *a, **k: [{"name": "AIB", "country": "IE"}])
    monkeypatch.setenv("ENABLE_BANKING_APP_ID", "app")
    key = tmp_path / "k.pem"
    key.write_text("x")
    monkeypatch.setenv("ENABLE_BANKING_PRIVATE_KEY_PATH", str(key))
    cfg = _cfg(tmp_path, extra="enable_banking: {redirect_url: 'https://localhost/cb', aspsp_country: IE}")
    assert authorize.main(["--dry-run", "--config", str(cfg)]) == 0
    # dry-run must not write account uids to state
    conn = db.connect(tmp_path / "ledger.db")
    db.init_db(conn)
    assert db.get_state(conn, "eb_account_uids") is None
    conn.close()
