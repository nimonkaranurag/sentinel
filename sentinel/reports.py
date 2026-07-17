"""
Monthly expense report (SPEC §4, feature 5): decompose the untracked spend.

Writes EXPENSE_REPORT.md, subscriptions.md, and PNG charts to reports/. Built
from SQL, the stdlib statistics module, and matplotlib, with no LLM; the report
is deterministic arithmetic on the ledger.

Money stays integer cents in every computation; floats appear only in
dimensionless statistics (percentages, coefficients of variation) and at the
matplotlib display boundary.

Spend math excludes the Transfers and Income categories (SPEC §3). The gap
reconciliation computes, per calendar month:

    residual = inflows − fixed − categorized variable

which by arithmetic identity equals uncategorized spend + outbound transfers +
net saved; the table shows all three so the owner can attribute the gap.
Gate: |residual| < €200 for full months.

CLI: python -m sentinel.reports [--dry-run] [--as-of DATE] [--db PATH] [--config PATH]
"""

from __future__ import annotations

import argparse
import calendar
import logging
import statistics
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from . import db
from .categorize import FIXED_CATEGORIES, NON_SPEND_CATEGORIES
from .db import fmt_eur

log = logging.getLogger(__name__)

# Size bands in cents: [lo, hi) — tests the small-taps hypothesis.
SIZE_BANDS = (
    (0, 500, "<€5"),
    (500, 1_500, "€5–15"),
    (1_500, 4_000, "€15–40"),
    (4_000, 10_000, "€40–100"),
    (10_000, None, ">€100"),
)

RESIDUAL_GATE_CENTS = 20_000  # €200/mo

# Chart palette (light-mode PNGs). Dark ink on a near-white surface, one accent
# blue for magnitude bars, a green for the weekend series, and a small ordered
# set of hues for the multi-month burn-rate lines.
INK = "#0b0b0b"          # titles
INK_2 = "#52514e"        # axis labels / value labels
GRID = "#e4e3df"
SURFACE = "#fcfcfb"
BLUE = "#2a78d6"         # magnitude bars
AQUA = "#1baf7a"         # weekend bars (value-labelled so the hue isn't load-bearing)
SERIES = ("#2a78d6", "#1baf7a", "#eda100", "#008300")  # up to 4 months, assigned in order
NEUTRAL = "#8a8985"      # reference lines


def _window(as_of: date, window_days: int) -> tuple[str, str]:
    return (as_of - timedelta(days=window_days)).isoformat(), as_of.isoformat()


def _spend_where(prefix: str = "") -> str:
    non_spend = ", ".join(f"'{c}'" for c in NON_SPEND_CATEGORIES)
    return (f"{prefix}amount_cents < 0 AND {prefix}category NOT IN ({non_spend}) "
            f"AND {prefix}booking_date >= ? AND {prefix}booking_date <= ?")


# ── Extractors (SQL, integer cents) ───────────────────────────────────────


def category_pareto(conn, start: str, end: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT category, SUM(-amount_cents) AS spend, COUNT(*) AS n "
        f"FROM v_transactions_categorized WHERE {_spend_where()} "
        "GROUP BY category ORDER BY spend DESC", (start, end),
    ).fetchall()
    total = sum(r["spend"] for r in rows) or 1
    out, running = [], 0
    for r in rows:
        running += r["spend"]
        out.append({"category": r["category"], "spend_cents": r["spend"], "txns": r["n"],
                    "pct": 100.0 * r["spend"] / total, "cum_pct": 100.0 * running / total})
    return out


def merchant_pareto(conn, start: str, end: str, top: int = 25) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT COALESCE(m.display_name, m.name_normalized, '(unlinked)') AS name, "
        "  v.category AS category, SUM(-v.amount_cents) AS spend, COUNT(*) AS n "
        "FROM v_transactions_categorized v LEFT JOIN merchants m ON m.id = v.merchant_id "
        f"WHERE {_spend_where('v.')} "
        "GROUP BY v.merchant_id ORDER BY spend DESC LIMIT ?", (start, end, top),
    ).fetchall()
    total_row = conn.execute(
        f"SELECT COALESCE(SUM(-amount_cents), 0) FROM v_transactions_categorized WHERE {_spend_where()}",
        (start, end)).fetchone()
    total = total_row[0] or 1
    return [{"merchant": r["name"], "category": r["category"], "spend_cents": r["spend"],
             "txns": r["n"], "pct": 100.0 * r["spend"] / total} for r in rows]


def size_bands(conn, start: str, end: str) -> list[dict[str, Any]]:
    out = []
    for lo, hi, label in SIZE_BANDS:
        clause = "AND -amount_cents >= ? " + ("AND -amount_cents < ? " if hi is not None else "")
        params = (start, end, lo) + ((hi,) if hi is not None else ())
        row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(-amount_cents), 0) AS total "
            f"FROM v_transactions_categorized WHERE {_spend_where()} {clause}", params,
        ).fetchone()
        out.append({"band": label, "count": row["n"], "total_cents": row["total"]})
    return out


def weekday_profile(conn, start: str, end: str) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT CAST(strftime('%w', booking_date) AS INTEGER) AS dow, "
        "  SUM(-amount_cents) AS spend, COUNT(*) AS n "
        f"FROM v_transactions_categorized WHERE {_spend_where()} GROUP BY dow", (start, end),
    ).fetchall()
    by_dow = {r["dow"]: (r["spend"], r["n"]) for r in rows}  # sqlite: 0=Sun … 6=Sat
    labels = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    days = []
    for i, label in enumerate(labels):
        sqlite_dow = (i + 1) % 7
        spend, n = by_dow.get(sqlite_dow, (0, 0))
        days.append({"day": label, "spend_cents": spend, "txns": n, "weekend": label in ("Sat", "Sun")})
    weekend = sum(d["spend_cents"] for d in days if d["weekend"])
    weekday = sum(d["spend_cents"] for d in days if not d["weekend"])
    return {"days": days, "weekday_cents": weekday, "weekend_cents": weekend}


def daily_spend(conn, start: str, end: str) -> list[tuple[str, int]]:
    rows = conn.execute(
        "SELECT booking_date, SUM(-amount_cents) AS spend "
        f"FROM v_transactions_categorized WHERE {_spend_where()} "
        "GROUP BY booking_date ORDER BY booking_date", (start, end),
    ).fetchall()
    return [(r["booking_date"], r["spend"]) for r in rows]


def reconciliation(conn, start: str, end: str) -> list[dict[str, Any]]:
    """
    Return the gap table per calendar month; residual = inflows − fixed −
    variable.
    """
    fixed_in = ", ".join(f"'{c}'" for c in FIXED_CATEGORIES)
    non_spend = ", ".join(f"'{c}'" for c in NON_SPEND_CATEGORIES)
    rows = conn.execute(
        "SELECT strftime('%Y-%m', booking_date) AS month, "
        f" SUM(CASE WHEN amount_cents > 0 AND category IN ({non_spend}) THEN amount_cents ELSE 0 END) AS inflows, "
        f" SUM(CASE WHEN amount_cents < 0 AND category IN ({fixed_in}) THEN -amount_cents ELSE 0 END) AS fixed, "
        f" SUM(CASE WHEN amount_cents < 0 AND category NOT IN ({fixed_in}, {non_spend}, 'Uncategorized') "
        "      THEN -amount_cents ELSE 0 END) AS variable, "
        " SUM(CASE WHEN amount_cents < 0 AND category = 'Uncategorized' "
        "     THEN -amount_cents ELSE 0 END) AS uncategorized, "
        " SUM(CASE WHEN amount_cents < 0 AND category = 'Transfers' THEN -amount_cents ELSE 0 END) AS transfers_out, "
        " SUM(amount_cents) AS net "
        "FROM v_transactions_categorized WHERE booking_date >= ? AND booking_date <= ? "
        "GROUP BY month ORDER BY month", (start, end),
    ).fetchall()
    # "Full" means actual data spans the whole month — not merely that the
    # report window does. The API's first pull usually starts mid-month, so the
    # first calendar month is data-incomplete and must not be judged by the
    # residual gate.
    bounds = conn.execute(
        "SELECT MIN(booking_date) AS lo, MAX(booking_date) AS hi "
        "FROM v_transactions_categorized WHERE booking_date >= ? AND booking_date <= ?",
        (start, end),
    ).fetchone()
    data_lo = date.fromisoformat(bounds["lo"]) if bounds["lo"] else date.fromisoformat(start)
    data_hi = date.fromisoformat(bounds["hi"]) if bounds["hi"] else date.fromisoformat(end)
    out = []
    for r in rows:
        month_start = date.fromisoformat(r["month"] + "-01")
        month_end = date(month_start.year, month_start.month,
                         calendar.monthrange(month_start.year, month_start.month)[1])
        full = month_start >= data_lo and month_end <= data_hi
        residual = r["inflows"] - r["fixed"] - r["variable"]
        out.append({"month": r["month"], "full_month": full,
                    "inflows_cents": r["inflows"], "fixed_cents": r["fixed"],
                    "variable_cents": r["variable"], "uncategorized_cents": r["uncategorized"],
                    "transfers_out_cents": r["transfers_out"], "net_cents": r["net"],
                    "residual_cents": residual,
                    "gate_pass": abs(residual) < RESIDUAL_GATE_CENTS})
    return out


def detect_recurring(conn, cfg: dict[str, Any], as_of: date) -> list[dict[str, Any]]:
    """
    Detect recurring merchants over the full history.

    A merchant qualifies when it has at least the configured minimum number of
    occurrences, an amount coefficient of variation under the configured maximum
    (or identical amounts), and a median inter-charge interval in the monthly or
    weekly band with low deviation. Only still-active merchants are returned:
    those whose next expected charge is no more than one interval overdue.
    """
    rcfg = (cfg.get("reports") or {}).get("recurring") or {}
    min_n = int(rcfg.get("min_occurrences", 3))
    cv_max = float(rcfg.get("amount_cv_max", 0.10))
    monthly_lo, monthly_hi = rcfg.get("monthly_days", [28, 32])
    weekly_lo, weekly_hi = rcfg.get("weekly_days", [6, 8])
    mad_max = int(rcfg.get("mad_max_days", 3))

    rows = conn.execute(
        "SELECT t.merchant_id, COALESCE(m.display_name, m.name_normalized) AS name, "
        "  m.first_seen, t.booking_date, -t.amount_cents AS spend "
        "FROM transactions t JOIN merchants m ON m.id = t.merchant_id "
        "WHERE t.amount_cents < 0 ORDER BY t.merchant_id, t.booking_date",
    ).fetchall()

    groups: dict[int, dict[str, Any]] = {}
    for r in rows:
        g = groups.setdefault(r["merchant_id"], {"name": r["name"], "first_seen": r["first_seen"],
                                                 "dates": [], "amounts": []})
        g["dates"].append(date.fromisoformat(r["booking_date"]))
        g["amounts"].append(r["spend"])

    found = []
    for g in groups.values():
        if len(g["dates"]) < min_n:
            continue
        amounts = g["amounts"]
        mean = statistics.fmean(amounts)
        cv = statistics.pstdev(amounts) / mean if mean else 0.0  # dimensionless
        if cv >= cv_max:
            continue
        intervals = [(b - a).days for a, b in zip(g["dates"], g["dates"][1:], strict=False)]
        med = statistics.median_low(intervals)
        mad = statistics.median_low([abs(x - med) for x in intervals])
        if monthly_lo <= med <= monthly_hi and mad <= mad_max:
            period, per_year = "monthly", 12
        elif weekly_lo <= med <= weekly_hi and mad <= mad_max:
            period, per_year = "weekly", 52
        else:
            continue
        amount = statistics.median_low(amounts)  # int cents
        next_expected = g["dates"][-1] + timedelta(days=med)
        if next_expected < as_of - timedelta(days=med):
            continue  # looks cancelled — last charge more than one period overdue
        found.append({"merchant": g["name"], "amount_cents": amount, "period": period,
                      "interval_days": med, "next_expected": next_expected.isoformat(),
                      "annualized_cents": amount * per_year,
                      "first_seen": g["first_seen"], "occurrences": len(g["dates"])})
    found.sort(key=lambda s: -s["annualized_cents"])
    return found


# ── Charts (matplotlib, light-mode PNGs) ──────────────────────────────────


def _styled_axes(ax):
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK_2, labelsize=8)
    ax.yaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def _new_figure(height=4.2, nrows=1, sharex=False):
    fig, axes = plt.subplots(nrows, 1, figsize=(8, height), dpi=150, sharex=sharex)
    fig.patch.set_facecolor(SURFACE)
    for ax in (axes if nrows > 1 else [axes]):
        _styled_axes(ax)
    return fig, axes


def _save(fig, outdir: Path, name: str) -> str:
    path = outdir / name
    fig.tight_layout()
    fig.savefig(path, facecolor=SURFACE, bbox_inches="tight")
    plt.close(fig)
    return name


def chart_category_pareto(outdir: Path, pareto) -> str | None:
    if not pareto:
        return None
    labels = [p["category"] for p in pareto]
    euros = [p["spend_cents"] / 100 for p in pareto]  # display boundary only
    # No dual axis: € bars and cumulative % as stacked panels sharing x.
    fig, (ax1, ax2) = _new_figure(height=5.6, nrows=2, sharex=True)
    ax1.bar(labels, euros, color=BLUE, width=0.62)
    ax1.set_title("Category Pareto — trailing 90d spend", color=INK, fontsize=11, loc="left")
    ax1.set_ylabel("€", color=INK_2, fontsize=9)
    ax2.plot(labels, [p["cum_pct"] for p in pareto], color=BLUE, linewidth=2,
             marker="o", markersize=4)
    ax2.set_ylabel("cumulative %", color=INK_2, fontsize=9)
    ax2.set_ylim(0, 105)
    ax2.axhline(80, color=NEUTRAL, linewidth=1, linestyle="--")
    ax2.annotate("80%", xy=(len(labels) - 1, 80), color=INK_2, fontsize=8,
                 xytext=(0, 4), textcoords="offset points", ha="right")
    for ax in (ax1, ax2):
        plt.setp(ax.get_xticklabels(), rotation=40, ha="right")
    return _save(fig, outdir, "category_pareto.png")


def chart_merchant_pareto(outdir: Path, merchants) -> str | None:
    if not merchants:
        return None
    rows = merchants[::-1]  # biggest at top
    fig, ax = _new_figure(height=max(3.0, 0.28 * len(rows) + 1.2))
    ax.barh([m["merchant"] for m in rows], [m["spend_cents"] / 100 for m in rows],
            color=BLUE, height=0.62)
    ax.xaxis.grid(True, color=GRID, linewidth=0.8)
    ax.yaxis.grid(False)
    for i, m in enumerate(rows):
        ax.annotate(f" {fmt_eur(m['spend_cents'])}", xy=(m["spend_cents"] / 100, i),
                    va="center", color=INK_2, fontsize=7)
    ax.set_title(f"Top {len(merchants)} merchants — trailing 90d spend",
                 color=INK, fontsize=11, loc="left")
    ax.set_xlabel("€", color=INK_2, fontsize=9)
    return _save(fig, outdir, "merchant_pareto.png")


def chart_size_bands(outdir: Path, bands) -> str | None:
    if not any(b["count"] for b in bands):
        return None
    labels = [b["band"] for b in bands]
    # Count and € total are different scales → two panels, never twin axes.
    fig, (ax1, ax2) = _new_figure(height=5.2, nrows=2, sharex=True)
    ax1.bar(labels, [b["count"] for b in bands], color=BLUE, width=0.62)
    ax1.set_title("Transaction size bands — count vs total (small-taps check)",
                  color=INK, fontsize=11, loc="left")
    ax1.set_ylabel("count", color=INK_2, fontsize=9)
    for i, b in enumerate(bands):
        ax1.annotate(str(b["count"]), xy=(i, b["count"]), ha="center", va="bottom",
                     color=INK_2, fontsize=8)
    ax2.bar(labels, [b["total_cents"] / 100 for b in bands], color=BLUE, width=0.62)
    ax2.set_ylabel("total €", color=INK_2, fontsize=9)
    for i, b in enumerate(bands):
        ax2.annotate(fmt_eur(b["total_cents"]), xy=(i, b["total_cents"] / 100),
                     ha="center", va="bottom", color=INK_2, fontsize=8)
    return _save(fig, outdir, "size_bands.png")


def chart_weekday(outdir: Path, profile) -> str | None:
    days = profile["days"]
    if not any(d["spend_cents"] for d in days):
        return None
    fig, ax = _new_figure()
    colors = [AQUA if d["weekend"] else BLUE for d in days]
    ax.bar([d["day"] for d in days], [d["spend_cents"] / 100 for d in days],
           color=colors, width=0.62)
    for i, d in enumerate(days):  # value label on every bar
        ax.annotate(fmt_eur(d["spend_cents"]), xy=(i, d["spend_cents"] / 100),
                    ha="center", va="bottom", color=INK_2, fontsize=8)
    ax.set_title("Spend by day of week — trailing 90d", color=INK, fontsize=11, loc="left")
    ax.set_ylabel("€", color=INK_2, fontsize=9)
    handles = [plt.Rectangle((0, 0), 1, 1, color=BLUE), plt.Rectangle((0, 0), 1, 1, color=AQUA)]
    ax.legend(handles, ["weekday", "weekend"], frameon=False, fontsize=8, labelcolor=INK_2)
    return _save(fig, outdir, "weekday_profile.png")


def chart_burn_rate(outdir: Path, daily, budget_cents: int) -> str | None:
    if not daily:
        return None
    by_month: dict[str, dict[int, int]] = {}
    for iso_day, spend in daily:
        d = date.fromisoformat(iso_day)
        by_month.setdefault(iso_day[:7], {})[d.day] = spend
    months = sorted(by_month)[-len(SERIES):]  # up to 4 months, colored in order

    fig, ax = _new_figure(height=4.6)
    ref_days = 30  # longest plotted month, for the straight-line reference
    for idx, month in enumerate(months):
        days_in_month = calendar.monthrange(int(month[:4]), int(month[5:7]))[1]
        ref_days = max(ref_days, days_in_month)
        xs, ys, running = [], [], 0
        for day in range(1, days_in_month + 1):
            running += by_month[month].get(day, 0)
            xs.append(day)
            ys.append(running / 100)
        last_day = max(by_month[month])
        xs, ys = xs[:last_day], ys[:last_day]
        ax.plot(xs, ys, color=SERIES[idx], linewidth=2, label=month)
        ax.annotate(f" {month}", xy=(xs[-1], ys[-1]), color=SERIES[idx], fontsize=8,
                    va="center")  # end labels, so the line hue isn't load-bearing
    # Even-spend reference to the END of the longest plotted month, not a fixed
    # day 31 — a fixed 31 makes a 28-day February look 10% under budget.
    ax.plot([1, ref_days], [0, budget_cents / 100], color=NEUTRAL, linewidth=1.4,
            linestyle="--", label="straight-line budget")
    ax.set_title("Cumulative spend per month vs straight-line budget",
                 color=INK, fontsize=11, loc="left")
    ax.set_xlabel("day of month", color=INK_2, fontsize=9)
    ax.set_ylabel("cumulative €", color=INK_2, fontsize=9)
    ax.legend(frameon=False, fontsize=8, labelcolor=INK_2)
    return _save(fig, outdir, "burn_rate.png")


# ── Markdown rendering ────────────────────────────────────────────────────


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines)


def render_subscriptions_md(recurring, as_of: date, min_n: int = 3) -> str:
    lines = [f"# Recurring spend — detected {as_of.isoformat()}", ""]
    if not recurring:
        lines.append(f"No recurring merchants detected yet (need ≥{min_n} regular occurrences).")
        return "\n".join(lines) + "\n"
    total_annual = sum(s["annualized_cents"] for s in recurring)
    lines.append(f"**{len(recurring)} recurring merchant(s) · "
                 f"{fmt_eur(total_annual)}/year if all continue.**\n")
    lines.append(_md_table(
        ["Merchant", "Amount", "Period", "Next expected", "Annualized", "First seen", "n"],
        [[s["merchant"], fmt_eur(s["amount_cents"]), s["period"], s["next_expected"],
          fmt_eur(s["annualized_cents"]), s["first_seen"] or "—", str(s["occurrences"])]
         for s in recurring]))
    return "\n".join(lines) + "\n"


def render_report_md(data: dict[str, Any]) -> str:
    start, end = data["window"]
    total = data["spend_cents"]
    lines = [
        f"# Sentinel expense report — trailing {data['window_days']} days ({start} → {end})",
        "",
        f"Generated {data['generated_at']} · {data['txns']} spend transactions · "
        f"total spend {fmt_eur(total)} (Transfers/Income excluded, SPEC §3).",
        "",
        "## 1. Category Pareto",
        "",
        "![category pareto](category_pareto.png)",
        "",
        _md_table(["Category", "Spend", "% of total", "Cumulative %", "Txns"],
                  [[c["category"], fmt_eur(c["spend_cents"]), f"{c['pct']:.1f}%",
                    f"{c['cum_pct']:.1f}%", str(c["txns"])] for c in data["categories"]]),
        "",
        "## 2. Merchant Pareto (top 25)",
        "",
        "![merchant pareto](merchant_pareto.png)",
        "",
        _md_table(["Merchant", "Category", "Spend", "% of total", "Txns"],
                  [[m["merchant"], m["category"], fmt_eur(m["spend_cents"]),
                    f"{m['pct']:.1f}%", str(m["txns"])] for m in data["merchants"]]),
        "",
        "## 3. Size-band histogram (small-taps hypothesis)",
        "",
        "![size bands](size_bands.png)",
        "",
        _md_table(["Band", "Count", "Total"],
                  [[b["band"], str(b["count"]), fmt_eur(b["total_cents"])]
                   for b in data["size_bands"]]),
        "",
        "## 4. Weekday vs weekend",
        "",
        "![weekday profile](weekday_profile.png)",
        "",
        f"Weekday total {fmt_eur(data['weekday']['weekday_cents'])} · "
        f"weekend total {fmt_eur(data['weekday']['weekend_cents'])}.",
        "",
        _md_table(["Day", "Spend", "Txns"],
                  [[d["day"], fmt_eur(d["spend_cents"]), str(d["txns"])]
                   for d in data["weekday"]["days"]]),
        "",
        "## 5. Burn rate",
        "",
        "![burn rate](burn_rate.png)",
        "",
        f"Straight-line reference: {fmt_eur(data['budget_cents'])}/month "
        f"({data['budget_source']}).",
        "",
        "## 6. Recurring spend",
        "",
        f"{len(data['recurring'])} recurring merchant(s) detected — see "
        "[subscriptions.md](subscriptions.md).",
        "",
        "## 7. Gap reconciliation (the point of all this)",
        "",
        "`residual = inflows − fixed − categorized variable`; by identity it equals "
        "`uncategorized + transfers out + net saved` — those columns name the gap.",
        "",
        _md_table(
            ["Month", "Inflows", "Fixed", "Variable", "Uncategorized", "Transfers out",
             "Net saved", "Residual", f"< {fmt_eur(RESIDUAL_GATE_CENTS)}?"],
            [[m["month"] + ("" if m["full_month"] else " (partial)"),
              fmt_eur(m["inflows_cents"]), fmt_eur(m["fixed_cents"]),
              fmt_eur(m["variable_cents"]), fmt_eur(m["uncategorized_cents"]),
              fmt_eur(m["transfers_out_cents"]), fmt_eur(m["net_cents"]),
              fmt_eur(m["residual_cents"]),
              ("✅" if m["gate_pass"] else "❌") if m["full_month"] else "—"]
             for m in data["months"]]),
        "",
        "",
        "## 8. Month-over-month — spend by bucket",
        "",
        (_md_table(["Bucket", data["mom"]["prev"], data["mom"]["curr"], "Δ€", "Δ%"],
                   [[r["bucket"], fmt_eur(r["prev_cents"]), fmt_eur(r["curr_cents"]),
                     fmt_eur(r["delta_cents"]), f"{r['delta_pct']:+d}%"]
                    for r in data["mom"]["rows"]]) if data["mom"]["rows"]
         else "_No two comparable full months yet._"),
        "",
    ]
    full_months = [m for m in data["months"] if m["full_month"]]
    if full_months:
        worst = max(abs(m["residual_cents"]) for m in full_months)
        verdict = ("✅ GATE PASS" if all(m["gate_pass"] for m in full_months)
                   else "❌ GATE FAIL")
        lines.append(f"**{verdict}** — worst full-month |residual| {fmt_eur(worst)} "
                     f"vs {fmt_eur(RESIDUAL_GATE_CENTS)} target. "
                     "If failing: run `make categorize`, then chase the Uncategorized "
                     "and Transfers-out columns above.")
    else:
        lines.append("_No full calendar month in the window yet — residual verdict pending._")
    return "\n".join(lines) + "\n"


# ── Orchestration ─────────────────────────────────────────────────────────


def month_over_month(conn, as_of: date) -> dict[str, Any]:
    """
    Return spend per bucket for the last full month versus the one before, with
    deltas.
    """
    from .categorize import BUCKETS, bucket

    def _by_bucket(month_key: str) -> dict[str, int]:
        rows = conn.execute(
            "SELECT category, SUM(-amount_cents) AS c FROM v_transactions_categorized "
            "WHERE amount_cents < 0 AND category NOT IN ('Income', 'Transfers') "
            "AND strftime('%Y-%m', booking_date) = ? GROUP BY category", (month_key,),
        ).fetchall()
        out = dict.fromkeys(BUCKETS, 0)
        for r in rows:
            out[bucket(r["category"])] += r["c"]
        return out

    first = as_of.replace(day=1)
    curr_key = (first - timedelta(days=1)).strftime("%Y-%m")
    prev_key = ((first - timedelta(days=1)).replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    curr, prev = _by_bucket(curr_key), _by_bucket(prev_key)
    rows = []
    for b in BUCKETS:
        c, p = curr[b], prev[b]
        if not c and not p:
            continue
        pct = round((c - p) * 100 / p) if p else (100 if c else 0)
        rows.append({"bucket": b, "curr_cents": c, "prev_cents": p,
                     "delta_cents": c - p, "delta_pct": pct})
    return {"curr": curr_key, "prev": prev_key, "rows": rows}


def run_reports(conn, cfg: dict[str, Any], as_of: date | None = None,
                dry_run: bool = False) -> dict[str, Any]:
    rcfg = cfg.get("reports") or {}
    as_of = as_of or datetime.now(db.TZ).date()
    window_days = int(rcfg.get("window_days", 90))
    start, end = _window(as_of, window_days)
    outdir = Path(rcfg.get("output_dir", "reports"))

    months = reconciliation(conn, start, end)
    full_totals = [m["fixed_cents"] + m["variable_cents"] + m["uncategorized_cents"]
                   for m in months if m["full_month"]]
    configured = rcfg.get("monthly_budget_cents")
    if configured:
        budget, budget_source = int(configured), "config reports.monthly_budget_cents"
    elif full_totals:
        budget, budget_source = sum(full_totals) // len(full_totals), "trailing full-month average"
    else:
        budget, budget_source = 0, "no full month yet"

    spend_row = conn.execute(
        f"SELECT COUNT(*) AS n, COALESCE(SUM(-amount_cents), 0) AS total "
        f"FROM v_transactions_categorized WHERE {_spend_where()}", (start, end)).fetchone()

    data: dict[str, Any] = {
        "window": (start, end), "window_days": window_days,
        "generated_at": db.now_iso(), "txns": spend_row["n"], "spend_cents": spend_row["total"],
        "categories": category_pareto(conn, start, end),
        "merchants": merchant_pareto(conn, start, end),
        "size_bands": size_bands(conn, start, end),
        "weekday": weekday_profile(conn, start, end),
        "daily": daily_spend(conn, start, end),
        "months": months,
        "mom": month_over_month(conn, as_of),
        "recurring": detect_recurring(conn, cfg, as_of),
        "budget_cents": budget, "budget_source": budget_source,
        "files": [],
    }

    if dry_run:
        log.info("dry-run: would write EXPENSE_REPORT.md, subscriptions.md and charts to %s/", outdir)
    else:
        outdir.mkdir(parents=True, exist_ok=True)
        charts = [
            chart_category_pareto(outdir, data["categories"]),
            chart_merchant_pareto(outdir, data["merchants"]),
            chart_size_bands(outdir, data["size_bands"]),
            chart_weekday(outdir, data["weekday"]),
            chart_burn_rate(outdir, data["daily"], budget),
        ]
        min_n = int((rcfg.get("recurring") or {}).get("min_occurrences", 3))
        (outdir / "EXPENSE_REPORT.md").write_text(render_report_md(data), encoding="utf-8")
        (outdir / "subscriptions.md").write_text(
            render_subscriptions_md(data["recurring"], as_of, min_n), encoding="utf-8")
        data["files"] = ["EXPENSE_REPORT.md", "subscriptions.md"] + [c for c in charts if c]
        log.info("wrote %s to %s/", ", ".join(data["files"]), outdir)

    full = [m for m in data["months"] if m["full_month"]]
    if full:
        worst = max(abs(m["residual_cents"]) for m in full)
        log.info("gap residual: worst full month %s (gate %s at %s)",
                 fmt_eur(worst),
                 "PASS" if all(m["gate_pass"] for m in full) else "FAIL",
                 fmt_eur(RESIDUAL_GATE_CENTS))
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the monthly expense report (SPEC §4).")
    parser.add_argument("--dry-run", action="store_true", help="compute + log, write no files")
    parser.add_argument("--as-of", default=None, metavar="ISO_DATE",
                        help="window end date (default: today, Europe/Dublin)")
    parser.add_argument("--db", default=None)
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    cfg = db.load_config(args.config)
    conn = db.connect(args.db or cfg.get("db_path", "ledger.db"))
    try:
        db.init_db(conn)
        as_of = date.fromisoformat(args.as_of) if args.as_of else None
        run_reports(conn, cfg, as_of=as_of, dry_run=args.dry_run)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
