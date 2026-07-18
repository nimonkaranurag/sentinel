"""
Spending-policy alerts (SPEC §4, feature 1).

On each poll, every newly-booked spend transaction is checked against
policies.yaml. A match whose month-to-date total exceeds its cap emits an
escalating templated alert, computed by arithmetic alone: the merchant and
amount, the occurrence count this month, the month-to-date total against the cap,
and the annualized pace.

Month-to-date is NETTED (SPEC §4): a refund matching the same policy offsets its
purchase, so refunds never fire false alerts — except a large *Uncategorized*
inflow (an unlabeled transfer), which is held out of the netting exactly as
safe-to-spend holds it out of the pool, so it cannot suppress genuine alerts.

policies.yaml is schema-checked at load. Each policy requires a name, a
cap_monthly_cents, and exactly one matcher (bucket, sub_label, or pattern); a
missing, unknown, or misspelled key raises rather than defaulting silently.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from . import db
from .categorize import BUCKETS, TAXONOMY, bucket
from .controller import month_bounds, unlabeled_inflow_exclude_cents
from .normalize import display_merchant, normalize

log = logging.getLogger(__name__)

DEFAULT_POLICIES_PATH = Path(__file__).resolve().parent / "policies.yaml"
_MATCHERS = ("bucket", "sub_label", "pattern")
_ALLOWED_KEYS = {"name", "cap_monthly_cents", *_MATCHERS}


def load_policies(path: str | Path | None = None) -> list[dict[str, Any]]:
    p = Path(path) if path else DEFAULT_POLICIES_PATH
    if not p.exists():
        return []
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    out: list[dict[str, Any]] = []
    for i, entry in enumerate(raw.get("policies", []), 1):
        if not isinstance(entry, dict):
            raise ValueError(f"{p} policy #{i}: must be a mapping")
        keys = set(entry)
        if "name" not in entry or "cap_monthly_cents" not in entry:
            raise ValueError(f"{p} policy #{i}: needs 'name' and 'cap_monthly_cents'")
        matchers = keys & set(_MATCHERS)
        if len(matchers) != 1:
            raise ValueError(
                f"{p} policy #{i} ({entry.get('name')}): needs exactly one matcher "
                f"of {_MATCHERS}, got {sorted(matchers) or 'none'}"
            )
        stray = keys - _ALLOWED_KEYS
        if stray:
            raise ValueError(f"{p} policy #{i} ({entry['name']}): unknown key(s) {sorted(stray)}")
        if entry.get("bucket") is not None and entry.get("bucket") not in BUCKETS:
            raise ValueError(f"{p} policy #{i}: bucket {entry['bucket']!r} not in {BUCKETS}")
        if entry.get("sub_label") is not None and entry.get("sub_label") not in TAXONOMY:
            raise ValueError(f"{p} policy #{i}: sub_label {entry['sub_label']!r} not in the taxonomy")
        compiled = dict(entry)
        if "pattern" in compiled:
            compiled["_re"] = re.compile(compiled["pattern"])
        out.append(compiled)
    return out


def _matches(policy: dict[str, Any], txn) -> bool:
    if "bucket" in policy:
        return bucket(txn["category"]) == policy["bucket"]
    if "sub_label" in policy:
        return txn["category"] == policy["sub_label"]
    # Match the SAME surface the categorizer's regex rules match: the normalized
    # merchant name (blob stripped, uppercased). Matching raw merchant_raw would
    # give one YAML author two different matching surfaces.
    return policy["_re"].search(normalize(txn["merchant_raw"])) is not None


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def alert_text(policy: dict[str, Any], txn, mtd_cents: int, count: int) -> str:
    cap = int(policy["cap_monthly_cents"])
    icon = "🔴" if mtd_cents > cap else "⚠️"
    merchant = display_merchant(txn["merchant_raw"]) or policy["name"]
    return (
        f"{icon} {merchant} {db.fmt_eur(-txn['amount_cents'])} — {_ordinal(count)} this month. "
        f"{db.fmt_eur(mtd_cents)} of your {db.fmt_eur(cap)} {policy['name']} cap. "
        f"{db.fmt_eur(mtd_cents * 12)}/yr at this pace."
    )


def evaluate(
    conn, cfg: dict[str, Any], as_of: date, new_txn_ids, path: str | Path | None = None
) -> list[dict[str, Any]]:
    """
    Return {txn_id, policy, text} for each newly-booked spend transaction that
    trips a policy over its monthly cap.

    The month-to-date total is netted: refunds (positive amounts matching the
    same policy) offset their purchases, so a refunded charge cannot fire a
    false alert (SPEC §4). Large Uncategorized inflows are held out of the
    netting (see module docstring). Only spend transactions can be alert
    subjects or count toward the occurrence ordinal.

    The window runs to the END of as_of's month, not to as_of: the watermark
    visits each row exactly once, so a booking stamped by a calendar slightly
    ahead of Europe/Dublin (a CET/EET-dated row landing near midnight) must be
    evaluable on the poll that first sees it — clamping at as_of would drop its
    alert forever.

    Read-only; the caller sends and records the alerts.
    """
    policies = load_policies(path or (cfg.get("policies") or {}).get("path"))
    if not policies:
        return []
    month_start, month_end, _ = month_bounds(as_of)
    # Query the base table (not the view) so we get rowid: booking_date is
    # day-granular, so ordering by it alone lets same-day charges that were
    # inserted later count into an earlier one's "9th this month". rowid is
    # insertion order and breaks the tie deterministically.
    rows = conn.execute(
        "SELECT t.rowid AS rid, t.id, t.booking_date, t.merchant_raw, "
        "  COALESCE(t.category_override, m.category, 'Uncategorized') AS category, "
        "  t.amount_cents "
        "FROM transactions t LEFT JOIN merchants m ON m.id = t.merchant_id "
        "WHERE t.booking_date >= ? AND t.booking_date <= ? "
        "ORDER BY t.booking_date, t.rowid",
        (month_start.isoformat(), month_end.isoformat()),
    ).fetchall()
    exclude = unlabeled_inflow_exclude_cents(cfg)
    month_txns = [
        t for t in rows if not (exclude > 0 and t["amount_cents"] >= exclude and t["category"] == "Uncategorized")
    ]
    new_ids = set(new_txn_ids)
    alerts = []
    for txn in month_txns:
        if txn["id"] not in new_ids or txn["amount_cents"] >= 0:
            continue  # a refund/inflow nets into MTD but never fires an alert itself
        for p in policies:
            if not _matches(p, txn):
                continue
            prior = [
                t
                for t in month_txns
                if _matches(p, t) and (t["booking_date"], t["rid"]) <= (txn["booking_date"], txn["rid"])
            ]
            mtd = sum(-t["amount_cents"] for t in prior)  # refunds subtract
            count = sum(1 for t in prior if t["amount_cents"] < 0)  # "Nth this month" counts charges only
            if mtd > int(p["cap_monthly_cents"]):
                alerts.append({"txn_id": txn["id"], "policy": p["name"], "text": alert_text(p, txn, mtd, count)})
            break  # first matching policy wins
    return alerts
