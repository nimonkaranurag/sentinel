"""
Telegram command router and inline-keyboard callback state machine (SPEC §4/§7).

Owner-only: every update is authorized by the sender's id (`from.id`), not the
chat id, so pointing the bot at a group does not let other members drive the
ledger. The long-poll listener survives transient errors (per-update try/except
and jittered backoff) and commits its offset per update, so a mid-batch failure
does not replay earlier commands.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import alerts, bills, categorize, db, ingest, render, state_keys, telegram

log = logging.getLogger(__name__)


# ── Transaction / command handlers ──────────────────────────────────────────


def _resolve_txn(conn, ref: str):
    ref = ref.strip()
    if len(ref) < 6:
        return None, "Ref too short — use at least 6 characters from /cat."
    # Exact prefix match via substr, NOT LIKE: '%'/'_' in a pasted ref are live
    # LIKE wildcards, so `LIKE ref||'%'` is a foot-gun. Case-sensitive is correct
    # — /cat shows the id verbatim, so the ref is copied verbatim.
    rows = conn.execute(
        "SELECT * FROM transactions WHERE substr(id, 1, length(?)) = ? LIMIT 2",
        (ref, ref),
    ).fetchall()
    if not rows:
        return None, f"No transaction matches ref {ref!r}."
    if len(rows) > 1:
        return None, f"Ref {ref!r} is ambiguous — give more characters."
    return rows[0], None


def do_recat(conn, cfg: dict[str, Any], ref: str, category_name: str) -> str:
    category = render.resolve_category(category_name)
    if category is None:
        return f"Unknown category {category_name!r}. Valid: {', '.join(categorize.TAXONOMY)}"
    txn, err = _resolve_txn(conn, ref)
    if err:
        return err
    conn.execute("UPDATE transactions SET category_override = ? WHERE id = ?",
                 (category, txn["id"]))
    merchant_note = ""
    if txn["merchant_id"] is not None:
        merchant = conn.execute("SELECT name_normalized FROM merchants WHERE id = ?",
                                (txn["merchant_id"],)).fetchone()
        conn.execute("UPDATE merchants SET category = ?, categorized_by = 'manual', "
                     "confidence = 1.0 WHERE id = ?", (category, txn["merchant_id"]))
        map_path = Path((cfg.get("categorize") or {}).get("merchant_map_path")
                        or categorize.DEFAULT_MERCHANT_MAP_PATH)
        mapping = categorize.load_merchant_map(map_path)
        mapping[merchant["name_normalized"]] = {"category": category, "by": "manual",
                                                "confidence": 1.0}
        categorize.save_merchant_map(map_path, mapping)
        merchant_note = f"; merchant '{merchant['name_normalized']}' is now always {category}"
    conn.commit()
    return f"✅ {txn['id'][:8]} → {category}{merchant_note}"


def do_date(conn, ref: str) -> str:
    txn, err = _resolve_txn(conn, ref)
    if err:
        return err
    conn.execute("UPDATE transactions SET category_override = 'Dates' WHERE id = ?",
                 (txn["id"],))
    conn.commit()
    return (f"✅ {txn['id'][:8]} → Dates (just this one — "
            f"'{txn['merchant_raw'] or 'merchant'}' keeps its usual category)")


def do_sync(conn, cfg: dict[str, Any]) -> str:
    app_id = os.environ.get("ENABLE_BANKING_APP_ID")
    key_path = os.environ.get("ENABLE_BANKING_PRIVATE_KEY_PATH")
    uids_raw = db.get_state(conn, state_keys.EB_ACCOUNT_UIDS)
    if not app_id or not key_path or not uids_raw:
        return "Enable Banking not configured yet — finish the Phase 0 runbook (SPEC §6)."
    eb_cfg = cfg.get("enable_banking") or {}
    try:
        # /sync is owner-initiated: send PSU-present headers → attended access,
        # exempt from the ~4/day UNATTENDED allowance the crons consume (PSD2 RTS
        # Art. 36(5)). The PSU IP is this host's real LAN IP, not a fiction.
        psu_ip = eb_cfg.get("psu_ip_address") or ingest.local_ip()
        psu_headers = {"Psu-Ip-Address": str(psu_ip),
                       "Psu-User-Agent": str(eb_cfg.get("psu_user_agent") or "Sentinel/1.0")}
        client = ingest.build_client(cfg, app_id, key_path)
        default_from = (datetime.now(db.TZ).date()
                        - timedelta(days=int(eb_cfg.get("first_pull_days", 90)))).isoformat()
        alerts.ensure_baseline(conn)  # before ingest, so this pull's rows still alert
        inserted, submitted = ingest.run_ingest(
            conn, client, json.loads(uids_raw), default_from=default_from,
            psu_headers=psu_headers, cursor_overlap_days=int(eb_cfg.get("cursor_overlap_days", 5)),
            currency=cfg.get("currency", "EUR"))
        # Attended ingest must also categorize + alert — else a charge that
        # arrives via /sync, precisely when you're watching, can never alert.
        today = datetime.now(db.TZ).date()
        categorize.run(conn, cfg)
        fired = alerts.poll_alerts(conn, cfg, today)
        fired += bills.send_alerts(conn, cfg, today)  # late/drift bills fire here too
        return f"Synced: {inserted} new / {submitted} fetched (attended); {fired} alert(s)."
    except Exception as exc:  # bank errors must never crash the bot loop
        log.warning("/sync failed: %s", exc)
        return f"Sync failed: {exc}"


def handle_command(conn, cfg: dict[str, Any], text: str, as_of: date) -> str:
    parts = text.strip().split()
    if not parts:
        return render.HELP_TEXT
    cmd, args = parts[0].lower(), parts[1:]
    if cmd == "/today":
        return render.compose_daily(conn, cfg, as_of)
    if cmd == "/status":
        return render.status_text(conn, cfg, as_of)
    if cmd == "/cat":
        return render.cat_text(conn, cfg, " ".join(args), as_of) if args else "Usage: /cat <name>"
    if cmd == "/sync":
        return do_sync(conn, cfg)
    if cmd == "/recat":
        return do_recat(conn, cfg, args[0], " ".join(args[1:])) if len(args) >= 2 \
            else "Usage: /recat <ref> <category>"
    if cmd == "/date":
        return do_date(conn, args[0]) if args else "Usage: /date <ref>"
    return render.HELP_TEXT


# ── Inline-keyboard callbacks ────────────────────────────────────────────────


def handle_callback(conn, cfg: dict[str, Any], callback: dict[str, Any]) -> None:
    """
    Process one inline-keyboard tap, editing the alert message in place.

    Idempotent but not exactly-once: the dedupe marker is inserted after the
    effects, so a crash between them replays, but every effect (message edit,
    events status, do_recat) is itself idempotent, so the replay is harmless.
    Callback data carries the exact transaction ref.
    """
    cb_id = str(callback.get("id"))
    if conn.execute("SELECT 1 FROM processed_callbacks WHERE callback_id = ?", (cb_id,)).fetchone():
        telegram.answer_callback(cb_id)  # a retried tap must edit nothing twice
        return
    message_id = (callback.get("message") or {}).get("message_id")
    action, _, rest = (callback.get("data") or "").partition(":")
    if action == "ok":
        telegram.edit_message(message_id, "✓ noted.")
        conn.execute("UPDATE events SET status = 'fine' WHERE message_id = ?", (message_id,))
    elif action == "rc":
        telegram.edit_message(message_id, "Pick a category:", render.reclass_keyboard(rest))
    elif action == "set":
        ref, _, category = rest.partition(":")
        reply = do_recat(conn, cfg, ref, category)
        telegram.edit_message(message_id, f"Got it — {category}. {reply}")
        conn.execute("UPDATE events SET status = 'reclassified' WHERE message_id = ?", (message_id,))
    conn.execute("INSERT INTO processed_callbacks (callback_id, processed_at) VALUES (?, ?)",
                 (cb_id, db.now_iso()))
    conn.commit()
    telegram.answer_callback(cb_id)


def _handle_update(conn, cfg: dict[str, Any], update: dict[str, Any], owner_id: str,
                   as_of: date, dry_run: bool) -> bool:
    """
    Dispatch one update. Returns True if it was an owner action that was handled.

    Authorization is the sender's `from.id` against the owner id (not the chat
    id), so the bot can be delivered into a group while only the owner drives it.
    """
    callback = update.get("callback_query")
    if callback:
        sender = str((callback.get("from") or {}).get("id", ""))  # WHO tapped
        if sender != str(owner_id):
            log.warning("ignoring callback from non-owner sender %s", sender or "?")
            return False
        if not dry_run:
            handle_callback(conn, cfg, callback)
        return True
    message = update.get("message") or {}
    text = message.get("text") or ""
    sender = str((message.get("from") or {}).get("id", ""))  # WHO sent
    if sender != str(owner_id):
        log.warning("ignoring message from non-owner sender %s", sender or "?")
        return False
    if not text.startswith("/"):
        return False
    reply = handle_command(conn, cfg, text, as_of)
    if dry_run:
        log.info("dry-run reply to %r: %s", text, reply)
    else:
        telegram.send_message(reply)
    return True


def process_updates(conn, cfg: dict[str, Any], as_of: date | None = None,
                    listen: bool = False, dry_run: bool = False) -> int:
    """
    Answer pending commands: one pass by default, long-polling when listen is
    set.

    In listen mode a transient getUpdates error backs off and retries rather
    than killing the loop; every update advances the offset even if its handler
    throws, so one poison update can neither replay the batch nor crash-loop the
    listener. Under --dry-run the offset is tracked in memory only (never
    persisted), so listen+dry-run cannot busy-spin re-reading the same batch.
    """
    owner = telegram.owner_id()
    as_of = as_of or datetime.now(db.TZ).date()
    tg_cfg = cfg.get("telegram") or {}
    timeout = int(tg_cfg.get("poll_timeout_seconds", 50)) if listen else 0
    backoff = float(tg_cfg.get("listen_backoff_seconds", 3))
    handled = 0
    local_offset: int | None = None  # dry-run cursor, advanced in memory
    while True:
        if dry_run and local_offset is not None:
            offset = local_offset
        else:
            offset = int(db.get_state(conn, state_keys.TG_UPDATE_OFFSET, "0") or "0")
        try:
            updates = telegram.get_updates(offset, timeout)
        except telegram.NotifyError as exc:
            if not listen:
                raise
            delay = backoff * (1 + random.random() * 0.5)
            log.warning("getUpdates failed; backing off %.1fs then retrying: %s", delay, exc)
            time.sleep(delay)
            continue
        for update in updates:
            uid = update.get("update_id")
            if uid is None:
                log.warning("skipping update with no update_id: %r", update)
                continue
            next_offset = int(uid) + 1
            try:
                if _handle_update(conn, cfg, update, owner, as_of, dry_run):
                    handled += 1
            except telegram.NotifyError as exc:
                # A reply/edit failed to send: log and advance past it below.
                log.warning("update %s: handler send failed: %s", uid, exc)
            except Exception as exc:
                # Any other handler fault (a WAL lock, a malformed payload) must
                # not kill the loop or wedge on one poison update: discard its
                # partial work, log, and advance the offset past it.
                log.warning("update %s: handler error, skipping: %s", uid, exc)
                if not dry_run:
                    conn.rollback()
            if dry_run:
                local_offset = next_offset
            else:
                db.set_state(conn, state_keys.TG_UPDATE_OFFSET, str(next_offset))
                conn.commit()
        if not listen:
            return handled
