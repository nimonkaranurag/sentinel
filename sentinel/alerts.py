"""
Policy-alert engine: evaluate newly-booked rows, send, record, and remember.

The watermark marking how far alerting has progressed lives in the `state`
table, not in memory, so a crash between ingest and send replays on the next
poll instead of dropping the batch's alerts. The cursor is rowid (monotonic
insertion order), which avoids the second-granularity ties an inserted_at cursor
would carry.

Sending, recording, and advancing the watermark all happen only after a send
succeeds, and a per-transaction `events` guard makes a replay idempotent.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from . import db, policies, render, state_keys, telegram

log = logging.getLogger(__name__)


def ensure_baseline(conn) -> None:
    """
    On the first poll or sync, set the watermark to the current tip so the
    historical backfill does not alert all at once.

    Call before ingesting, so the rows this run adds (rowid greater than the tip)
    still alert.
    """
    if db.get_state(conn, state_keys.ALERTS_CHECKED_THROUGH) is None:
        tip = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM transactions").fetchone()[0]
        db.set_state(conn, state_keys.ALERTS_CHECKED_THROUGH, str(tip))
        conn.commit()


def send_policy_alert(conn, alert: dict[str, Any]) -> int | None:
    """
    Send one alert with the inline keyboard and record it in `events` for the
    audit trail and in-place edits.
    """
    token, chat_id = telegram.credentials()
    body = telegram._post_telegram(token, "sendMessage",
                                   {"chat_id": chat_id, "text": alert["text"],
                                    "reply_markup": render.alert_keyboard(alert["txn_id"])})
    message_id = (body.get("result") or {}).get("message_id")
    conn.execute("INSERT INTO events (kind, txn_id, message_id, status, detail, created_at) "
                 "VALUES ('policy_alert', ?, ?, 'sent', ?, ?)",
                 (alert["txn_id"], message_id, alert["policy"], db.now_iso()))
    conn.commit()
    return message_id


def poll_alerts(conn, cfg: dict[str, Any], as_of: date, dry_run: bool = False) -> int:
    """
    Evaluate policies over rows past the durable watermark and alert on each
    breach.

    Idempotent: a transaction that already has a 'policy_alert' event is skipped,
    so a replay after a crash re-sends only what never went out. The watermark
    advances and commits only after the batch is processed.
    """
    watermark = int(db.get_state(conn, state_keys.ALERTS_CHECKED_THROUGH, "0") or "0")
    rows = conn.execute(
        "SELECT rowid AS rid, id FROM transactions WHERE rowid > ? ORDER BY rowid",
        (watermark,),
    ).fetchall()
    if not rows:
        return 0
    high = max(r["rid"] for r in rows)
    alerts = policies.evaluate(conn, cfg, as_of, [r["id"] for r in rows])
    sent = 0
    for a in alerts:
        already = conn.execute(
            "SELECT 1 FROM events WHERE kind = 'policy_alert' AND txn_id = ?", (a["txn_id"],)
        ).fetchone()
        if already:
            continue
        if dry_run:
            log.info("dry-run alert: %s", a["text"])
        else:
            send_policy_alert(conn, a)
        sent += 1
    if not dry_run:
        db.set_state(conn, state_keys.ALERTS_CHECKED_THROUGH, str(high))
        conn.commit()
    log.info("poll_alerts: %d row(s) past watermark, %d alert(s)%s",
             len(rows), sent, " (dry-run)" if dry_run else "")
    return sent
