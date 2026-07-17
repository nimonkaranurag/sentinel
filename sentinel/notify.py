"""
Telegram orchestration and CLI (SPEC §4/§7): the cron entry point.

Assembles the transport (telegram), text (render), policy alerts (alerts), and
bot commands (commands), and owns the scheduled actions: the daily safe-to-spend
push, the Monday plan, the Sunday digest, and `run_poll` (ingest, categorize,
alert). Each scheduled push is idempotent per period, so a re-run cron cannot
double-send.

CLI: python -m sentinel.notify [--push] [--updates] [--listen] [--digest]
                               [--poll] [--plan] [--as-of DATE] [--dry-run]
                               [--db] [--config]
     (no flags = --push --updates, one pass)
Exit codes: 0 ok · 2 telegram not configured
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import date, datetime, timedelta

from dotenv import load_dotenv

from . import alerts, categorize, commands, db, ingest, render, state_keys, telegram

log = logging.getLogger(__name__)


# ── Daily push ──────────────────────────────────────────────────────────────


def push_daily(conn, cfg: dict, as_of: date | None = None,
               dry_run: bool = False, force: bool = False) -> bool:
    """
    Send the daily push (safe-to-spend and traffic light). Idempotent per day.

    Per-category cap and leak alerts are handled by the poll (alerts.py), not
    here.
    """
    as_of = as_of or datetime.now(db.TZ).date()
    sent_key = state_keys.daily_push_sent(as_of.isoformat())
    if not force and db.get_state(conn, sent_key) is not None:
        log.info("daily push already sent for %s — skipping", as_of)
        return False
    text = render.compose_daily(conn, cfg, as_of)
    if dry_run:
        log.info("dry-run push: %s", text)
        return True
    telegram.send_message(text)
    db.set_state(conn, sent_key, db.now_iso())
    conn.commit()
    log.info("pushed: %s", text)
    return True


# ── Poll (ingest → categorize → alert) ──────────────────────────────────────


def run_poll(conn, cfg: dict, as_of: date | None = None, dry_run: bool = False) -> int:
    """
    Run one cron poll: ingest new rows (unattended, consuming the daily
    allowance), categorize, then fire policy alerts on what just booked.

    Fully read-only on --dry-run: no ingest, categorization is rolled back,
    nothing is sent, and the alert watermark does not advance.
    """
    as_of = as_of or datetime.now(db.TZ).date()
    if not dry_run:
        alerts.ensure_baseline(conn)  # before ingest, so this poll's rows still alert
        app_id = os.environ.get("ENABLE_BANKING_APP_ID")
        key_path = os.environ.get("ENABLE_BANKING_PRIVATE_KEY_PATH")
        uids_raw = db.get_state(conn, state_keys.EB_ACCOUNT_UIDS)
        if app_id and key_path and uids_raw:
            eb = cfg.get("enable_banking") or {}
            if ingest.check_and_consume_allowance(conn, int(eb.get("api_daily_call_limit", 4))):
                try:
                    client = ingest.build_client(cfg, app_id, key_path)
                    default_from = (as_of - timedelta(days=int(eb.get("first_pull_days", 90)))).isoformat()
                    ingest.run_ingest(conn, client, json.loads(uids_raw), default_from=default_from,
                                      cursor_overlap_days=int(eb.get("cursor_overlap_days", 5)),
                                      currency=cfg.get("currency", "EUR"))
                except Exception as exc:
                    log.warning("poll ingest failed: %s", exc)
    categorize.run(conn, cfg, dry_run=dry_run, as_of=as_of)  # forward dry_run
    return alerts.poll_alerts(conn, cfg, as_of, dry_run=dry_run)


# ── Weekly plan (Monday) ────────────────────────────────────────────────────


def push_weekly_plan(conn, cfg: dict, as_of: date | None = None, dry_run: bool = False) -> bool:
    """
    Send the Monday plan push. Idempotent per ISO week, so a re-run cron does
    not double-send.
    """
    as_of = as_of or datetime.now(db.TZ).date()
    iso_year, iso_week, _ = as_of.isocalendar()
    sent_key = state_keys.plan_sent(iso_year, iso_week)
    if not dry_run and db.get_state(conn, sent_key) is not None:
        log.info("weekly plan already sent for %s — skipping", sent_key)
        return False
    text = render.compose_weekly_plan(conn, cfg, as_of)
    if dry_run:
        log.info("dry-run plan: %s", text)
        return True
    telegram.send_message(text)
    db.set_state(conn, sent_key, db.now_iso())
    conn.commit()
    log.info("plan pushed: %s", text)
    return True


# ── Weekly digest (Sunday) ──────────────────────────────────────────────────


def run_digest(conn, cfg: dict, as_of: date | None = None, dry_run: bool = False) -> str | None:
    as_of = as_of or datetime.now(db.TZ).date()
    iso_year, iso_week, _ = as_of.isocalendar()
    sent_key = state_keys.digest_sent(iso_year, iso_week)
    if not dry_run and db.get_state(conn, sent_key) is not None:
        log.info("weekly digest already sent for %s — skipping", sent_key)
        return None
    text = render.digest_text(conn, cfg, as_of)
    if dry_run:
        log.info("dry-run digest:\n%s", text)
        return None
    telegram.send_message(text)
    db.set_state(conn, sent_key, db.now_iso())
    conn.commit()
    log.info("digest sent (%d chars)", len(text))
    return text


# ── CLI ──────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Telegram pushes + commands (SPEC §7).")
    parser.add_argument("--push", action="store_true", help="send the daily safe-to-spend push")
    parser.add_argument("--updates", action="store_true", help="answer pending commands once")
    parser.add_argument("--listen", action="store_true", help="long-poll for commands")
    parser.add_argument("--digest", action="store_true", help="send the weekly digest (deterministic template)")
    parser.add_argument("--poll", action="store_true", help="ingest + categorize + policy alerts (cron)")
    parser.add_argument("--plan", action="store_true", help="send the Monday weekly-plan push")
    parser.add_argument("--as-of", default=None, metavar="ISO_DATE")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db", default=None)
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    load_dotenv()
    cfg = db.load_config(args.config)
    conn = db.connect(args.db or cfg.get("db_path", "ledger.db"))
    try:
        db.init_db(conn)
        as_of = date.fromisoformat(args.as_of) if args.as_of else None
        if not any((args.push, args.updates, args.listen, args.digest, args.poll, args.plan)):
            args.push = args.updates = True
        if args.poll:
            run_poll(conn, cfg, as_of=as_of, dry_run=args.dry_run)
        if args.push:
            push_daily(conn, cfg, as_of=as_of, dry_run=args.dry_run)
        if args.plan:
            push_weekly_plan(conn, cfg, as_of=as_of, dry_run=args.dry_run)
        if args.digest:
            run_digest(conn, cfg, as_of=as_of, dry_run=args.dry_run)
        if args.listen or args.updates:
            commands.process_updates(conn, cfg, as_of=as_of, listen=args.listen, dry_run=args.dry_run)
        return 0
    except telegram.NotifyError as exc:
        log.error("%s", exc)
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
