"""
Safe-to-spend: the single daily figure.

The discretionary pool is one config value (`budgets.pool_monthly_cents`, set
once by hand from the labeled history). It is anchored to the owner's PAY CYCLE,
not the calendar month: the pool resets on payday and safe-to-spend is what's
left spread over the days until the *next* payday:

    cycle_start   = the payday on or before today (see `cycle_for`)
    discretionary = cycle-to-date spend in the discretionary buckets
                    (Groceries + FoodDelivery + Other)
    days_left     = days from today until the next payday (>= 1)
    safe_today    = max(0, (pool − discretionary) // days_left)

Payday is a nominal day-of-month (`payday.day_of_month`, default 23). Sentinel
keeps no holiday calendar: when the bank pays early (a weekend/bank holiday) or
late, the owner logs the real day with /paid-today, which writes a
`payday_actual:<YYYY-MM>` override that `cycle_for` honours in place of the
nominal day.

`graduation_surplus` stays a calendar-month query for the monthly report,
recomputed from the ledger rather than stored as a streak. All amounts are
integer cents.

CLI: python -m sentinel.controller [--as-of DATE] [--db PATH] [--config PATH]
"""

from __future__ import annotations

import argparse
import calendar
import logging
from datetime import date, datetime, timedelta
from typing import Any

from . import db, state_keys
from .categorize import BUCKETS, DISCRETIONARY_BUCKETS, bucket

log = logging.getLogger(__name__)


def month_bounds(as_of: date) -> tuple[date, date, int]:
    days = calendar.monthrange(as_of.year, as_of.month)[1]
    return as_of.replace(day=1), as_of.replace(day=days), days


# ── Pay cycle (payday-anchored) ─────────────────────────────────────────────


def payday_day(cfg: dict[str, Any]) -> int:
    return int((cfg.get("payday") or {}).get("day_of_month", 23))


def _add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    idx = year * 12 + (month - 1) + delta
    return idx // 12, idx % 12 + 1


def scheduled_payday(year: int, month: int, cfg: dict[str, Any]) -> date:
    """
    Return the nominal payday for a month, clamped to the month's last day (so a
    day_of_month of 31 still lands in February).
    """
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(payday_day(cfg), last))


def _resolved_payday(conn, cfg: dict[str, Any], year: int, month: int) -> tuple[date, str]:
    """
    Return (payday, source) for one month: a logged /paid-today override if one
    exists for that cycle, else the scheduled nominal payday.
    """
    override = db.get_state(conn, state_keys.payday_actual(year, month))
    if override:
        try:
            return date.fromisoformat(override), "logged"
        except ValueError:
            log.warning("ignoring malformed logged payday %r for %d-%02d", override, year, month)
    return scheduled_payday(year, month, cfg), "scheduled"


def cycle_for(conn, cfg: dict[str, Any], as_of: date) -> dict[str, Any]:
    """
    Return the pay cycle containing `as_of`: its start (the payday on or before
    today), the next payday, how many days remain until it (>= 1), and whether
    the current cycle's start came from a logged /paid-today override.

    The three-month bracket around `as_of` always contains a payday on each side,
    so the cycle is well-defined for any date.
    """
    paydays = sorted(
        (_resolved_payday(conn, cfg, *_add_months(as_of.year, as_of.month, k)) for k in (-1, 0, 1)),
        key=lambda pd: pd[0],
    )
    start = max((pd for pd in paydays if pd[0] <= as_of), key=lambda pd: pd[0])
    nxt = min((pd for pd in paydays if pd[0] > as_of), key=lambda pd: pd[0])
    return {
        "cycle_start": start[0],
        "cycle_start_source": start[1],
        "next_payday": nxt[0],
        "days_left": (nxt[0] - as_of).days,
    }


def cycle_month_for(cfg: dict[str, Any], when: date) -> tuple[int, int]:
    """
    Return the (year, month) whose scheduled payday is nearest `when` — the cycle
    a /paid-today on `when` should override. Ties break to the earlier month.
    """
    best: tuple[int, int, int] | None = None
    for k in (-1, 0, 1):
        year, month = _add_months(when.year, when.month, k)
        dist = abs((scheduled_payday(year, month, cfg) - when).days)
        if best is None or dist < best[0]:
            best = (dist, year, month)
    assert best is not None
    return best[1], best[2]


def pool_cents(cfg: dict[str, Any]) -> int:
    return int((cfg.get("budgets") or {}).get("pool_monthly_cents", 0))


def unlabeled_inflow_exclude_cents(cfg: dict[str, Any]) -> int:
    return int((cfg.get("controller") or {}).get("unlabeled_inflow_exclude_cents", 10_000))


def spend_by_bucket(conn, start: date, end: date, inflow_exclude_cents: int = 0) -> dict[str, int]:
    """
    Return net spend per math bucket over [start, end] inclusive, in integer
    cents.

    Amounts are netted, not summed as outflows only: a positive amount in a
    spend category is a refund and offsets the matching purchase, so refunds do
    not deplete the pool or trigger false alerts. Only a *large, Uncategorized*
    inflow (an unmapped transfer, at or above `inflow_exclude_cents`) is held out
    of the netting until it is labeled — a big refund the categorizer has already
    attached to a known merchant is exactly the case netting is for, so it must
    still net.
    """
    clause = ""
    params: list[Any] = [start.isoformat(), end.isoformat()]
    if inflow_exclude_cents > 0:
        clause = "AND NOT (amount_cents >= ? AND category = 'Uncategorized') "
        params.append(inflow_exclude_cents)
    rows = conn.execute(
        "SELECT category, SUM(-amount_cents) AS spend FROM v_transactions_categorized "
        "WHERE category NOT IN ('Income', 'Transfers') "
        "AND booking_date >= ? AND booking_date <= ? " + clause + "GROUP BY category",
        params,
    ).fetchall()
    out: dict[str, int] = {b: 0 for b in BUCKETS}
    for r in rows:
        out[bucket(r["category"])] += r["spend"]
    return out


def unlabeled_inflows(conn, start: date, end: date, threshold_cents: int) -> tuple[int, int]:
    """
    Return (count, total_cents) of the large *Uncategorized* inflows held out of
    the pool, so they can be surfaced for labeling rather than silently dropped.

    Scoped to Uncategorized to match spend_by_bucket: a large inflow the
    categorizer already attached to a known merchant is a refund that nets, not an
    unlabeled transfer, so it belongs in neither this count nor the exclusion.
    """
    if threshold_cents <= 0:
        return 0, 0
    row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(amount_cents), 0) AS total "
        "FROM v_transactions_categorized "
        "WHERE category = 'Uncategorized' AND amount_cents >= ? "
        "AND booking_date >= ? AND booking_date <= ?",
        (threshold_cents, start.isoformat(), end.isoformat()),
    ).fetchone()
    return row["n"], row["total"]


def safe_to_spend(conn, cfg: dict[str, Any], as_of: date) -> dict[str, Any]:
    """
    Compute safe-to-spend and its components. Read-only; a pure function of the
    ledger, the configured pool, and the pay cycle (`cycle_for`).

    Spend and unlabeled inflows are measured over the current pay cycle
    (cycle_start → as_of), not the calendar month, so the pool resets on payday.
    """
    cycle = cycle_for(conn, cfg, as_of)
    cycle_start: date = cycle["cycle_start"]
    exclude = unlabeled_inflow_exclude_cents(cfg)
    spent = spend_by_bucket(conn, cycle_start, as_of, inflow_exclude_cents=exclude)
    discretionary = sum(spent.get(b, 0) for b in DISCRETIONARY_BUCKETS)
    pool = pool_cents(cfg)
    remaining = pool - discretionary
    days_left = cycle["days_left"]
    infl_n, infl_cents = unlabeled_inflows(conn, cycle_start, as_of, exclude)
    return {
        "as_of": as_of.isoformat(),
        "cycle_start": cycle_start.isoformat(),
        "next_payday": cycle["next_payday"].isoformat(),
        "cycle_start_source": cycle["cycle_start_source"],
        "days_left": days_left,
        "pool_cents": pool,
        "discretionary_spent_cents": discretionary,
        "remaining_cents": remaining,
        "safe_today_cents": max(0, remaining // days_left),
        "by_bucket_cents": spent,
        "unlabeled_inflow_count": infl_n,
        "unlabeled_inflow_cents": infl_cents,
    }


def month_surplus(conn, month_key: str, inflow_exclude_cents: int = 0) -> dict[str, Any]:
    """
    Return income minus spend for one calendar month.

    Transfers and Income are excluded from spend by construction, so the surplus
    measures spending independent of family transfers. Spend is *netted*: a
    positive amount in a spend category is a refund and reduces spend, exactly as
    in spend_by_bucket — except a large Uncategorized inflow (>= the exclude
    threshold), which is an unlabeled transfer rather than a refund and must not
    flatter the surplus.
    """
    exclude = "AND NOT (amount_cents >= ? AND category = 'Uncategorized') " if inflow_exclude_cents > 0 else ""
    params: list[Any] = ([inflow_exclude_cents] if inflow_exclude_cents > 0 else []) + [month_key]
    row = conn.execute(
        "SELECT "
        "  SUM(CASE WHEN amount_cents > 0 AND category = 'Income' THEN amount_cents ELSE 0 END) AS income, "
        "  SUM(CASE WHEN category NOT IN ('Transfers', 'Income') "
        + exclude
        + "      THEN -amount_cents ELSE 0 END) AS spend "
        "FROM v_transactions_categorized WHERE strftime('%Y-%m', booking_date) = ?",
        params,
    ).fetchone()
    income, spend = int(row["income"] or 0), int(row["spend"] or 0)
    return {"month": month_key, "income_cents": income, "spend_cents": spend, "surplus_cents": income - spend}


def graduation_surplus(conn, cfg: dict[str, Any], as_of: date) -> dict[str, Any]:
    """
    Return the most recent completed month's surplus against the target.

    Recomputed from the ledger on each call rather than stored as a streak, so
    backfills and /recat corrections apply retroactively.
    """
    target = int((cfg.get("controller") or {}).get("graduation_surplus_cents", 100_000))
    month_start, _, _ = month_bounds(as_of)
    prev_key = (month_start - timedelta(days=1)).strftime("%Y-%m")
    s = month_surplus(conn, prev_key, unlabeled_inflow_exclude_cents(cfg))
    return {**s, "target_cents": target, "met": s["surplus_cents"] >= target}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Safe-to-spend (the one daily number).")
    parser.add_argument("--as-of", default=None, metavar="ISO_DATE")
    parser.add_argument("--db", default=None)
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    cfg = db.load_config(args.config)
    conn = db.connect(args.db or cfg.get("db_path", "ledger.db"))
    try:
        db.init_db(conn)
        as_of = date.fromisoformat(args.as_of) if args.as_of else datetime.now(db.TZ).date()
        s = safe_to_spend(conn, cfg, as_of)
        log.info(
            "safe to spend today: %s · %s of %s pool left · %d day(s) to payday (%s)",
            db.fmt_eur(s["safe_today_cents"]),
            db.fmt_eur(s["remaining_cents"]),
            db.fmt_eur(s["pool_cents"]),
            s["days_left"],
            s["next_payday"],
        )
        # The hard-rails runbook (docs/RAILS.md) funds a discretionary card with a
        # weekly standing order; this is the amount — the monthly pool spread over
        # a year of weeks. Round DOWN when you set the standing order.
        log.info("suggested weekly rail: %s (monthly pool × 12 ÷ 52)", db.fmt_eur(pool_cents(cfg) * 12 // 52))
        for b in DISCRETIONARY_BUCKETS:
            log.info("  %-13s %s", b, db.fmt_eur(s["by_bucket_cents"].get(b, 0)))
        grad = graduation_surplus(conn, cfg, as_of)
        log.info(
            "last month (%s) surplus: %s (target %s%s)",
            grad["month"],
            db.fmt_eur(grad["surplus_cents"]),
            db.fmt_eur(grad["target_cents"]),
            " — 🎓 met" if grad["met"] else "",
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
