"""
Enable Banking to ledger ingest (SPEC §2).

Pulls transactions since the per-account cursor, applies INSERT OR IGNORE, and
advances the cursor. Warns to Telegram when consent expiry is within N days
(SPEC §6: AIB consent lasts at most 180 days).

Prerequisites (Phase 0 runbook, SPEC §6; the owner performs this once, manually):
  .env    ENABLE_BANKING_APP_ID, ENABLE_BANKING_PRIVATE_KEY_PATH
  state   eb_account_uids  — JSON list of account uids to pull
          consent_expiry   — ISO date the consent expires (warned at T-14d)

PSD2 allows roughly 4 unattended calls per day. Each run consumes one unit of the
daily counter in `state`, checked before any network I/O and committed before
fetching, so a run counts even if it crashes afterward, and a --dry-run run
counts as well. Attended /sync uses PSU-present headers and is exempt from this
counter (see commands.py).

The cursor is re-pulled a few days behind its high-water mark
(cursor_overlap_days) because banks backdate bookings; INSERT OR IGNORE makes the
overlap free. Without it, a backdated row would fall outside every future window
and be lost.

No bank credentials are stored: auth is a short-lived JWT signed locally with the
app private key (path from .env; the key is never read into the DB or logs). The
JWT is signed via the `openssl` CLI, so no crypto package is required.

CLI: python -m sentinel.ingest [--dry-run] [--from ISO_DATE] [--db PATH] [--config PATH]
     (ingest only, no alerts. The cron path is `make poll`, which also
      categorizes and fires policy alerts on what just booked.)
Exit codes: 0 ok · 2 Phase 0 incomplete · 3 daily API allowance exhausted
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import random
import socket
import subprocess
import time
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from . import db, state_keys, telegram

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.enablebanking.com"
JWT_TTL_SECONDS = 3600
HTTP_TIMEOUT = 30

BOOKED_STATUSES = ("BOOK", "BOOKED")  # pending rows have no stable identity yet


# ── Auth (JWT via openssl — no crypto dependency) ─────────────────────────


def _b64url(raw: bytes) -> bytes:
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


def make_jwt(app_id: str, private_key_path: str, ttl: int = JWT_TTL_SECONDS) -> str:
    """
    Build an RS256 JWT per the Enable Banking auth docs, signed with the app key.
    """
    header = {"typ": "JWT", "alg": "RS256", "kid": app_id}
    now = int(time.time())
    payload = {"iss": "enablebanking.com", "aud": "api.enablebanking.com", "iat": now, "exp": now + ttl}
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + b"."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode())
    )
    result = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", private_key_path],
        input=signing_input,
        capture_output=True,
        check=True,
    )
    return (signing_input + b"." + _b64url(result.stdout)).decode()


def local_ip() -> str:
    """
    Return this host's LAN egress IP, used as the PSU-Ip-Address for attended
    /sync.

    No packets are sent; connect() on a UDP socket only selects the route. Falls
    back to loopback when there is no network.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return str(s.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class EnableBankingClient:
    def __init__(
        self,
        app_id: str,
        private_key_path: str,
        base_url: str = DEFAULT_BASE_URL,
        max_retries: int = 2,
        retry_backoff: float = 1.5,
        max_pages: int = 50,
    ):
        self.app_id = app_id
        self.private_key_path = private_key_path
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.max_pages = max_pages
        # Accounts whose last pull hit the page cap, so run_ingest can refuse to
        # advance their cursor past an unfetched window (SPEC §2 / §6).
        self.truncated_accounts: set[str] = set()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {make_jwt(self.app_id, self.private_key_path)}"}

    def _get_json(self, url: str, params: dict[str, str], psu_headers: dict[str, str] | None) -> dict[str, Any]:
        """
        GET with a bounded, jittered retry on *transient* failures only.

        The daily allowance unit is consumed before this call, so a connection
        error or a 5xx is retried rather than discarding the pull. A 4xx is
        deterministic — a bad request, or an expired/revoked consent (401/403) —
        so it is raised immediately: retrying it just burns three more attempts,
        four times a day, forever.
        """
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = requests.get(
                    url, params=params, headers={**self._headers(), **(psu_headers or {})}, timeout=HTTP_TIMEOUT
                )
            except requests.RequestException as exc:
                last_exc = exc  # no response at all (DNS/connect/timeout) — transient
            else:
                if resp.status_code < 400:
                    return resp.json()
                if resp.status_code < 500:
                    resp.raise_for_status()  # 4xx — fail fast, do not retry
                last_exc = requests.HTTPError(f"{resp.status_code} server error", response=resp)
            if attempt < self.max_retries:
                delay = self.retry_backoff * (2**attempt) * (1 + random.random() * 0.5)
                log.warning(
                    "bank GET failed (attempt %d/%d), retry in %.1fs: %s",
                    attempt + 1,
                    self.max_retries + 1,
                    delay,
                    last_exc,
                )
                time.sleep(delay)
        assert last_exc is not None
        raise last_exc

    def iter_transactions(
        self, account_uid: str, date_from: str, psu_headers: dict[str, str] | None = None
    ) -> Iterator[dict[str, Any]]:
        """
        Yield raw transaction dicts, following continuation_key pagination.

        psu_headers (Psu-Ip-Address, Psu-User-Agent) signal PSU-present, attended
        access, which the ASPSP exempts from the unattended limit. Pagination is
        bounded by max_pages so a misbehaving continuation_key cannot loop
        indefinitely; if the cap is hit the account is recorded in
        truncated_accounts so the cursor is not advanced past the unfetched rows.
        """
        self.truncated_accounts.discard(account_uid)
        url = f"{self.base_url}/accounts/{account_uid}/transactions"
        params: dict[str, str] = {"date_from": date_from}
        for _page in range(self.max_pages):
            body = self._get_json(url, params, psu_headers)
            yield from body.get("transactions", [])
            continuation = body.get("continuation_key")
            if not continuation:
                return
            params = {"date_from": date_from, "continuation_key": continuation}
        self.truncated_accounts.add(account_uid)
        log.warning(
            "account %s: hit max_pages=%d — stopping pagination (runaway continuation_key?)",
            account_uid,
            self.max_pages,
        )


# ── Mapping (pure) ────────────────────────────────────────────────────────


def map_api_transaction(txn: dict[str, Any], account_uid: str) -> dict[str, Any] | None:
    """
    Convert Enable Banking transaction JSON to a ledger row. Returns None to skip
    a legitimately non-bookable-yet row (pending, or no date); raises on an
    un-bookable row (sign-ambiguous) so the caller quarantines it.

    A pure local mapping. Amounts arrive as decimal strings and stay integer
    cents.
    """
    status = (txn.get("status") or "BOOK").upper()
    if status not in BOOKED_STATUSES:
        return None
    booking_date = txn.get("booking_date") or txn.get("value_date")
    if not booking_date:
        log.warning("skipping transaction without booking/value date: %s", txn.get("entry_reference"))
        return None

    amount_info = txn.get("transaction_amount") or {}
    cents = db.to_cents(amount_info.get("amount", ""))
    indicator = (txn.get("credit_debit_indicator") or "").upper()
    if indicator == "DBIT":
        cents = -abs(cents)
    elif indicator == "CRDT":
        cents = abs(cents)
    else:
        # No direction → the sign would be a guess; reject rather than book a
        # debit as income. Raise (not return None) so run_ingest quarantines it to
        # the table instead of silently skipping — a sign-ambiguous row is a
        # rejection, not a legitimately-skipped pending/undated row.
        raise ValueError("no credit_debit_indicator (sign unknown)")

    remittance = txn.get("remittance_information") or []
    if isinstance(remittance, str):
        remittance = [remittance]
    description = " ".join(part.strip() for part in remittance if part).strip() or None

    counterparty = txn.get("creditor") if cents < 0 else txn.get("debtor")
    merchant_raw = (counterparty or {}).get("name") or description

    return {
        "id": txn.get("entry_reference") or None,  # None → hash fallback in db layer
        "account_id": account_uid,
        "booking_date": booking_date,
        "value_date": txn.get("value_date"),
        "amount_cents": cents,
        "currency": (amount_info.get("currency") or "EUR").upper(),
        "merchant_raw": merchant_raw,
        "description": description,
        "source": "api",
    }


# ── Allowance / consent bookkeeping ───────────────────────────────────────


def check_and_consume_allowance(conn, limit: int) -> bool:
    """
    Consume one allowance unit per run. Committed immediately, so a crashed run
    still counts. Returns False when the daily limit is already reached.
    """
    today = datetime.now(db.TZ).date().isoformat()
    key = state_keys.api_calls(today)
    conn.commit()  # close any implicit txn before the atomic RMW
    conn.execute("BEGIN IMMEDIATE")  # serialize 4 crons + /sync + callback loop
    used = int(db.get_state(conn, key, "0") or "0")
    if used >= limit:
        conn.rollback()
        log.error("daily API allowance exhausted (%d/%d) — refusing to call the bank", used, limit)
        return False
    db.set_state(conn, key, str(used + 1))
    conn.commit()
    log.info("API allowance: %d/%d used today", used + 1, limit)
    return True


def warn_if_consent_expiring(conn, warn_days: int, dry_run: bool) -> None:
    """
    Warn the owner over Telegram when the AIB consent is within `warn_days` of
    expiry, and, once expired, that every poll now fails until re-auth.

    Idempotent per day via CONSENT_WARNED_ON. Called from run_poll (SPEC §6), so
    the nag actually fires on a schedule rather than only from a manual CLI run.
    """
    expiry_raw = db.get_state(conn, state_keys.CONSENT_EXPIRY)
    if not expiry_raw:
        log.info("no consent_expiry in state yet (Phase 0 pending?) — skipping expiry check")
        return
    today = datetime.now(db.TZ).date()
    days_left = (date.fromisoformat(expiry_raw[:10]) - today).days
    if days_left > warn_days:
        return
    if days_left < 0:
        message = (
            f"🔴 Sentinel: bank consent EXPIRED {-days_left} day(s) ago ({expiry_raw[:10]}). "
            "Polls fail until you re-auth via the Phase 0 runbook (~2 min)."
        )
    else:
        message = (
            f"⚠️ Sentinel: bank consent expires in {days_left} day(s) "
            f"({expiry_raw[:10]}). Re-auth via the Phase 0 runbook (~2 min)."
        )
    log.warning(message)
    if dry_run:
        return
    if db.get_state(conn, state_keys.CONSENT_WARNED_ON) == today.isoformat():
        return  # already nagged today
    try:
        telegram.send_message(message)  # ONE redacting sender — token never logged
    except telegram.NotifyError as exc:
        log.warning("consent warning not sent: %s", exc)
        return
    db.set_state(conn, state_keys.CONSENT_WARNED_ON, today.isoformat())
    conn.commit()


def warn_on_auth_error(conn, exc: Exception) -> None:
    """
    On a bank 401/403 (consent expired or revoked), tell the owner over Telegram,
    once per day.

    The T−14d nag (warn_if_consent_expiring) only fires while `consent_expiry` is
    known and near; a session revoked early, or a consent that lapses without the
    date being refreshed, surfaces only as a hard auth failure. This turns that
    failure into an actionable message instead of a silent 401 in an unread log.
    """
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status not in (401, 403):
        return
    today = datetime.now(db.TZ).date().isoformat()
    if db.get_state(conn, state_keys.CONSENT_ERROR_NOTIFIED_ON) == today:
        return
    try:
        telegram.send_message(
            f"🔴 Sentinel: the bank rejected the last pull (HTTP {status} — consent expired "
            "or revoked). Re-auth via the Phase 0 runbook (~2 min)."
        )
    except telegram.NotifyError as e:
        log.warning("consent-error notice not sent: %s", e)
        return
    db.set_state(conn, state_keys.CONSENT_ERROR_NOTIFIED_ON, today)
    conn.commit()


# ── Orchestration ─────────────────────────────────────────────────────────


def run_ingest(
    conn,
    client,
    account_uids: list[str],
    default_from: str,
    date_from: str | None = None,
    dry_run: bool = False,
    psu_headers: dict[str, str] | None = None,
    cursor_overlap_days: int = 0,
    currency: str = "EUR",
) -> tuple[int, int]:
    """
    Pull each account since its cursor; returns (inserted, submitted) totals.

    `client` need only provide .iter_transactions(uid, date_from), which tests
    replace with a fake. The cursor is re-pulled `cursor_overlap_days` behind its
    high-water mark so backdated bookings are not missed; ids are stable and
    INSERT OR IGNORE drops the re-pulled duplicates. Rows whose booked currency
    is not `currency` are quarantined, since a non-EUR amount summed at face
    value would corrupt every total.
    """
    total_inserted = 0
    total_submitted = 0
    for uid in account_uids:
        cursor_date = db.get_state(conn, state_keys.cursor(uid))
        first_pull = not date_from and not cursor_date
        if date_from:
            since = date_from
        elif cursor_date:
            since = (date.fromisoformat(cursor_date[:10]) - timedelta(days=cursor_overlap_days)).isoformat()
        else:
            since = default_from
        if first_pull and not dry_run:
            # The clip boundary the CSV backfill trusts is a fact about *coverage*,
            # not an aggregate over row dates: record where the API window begins,
            # so a single backdated booking can't drag it back and amputate the
            # backfill (compute_clip_before, SPEC §2).
            prior = db.get_state(conn, state_keys.API_COVERAGE_START)
            if prior is None or since < prior:
                db.set_state(conn, state_keys.API_COVERAGE_START, since)
        rows, rejected = [], 0
        extra = {"psu_headers": psu_headers} if psu_headers else {}
        for txn in client.iter_transactions(uid, since, **extra):
            try:
                mapped = map_api_transaction(txn, uid)
                if mapped and mapped["currency"] != currency:
                    raise ValueError(f"non-{currency} booked currency {mapped['currency']!r}")
            except Exception as exc:  # one poisoned row must not blind the whole poll
                rejected += 1
                db.quarantine_row(conn, "api", str(exc), txn, uid)
                log.warning("quarantined malformed txn %s: %s", txn.get("entry_reference"), exc)
                continue
            if mapped:
                rows.append(mapped)
        if rejected:
            log.warning("account %s: quarantined %d malformed row(s), kept the rest", uid, rejected)
        inserted, submitted = db.insert_transactions(conn, rows)
        total_inserted += inserted
        total_submitted += submitted
        log.info(
            "account %s: since %s, %d fetched, %d new, %d duplicate",
            uid,
            since,
            submitted,
            inserted,
            submitted - inserted,
        )
        truncated = uid in getattr(client, "truncated_accounts", set())
        if truncated:
            # The page cap was hit, so an older window went unfetched. If the bank
            # pages newest-first, advancing the cursor to the newest fetched date
            # would strand that window outside every future pull — so hold the
            # cursor and re-attempt from it next run.
            log.warning("account %s: pagination truncated — cursor held, not advanced past the hole", uid)
        elif rows and not dry_run:
            # Advance the cursor to the max booking_date SEEN this pull, but never
            # regress it: an overlap pull that returns only backdated rows must
            # not drag the high-water mark backwards.
            new_cursor = max(r["booking_date"] for r in rows)
            if cursor_date and cursor_date[:10] > new_cursor:
                new_cursor = cursor_date[:10]
            db.set_state(conn, state_keys.cursor(uid), new_cursor)
    if dry_run:
        conn.rollback()
        log.info("dry-run: rolled back %d would-be inserts", total_inserted)
    else:
        conn.commit()
    return total_inserted, total_submitted


def build_client(cfg: dict[str, Any], app_id: str, key_path: str) -> EnableBankingClient:
    """
    Construct the bank client from config, including retry and pagination
    settings.
    """
    eb = cfg.get("enable_banking") or {}
    return EnableBankingClient(
        app_id,
        key_path,
        eb.get("base_url", DEFAULT_BASE_URL),
        max_retries=int(eb.get("max_retries", 2)),
        retry_backoff=float(eb.get("retry_backoff_seconds", 1.5)),
        max_pages=int(eb.get("max_pages", 50)),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pull transactions from Enable Banking into the ledger.")
    parser.add_argument(
        "--dry-run", action="store_true", help="fetch but roll back all writes (still consumes one API-allowance unit)"
    )
    parser.add_argument(
        "--from", dest="date_from", default=None, metavar="ISO_DATE", help="override the cursor start date"
    )
    parser.add_argument("--db", default=None, help="database path (default: config db_path)")
    parser.add_argument("--config", default=None, help="path to config.yaml")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    load_dotenv()
    cfg = db.load_config(args.config)
    eb_cfg = cfg.get("enable_banking", {})

    conn = db.connect(args.db or cfg.get("db_path", "ledger.db"))
    try:
        db.init_db(conn)
        warn_if_consent_expiring(conn, int(eb_cfg.get("consent_warn_days", 14)), args.dry_run)

        app_id = os.environ.get("ENABLE_BANKING_APP_ID")
        key_path = os.environ.get("ENABLE_BANKING_PRIVATE_KEY_PATH")
        uids_raw = db.get_state(conn, state_keys.EB_ACCOUNT_UIDS)
        if not app_id or not key_path or not uids_raw:
            log.error(
                "Phase 0 incomplete: need ENABLE_BANKING_APP_ID + "
                "ENABLE_BANKING_PRIVATE_KEY_PATH in .env and eb_account_uids "
                "in state (SPEC §6 runbook)"
            )
            return 2
        if not Path(key_path).is_file():
            log.error("private key not found at %s (path only, key stays outside the repo)", key_path)
            return 2

        if not check_and_consume_allowance(conn, int(eb_cfg.get("api_daily_call_limit", 4))):
            return 3

        default_from = (datetime.now(db.TZ).date() - timedelta(days=int(eb_cfg.get("first_pull_days", 90)))).isoformat()
        client = build_client(cfg, app_id, key_path)
        inserted, submitted = run_ingest(
            conn,
            client,
            json.loads(uids_raw),
            default_from=default_from,
            date_from=args.date_from,
            dry_run=args.dry_run,
            cursor_overlap_days=int(eb_cfg.get("cursor_overlap_days", 5)),
            currency=cfg.get("currency", "EUR"),
        )
        log.info("ingest %s: %d new / %d fetched", "dry-run" if args.dry_run else "done", inserted, submitted)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
