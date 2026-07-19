"""
Telegram Bot API transport: the single HTTP seam (SPEC §7).

Raw HTTPS to the Bot API, with no third-party client. Every call to Telegram
(pushes, alerts, command replies, and the consent-expiry warning) goes through
`_post_telegram`, so tests monkeypatch a single function and the bot token is
redacted on every error path.

`_post_telegram` also parses JSON defensively: an HTML error page from a proxy or
CDN raises a `NotifyError` rather than a `JSONDecodeError` that would escape
callers.
"""

from __future__ import annotations

import logging
import os
import socket
from typing import Any

import requests
from requests.adapters import HTTPAdapter

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
MAX_MESSAGE_CHARS = 4000

# ── Transport timeouts (seconds) ────────────────────────────────────────────
# Split by call shape. getUpdates is a long-poll: its server-side wait lives in
# the request payload ('timeout'), and the HTTP read must outlast that wait by a
# margin. EVERY OTHER call (sendMessage, editMessageText, answerCallbackQuery,
# setMyCommands) returns promptly and must NOT inherit the long-poll's patience —
# the old single fixed 65s meant a stuck reply could wedge the listener for over a
# minute. Connect is generous (the network to Telegram is ~50ms) yet still fails
# fast on a black-holed route.
CONNECT_TIMEOUT_SECONDS = 10
SHORT_READ_TIMEOUT_SECONDS = 20
LONG_POLL_READ_MARGIN_SECONDS = 10

# TCP keepalive on the pooled socket: this VPS route silently drops the getUpdates
# long-poll (RST / read-timeout ~17x/day in prod). Keepalive lets the kernel notice
# a dead peer in tens of seconds instead of only when the read timeout fires, and
# catches half-open sockets a read timeout alone would miss. The idle/interval/count
# knobs are Linux / newer-macOS only, so probe for each before setting it.
_KEEPALIVE_SOCKET_OPTIONS: list[tuple[int, int, int]] = [
    (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),
    (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
]
for _opt_name, _opt_value in (("TCP_KEEPIDLE", 15), ("TCP_KEEPINTVL", 5), ("TCP_KEEPCNT", 3)):
    _opt = getattr(socket, _opt_name, None)
    if _opt is not None:
        _KEEPALIVE_SOCKET_OPTIONS.append((socket.IPPROTO_TCP, _opt, _opt_value))


class _KeepAliveAdapter(HTTPAdapter):
    """HTTPAdapter that enables TCP keepalive (+ NODELAY) on every pooled socket."""

    def init_poolmanager(self, connections: int, maxsize: int, block: bool = False, **pool_kwargs: Any) -> None:
        pool_kwargs["socket_options"] = _KEEPALIVE_SOCKET_OPTIONS
        super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)


_session: requests.Session | None = None


def _http() -> requests.Session:
    """
    Return the process-wide keep-alive session (one pooled TLS connection reused
    across getUpdates and every reply), created lazily.

    Reuse means a reply no longer pays a fresh DNS+TCP+TLS handshake, and the
    pooled socket carries TCP keepalive so a dropped long-poll is noticed quickly.
    Adapter retries are off: the --listen loop owns retry/backoff (per-update,
    jittered), so urllib3 must not also retry underneath it.
    """
    global _session
    if _session is None:
        session = requests.Session()
        adapter = _KeepAliveAdapter(max_retries=0)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        _session = session
    return _session


def _timeout_for(method: str, payload: dict[str, Any]) -> tuple[int, int]:
    """
    (connect, read) timeout for one Bot API call. getUpdates reads for its
    server-side poll wait (payload['timeout']) plus a margin; every other method
    uses the short read budget, so a stuck call fails fast instead of wedging.
    """
    if method == "getUpdates":
        return CONNECT_TIMEOUT_SECONDS, int(payload.get("timeout", 0)) + LONG_POLL_READ_MARGIN_SECONDS
    return CONNECT_TIMEOUT_SECONDS, SHORT_READ_TIMEOUT_SECONDS


class NotifyError(RuntimeError):
    """
    A Telegram send or transport failure (token already redacted).
    """


def credentials() -> tuple[str, str]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise NotifyError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set (.env, SPEC §7)")
    return token, chat_id


def owner_id() -> str:
    """
    The user id authorized to issue commands and taps (the sender's `from.id`).

    Defaults to TELEGRAM_CHAT_ID — in a 1:1 chat the sender and the chat coincide
    — but is overridable via TELEGRAM_OWNER_ID so the bot can *deliver* into a
    group (negative chat id) while only the owner keeps *authority* over it.
    """
    _, chat_id = credentials()
    return os.environ.get("TELEGRAM_OWNER_ID") or chat_id


def redact(text: str, token: str) -> str:
    """
    Replace the bot token with a placeholder in text bound for logs or
    tracebacks; `requests` includes the full URL, token and all, in its
    exceptions.
    """
    return text.replace(token, "***") if token else text


def _post_telegram(token: str, method: str, payload: dict[str, Any]) -> dict[str, Any]:
    """
    Send one request to the Bot API. This is the single HTTP seam that tests
    monkeypatch.

    Raises NotifyError, with the token redacted, on any transport failure,
    non-JSON reply, or non-ok response.
    """
    try:
        resp = _http().post(f"{TELEGRAM_API}/bot{token}/{method}", json=payload, timeout=_timeout_for(method, payload))
    except requests.RequestException as exc:
        raise NotifyError(f"telegram {method} transport error: {redact(str(exc), token)}") from None
    try:
        body = resp.json()
    except ValueError:  # a non-JSON body (HTML error page) is not a RequestException
        raise NotifyError(redact(f"telegram {method}: non-JSON reply (HTTP {resp.status_code})", token)) from None
    if resp.status_code != 200 or not body.get("ok"):
        raise NotifyError(redact(f"telegram {method} failed: {resp.status_code} {str(body)[:300]}", token))
    return body


def post(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    """
    Public transport seam: send one Bot API request with the configured token and
    return the parsed body.

    Sibling modules (e.g. the alert engine) call this rather than reaching into
    the underscore-private `_post_telegram`, which stays the token-taking
    primitive tests monkeypatch.
    """
    token, _ = credentials()
    return _post_telegram(token, method, payload)


def send_message(text: str, parse_mode: str | None = None, reply_markup: dict[str, Any] | None = None) -> None:
    token, chat_id = credentials()
    chunks = [text[i : i + MAX_MESSAGE_CHARS] for i in range(0, len(text), MAX_MESSAGE_CHARS)] or [""]
    for i, chunk in enumerate(chunks):
        payload: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        # The keyboard attaches to the last chunk only (Telegram shows one per message).
        if reply_markup is not None and i == len(chunks) - 1:
            payload["reply_markup"] = reply_markup
        _post_telegram(token, "sendMessage", payload)


def edit_message(
    message_id: Any, text: str, reply_markup: dict[str, Any] | None = None, parse_mode: str | None = None
) -> None:
    token, chat_id = credentials()
    payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    _post_telegram(token, "editMessageText", payload)


def answer_callback(callback_id: str, text: str = "") -> None:
    token, _ = credentials()
    _post_telegram(token, "answerCallbackQuery", {"callback_query_id": callback_id, "text": text})


def set_my_commands(commands: list[dict[str, str]]) -> None:
    """
    Register the bot's command menu (the '/' autocomplete list) via setMyCommands.

    `commands` is a list of {"command", "description"} dicts; Telegram requires
    each command name to be 1–32 chars of [a-z0-9_]. Called once when the listener
    starts so the menu tracks the deployed command set.
    """
    post("setMyCommands", {"commands": commands})


def get_updates(offset: int, timeout: int) -> list[dict[str, Any]]:
    """
    Call getUpdates and return the raw update list, unwrapped from the JSON
    envelope.
    """
    token, _ = credentials()
    body = _post_telegram(token, "getUpdates", {"offset": offset, "timeout": timeout})
    return body.get("result", [])
