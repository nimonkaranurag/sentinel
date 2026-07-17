"""
Canonical keys for the `state` table.

The `state` table maps strings to strings: cursors, per-day counters,
watermarks, consent metadata, and idempotency markers. Defining every key in one
place, as a constant for the fixed keys and a formatter for the parameterized
keys, keeps call sites consistent and provides a single inventory of the table's
contents.
"""

from __future__ import annotations

# ── Enable Banking consent / session (authorize writes; ingest/notify read) ──
EB_ACCOUNT_UIDS = "eb_account_uids"      # JSON list of account uids to pull
EB_SESSION_ID = "eb_session_id"
EB_ASPSP = "eb_aspsp"                     # JSON {"name", "country"}
CONSENT_EXPIRY = "consent_expiry"        # ISO date the consent dies
CONSENT_WARNED_ON = "consent_warned_on"  # ISO date of the last expiry nag (once/day)
CONSENT_ERROR_NOTIFIED_ON = "consent_error_notified_on"  # ISO date the bank last 401/403'd us (once/day)
# ISO date the API's coverage window begins (the first pull's date_from). The CSV
# backfill clips to this, so one backdated bank row can't amputate the backfill.
API_COVERAGE_START = "api_coverage_start"

# ── Alerting ────────────────────────────────────────────────────────────────
# Durable policy-alert watermark: every transaction with rowid <= this has been
# through policy evaluation. Advanced only after the alert batch sends, so a
# crash mid-poll replays instead of silently dropping alerts.
#
# HAZARD: this keys on transactions.rowid. `transactions` has a TEXT primary key,
# so its rowids are IMPLICIT, and implicit rowids are renumbered by VACUUM. A
# VACUUM can therefore hand a freshly-inserted row a rowid at or below this
# watermark, and that row would never be evaluated (the events guard blocks
# duplicate alerts, not missed ones). Do NOT VACUUM ledger.db — the nightly
# `.backup` reclaims space safely without it (see the Makefile/crontab note).
ALERTS_CHECKED_THROUGH = "alerts_checked_through"

# ── Telegram long-poll cursor ────────────────────────────────────────────────
TG_UPDATE_OFFSET = "tg_update_offset"


def api_calls(day_iso: str) -> str:
    """
    Return the state key for the per-day unattended API-allowance counter
    (Europe/Dublin date).
    """
    return f"api_calls:{day_iso}"


def cursor(account_uid: str) -> str:
    """
    Return the state key for a per-account ingest cursor (the maximum
    booking_date pulled so far).
    """
    return f"cursor:{account_uid}"


def daily_push_sent(day_iso: str) -> str:
    """
    Return the state key for the daily safe-to-spend push idempotency marker.
    """
    return f"daily_push_sent:{day_iso}"


def digest_sent(iso_year: int, iso_week: int) -> str:
    """
    Return the state key for the weekly digest idempotency marker
    (ISO year-week).
    """
    return f"digest_sent:{iso_year}-W{iso_week:02d}"


def plan_sent(iso_year: int, iso_week: int) -> str:
    """
    Return the state key for the Monday plan push idempotency marker
    (ISO year-week).
    """
    return f"plan_sent:{iso_year}-W{iso_week:02d}"


def bill_alerted(name: str, due_iso: str, kind: str) -> str:
    """
    Return the state key for a per-cycle bill-alert idempotency marker.

    `check()` re-returns the same late/drift alert every poll; keying on the
    bill name, its current due date, and the alert kind means one late bill fires
    exactly one Telegram message per cycle, not one per poll.
    """
    return f"bill_alerted:{name}:{due_iso}:{kind}"
