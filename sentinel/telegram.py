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
from typing import Any

import requests

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
HTTP_TIMEOUT = 65  # > the long-poll timeout
MAX_MESSAGE_CHARS = 4000


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
        resp = requests.post(f"{TELEGRAM_API}/bot{token}/{method}", json=payload,
                             timeout=HTTP_TIMEOUT)
    except requests.RequestException as exc:
        raise NotifyError(f"telegram {method} transport error: {redact(str(exc), token)}") from None
    try:
        body = resp.json()
    except ValueError:  # a non-JSON body (HTML error page) is not a RequestException
        raise NotifyError(
            redact(f"telegram {method}: non-JSON reply (HTTP {resp.status_code})", token)
        ) from None
    if resp.status_code != 200 or not body.get("ok"):
        raise NotifyError(redact(f"telegram {method} failed: {resp.status_code} {str(body)[:300]}", token))
    return body


def send_message(text: str) -> None:
    token, chat_id = credentials()
    for start in range(0, len(text), MAX_MESSAGE_CHARS):
        _post_telegram(token, "sendMessage",
                       {"chat_id": chat_id, "text": text[start:start + MAX_MESSAGE_CHARS]})


def edit_message(message_id: Any, text: str, reply_markup: dict[str, Any] | None = None) -> None:
    token, chat_id = credentials()
    payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    _post_telegram(token, "editMessageText", payload)


def answer_callback(callback_id: str, text: str = "") -> None:
    token, _ = credentials()
    _post_telegram(token, "answerCallbackQuery",
                   {"callback_query_id": callback_id, "text": text})


def get_updates(offset: int, timeout: int) -> list[dict[str, Any]]:
    """
    Call getUpdates and return the raw update list, unwrapped from the JSON
    envelope.
    """
    token, _ = credentials()
    body = _post_telegram(token, "getUpdates", {"offset": offset, "timeout": timeout})
    return body.get("result", [])
