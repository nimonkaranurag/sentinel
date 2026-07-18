"""
Recurring-bill checklist and alerts.

A registry of expected recurring charges (bills.yaml, merged with the
git-ignored bills.local.yaml for owner-specific merchant patterns). Each poll
raises two kinds of alert:

- LATE: past the due date plus grace with no matching charge for the current
  cycle, which catches a bounced direct debit. Lateness is measured against a
  real due date that rolls across month boundaries, so end-of-month bills are
  detectable.
- DRIFT: a matching charge whose amount falls outside expected ± tolerance,
  which catches a quiet price change.

The weekly report renders the checklist.

Each bill is schema-checked at load (name, pattern, due_day, expected_cents,
tolerance_pct); a missing or unknown key, or an out-of-range value, raises rather
than defaulting silently. `pattern` is a regex over the NORMALIZED merchant name
— the same surface rules.yaml and policy `pattern` matchers see (see
_cycle_match).
"""

from __future__ import annotations

import calendar
import logging
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

from . import db, state_keys, telegram
from .db import fmt_eur
from .normalize import normalize

log = logging.getLogger(__name__)

DEFAULT_BILLS_PATH = Path(__file__).resolve().parent / "bills.yaml"
_REQUIRED = ("name", "pattern", "due_day", "expected_cents", "tolerance_pct")

# A direct debit can post a few days before its nominal due day; a charge in
# [due − early_match_days, as_of] counts as paying the current cycle. Kept well
# under a monthly period so a *previous* cycle's payment can't leak into it.
# Config overrides via bills.early_match_days (config.yaml).
DEFAULT_EARLY_MATCH_DAYS = 5


def _early_match_days(cfg: dict[str, Any]) -> int:
    return int((cfg.get("bills") or {}).get("early_match_days", DEFAULT_EARLY_MATCH_DAYS))


def load_bills(path: str | Path | None = None) -> tuple[list[dict[str, Any]], int]:
    """
    Load bills, merging the git-ignored bills.local.yaml ahead of the shared
    bills.yaml. Returns (bills, grace_days).

    Values are range-validated, not merely key-checked: an out-of-range due_day
    or tolerance_pct raises rather than defaulting silently.
    """
    base = Path(path) if path else DEFAULT_BILLS_PATH
    bills: list[dict[str, Any]] = []
    grace = 3
    grace_set = False
    # Iterate local-then-shared so local bills merge ahead. For the scalar
    # grace_days, LOCAL wins (locals-win is the contract everywhere else): take
    # it from the first file that defines it.
    for p in (base.with_name("bills.local.yaml"), base):
        if not p.exists():
            continue
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if "grace_days" in raw and not grace_set:
            grace = _validate_grace(p, raw["grace_days"])
            grace_set = True
        for i, entry in enumerate(raw.get("bills", []), 1):
            missing = [k for k in _REQUIRED if k not in entry]
            if missing:
                raise ValueError(f"{p} bill #{i} ({entry.get('name')}): missing {missing}")
            stray = set(entry) - set(_REQUIRED)
            if stray:
                raise ValueError(f"{p} bill #{i} ({entry['name']}): unknown key(s) {sorted(stray)}")
            b = dict(entry)
            _validate_bill(p, i, b)
            b["_re"] = re.compile(b["pattern"])
            bills.append(b)
    return bills, grace


def _validate_grace(p: Path, value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{p}: grace_days must be a non-negative integer, got {value!r}")
    return value


def _validate_bill(p: Path, i: int, b: dict[str, Any]) -> None:
    name = b["name"]
    due = b["due_day"]
    if not isinstance(due, int) or isinstance(due, bool) or not 1 <= due <= 31:
        raise ValueError(f"{p} bill #{i} ({name}): due_day must be an integer 1–31, got {due!r}")
    exp = b["expected_cents"]
    if not isinstance(exp, int) or isinstance(exp, bool) or exp <= 0:
        raise ValueError(
            f"{p} bill #{i} ({name}): expected_cents must be a positive integer (cents, not '12e2'), got {exp!r}"
        )
    tol = b["tolerance_pct"]
    if not isinstance(tol, int) or isinstance(tol, bool) or not 0 <= tol <= 100:
        raise ValueError(f"{p} bill #{i} ({name}): tolerance_pct must be an integer 0–100, got {tol!r}")


def _clamped_due(year: int, month: int, due_day: int) -> date:
    """
    Return due_day clamped to the month's length (so due_day 31 maps to Feb
    28 or 29).
    """
    return date(year, month, min(due_day, calendar.monthrange(year, month)[1]))


def current_due(as_of: date, due_day: int) -> date:
    """
    Return the most recent due date at or before as_of, across month boundaries.

    This makes end-of-month bills detectable: with due_day 28 and as_of on the
    2nd of the following month, the due date is the previous month's 28th, so a
    missed payment reads as late.
    """
    this_month = _clamped_due(as_of.year, as_of.month, due_day)
    if this_month <= as_of:
        return this_month
    year, month = (as_of.year, as_of.month - 1) if as_of.month > 1 else (as_of.year - 1, 12)
    return _clamped_due(year, month, due_day)


def _cycle_match(conn, bill: dict[str, Any], due: date, as_of: date, early_days: int):
    """
    Return the most recent matching charge in the current cycle window, or None.
    """
    start = (due - timedelta(days=early_days)).isoformat()
    for row in conn.execute(
        "SELECT booking_date, merchant_raw, amount_cents FROM transactions "
        "WHERE amount_cents < 0 AND booking_date >= ? AND booking_date <= ? "
        "ORDER BY booking_date DESC",
        (start, as_of.isoformat()),
    ).fetchall():
        if bill["_re"].search(normalize(row["merchant_raw"])):
            return row
    return None


def check(conn, cfg: dict[str, Any], as_of: date, path: str | Path | None = None) -> list[dict[str, Any]]:
    bills, grace = load_bills(path)
    early = _early_match_days(cfg)
    alerts = []
    for b in bills:
        due = current_due(as_of, b["due_day"])
        row = _cycle_match(conn, b, due, as_of, early)
        if row is not None:
            amt = -row["amount_cents"]
            lo = b["expected_cents"] * (100 - b["tolerance_pct"]) // 100
            hi = b["expected_cents"] * (100 + b["tolerance_pct"]) // 100
            if not lo <= amt <= hi:
                alerts.append(
                    {
                        "bill": b["name"],
                        "kind": "drift",
                        "due": due.isoformat(),
                        "text": f"⚠️ {b['name']} was {fmt_eur(amt)}, expected ~{fmt_eur(b['expected_cents'])} "
                        f"(±{b['tolerance_pct']}%) — price change?",
                    }
                )
        else:
            days_late = (as_of - due).days
            if days_late > grace:
                alerts.append(
                    {
                        "bill": b["name"],
                        "kind": "late",
                        "due": due.isoformat(),
                        "text": f"🔴 {b['name']} unpaid — {days_late} days past due ({due.isoformat()}). "
                        "Bounced direct debit?",
                    }
                )
    return alerts


def send_alerts(conn, cfg: dict[str, Any], as_of: date, dry_run: bool = False, path: str | Path | None = None) -> int:
    """
    Fire the late/drift alerts for the current cycle through the Telegram seam,
    idempotently. Returns the number sent.

    `check()` re-returns the same alert every poll, so each is guarded by a
    per-cycle state key: one late bill sends exactly one message per cycle, not
    one per poll (four polls a day). The guard is written only after the send
    succeeds, so a crashed send replays next poll.
    """
    path = path if path is not None else (cfg.get("bills") or {}).get("path")
    sent = 0
    for a in check(conn, cfg, as_of, path):
        key = state_keys.bill_alerted(a["bill"], a["due"], a["kind"])
        if db.get_state(conn, key) is not None:
            continue
        if dry_run:
            log.info("dry-run bill alert: %s", a["text"])
        else:
            telegram.send_message(a["text"])
            db.set_state(conn, key, db.now_iso())
            conn.commit()
        sent += 1
    return sent


def render_checklist(conn, cfg: dict[str, Any], as_of: date, path: str | Path | None = None) -> str:
    bills, grace = load_bills(path)
    early = _early_match_days(cfg)
    lines = ["Bills this month:"]
    for b in bills:
        due = current_due(as_of, b["due_day"])
        row = _cycle_match(conn, b, due, as_of, early)
        if row is not None:
            lines.append(f"  ✅ {b['name']} {fmt_eur(-row['amount_cents'])} (paid {row['booking_date']})")
        elif (as_of - due).days > grace:
            lines.append(f"  🔴 {b['name']} — overdue since {due.isoformat()}")
        else:
            lines.append(f"  ⏳ {b['name']} — due {due.isoformat()}")
    return "\n".join(lines) if bills else "Bills this month: (none configured — add to bills.local.yaml)"
