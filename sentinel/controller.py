"""
Safe-to-spend: the single daily figure.

The monthly discretionary pool is one config value
(`budgets.pool_monthly_cents`, set once by hand from the labeled history).
Safe-to-spend is the remainder of that pool spread over the days left in the
month:

    discretionary_MTD = month-to-date spend in the discretionary buckets
                        (Groceries + FoodDelivery + Other)
    safe_today        = max(0, (pool − discretionary_MTD) // days_left_incl_today)

`graduation_surplus` is a single query for the monthly report, recomputed from
the ledger rather than stored as a streak. All amounts are integer cents.

CLI: python -m sentinel.controller [--as-of DATE] [--db PATH] [--config PATH]
"""

from __future__ import annotations

import argparse
import calendar
import logging
from datetime import date, datetime, timedelta
from typing import Any

from . import db
from .categorize import BUCKETS, DISCRETIONARY_BUCKETS, bucket

log = logging.getLogger(__name__)


def month_bounds(as_of: date) -> tuple[date, date, int]:
    days = calendar.monthrange(as_of.year, as_of.month)[1]
    return as_of.replace(day=1), as_of.replace(day=days), days


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
    ledger and the configured pool.
    """
    month_start, _, days_in_month = month_bounds(as_of)
    exclude = unlabeled_inflow_exclude_cents(cfg)
    spent = spend_by_bucket(conn, month_start, as_of, inflow_exclude_cents=exclude)
    discretionary = sum(spent.get(b, 0) for b in DISCRETIONARY_BUCKETS)
    pool = pool_cents(cfg)
    remaining = pool - discretionary
    days_left = days_in_month - as_of.day + 1
    infl_n, infl_cents = unlabeled_inflows(conn, month_start, as_of, exclude)
    return {
        "as_of": as_of.isoformat(),
        "month": month_start.strftime("%Y-%m"),
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
        "  SUM(CASE WHEN category NOT IN ('Transfers', 'Income') " + exclude +
        "      THEN -amount_cents ELSE 0 END) AS spend "
        "FROM v_transactions_categorized WHERE strftime('%Y-%m', booking_date) = ?",
        params,
    ).fetchone()
    income, spend = int(row["income"] or 0), int(row["spend"] or 0)
    return {"month": month_key, "income_cents": income, "spend_cents": spend,
            "surplus_cents": income - spend}


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
        log.info("safe to spend today: %s · %s of %s pool left · %d days",
                 db.fmt_eur(s["safe_today_cents"]), db.fmt_eur(s["remaining_cents"]),
                 db.fmt_eur(s["pool_cents"]), s["days_left"])
        # The hard-rails runbook (docs/RAILS.md) funds a discretionary card with a
        # weekly standing order; this is the amount — the monthly pool spread over
        # a year of weeks. Round DOWN when you set the standing order.
        log.info("suggested weekly rail: %s (monthly pool × 12 ÷ 52)",
                 db.fmt_eur(pool_cents(cfg) * 12 // 52))
        for b in DISCRETIONARY_BUCKETS:
            log.info("  %-13s %s", b, db.fmt_eur(s["by_bucket_cents"].get(b, 0)))
        grad = graduation_surplus(conn, cfg, as_of)
        log.info("last month (%s) surplus: %s (target %s%s)", grad["month"],
                 db.fmt_eur(grad["surplus_cents"]), db.fmt_eur(grad["target_cents"]),
                 " — 🎓 met" if grad["met"] else "")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
