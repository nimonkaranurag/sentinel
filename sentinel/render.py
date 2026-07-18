"""
Read-only rendering for the Telegram surface (SPEC §4/§7).

Every function turns ledger and config data into a string or a keyboard dict. It
reads the ledger but performs no writes, no network, and no state mutation; those
live in commands.py, notify.py, and alerts.py. Keeping this layer side-effect
free lets the digest test assert, against a plain string, that no account id can
reach the push.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from . import bills, categorize, controller, db
from .db import fmt_eur
from .normalize import display_merchant

HELP_TEXT = (
    "Sentinel commands:\n"
    "/today — what's safe to spend today\n"
    "/status — this pay cycle's spend by bucket + safe-to-spend\n"
    "/cat <name> — one category + this month's transactions (with refs)\n"
    "/sync — pull the bank now (exempt from the daily allowance)\n"
    "/recat <ref> <category> — correct a transaction AND its merchant\n"
    "/date <ref> — mark one transaction as Dates (merchant untouched)\n"
    "/paid-today [date] — log the day your salary landed (e.g. early on a "
    "weekend/bank holiday) so the pay cycle rolls to it"
)

# Categories offered in the inline reclassify grid (everything but the sentinel).
RELABEL_CHOICES = tuple(c for c in categorize.TAXONOMY if c != "Uncategorized")

# Friendlier display labels for the money buckets (SPEC §3); others print as-is.
BUCKET_LABELS = {"FoodDelivery": "Food delivery"}


# ── Small text helpers ──────────────────────────────────────────────────────


def plural(n: int, word: str, plural_word: str | None = None) -> str:
    """
    Format a count with its noun, pluralised: plural(1, "day") -> "1 day",
    plural(3, "day") -> "3 days".
    """
    return f"{n} {word}" if n == 1 else f"{n} {plural_word or word + 's'}"


def fmt_day(d: date) -> str:
    """
    Format a date for the chat surface as e.g. "Wed 23 Jul" (no leading zero,
    locale-independent).
    """
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"


# ── Daily / status ──────────────────────────────────────────────────────────


def traffic_light(cfg: dict[str, Any], safe_cents: int) -> str:
    thresholds = cfg.get("thresholds") or {}
    if safe_cents > int(thresholds.get("green_cents", 2_500)):
        return "🟢"
    if safe_cents < int(thresholds.get("red_cents", 1_000)):
        return "🔴"
    return "🟡"


def _payday_line(s: dict[str, Any]) -> str:
    """
    Return the "N days to payday (Wed 23 Jul)" clause shared by /today and
    /status, noting when the current cycle started from a logged /paid-today.
    """
    days = plural(s["days_left"], "day")
    when = fmt_day(date.fromisoformat(s["next_payday"]))
    early = " · payday logged early" if s["cycle_start_source"] == "logged" else ""
    return f"{days} to payday ({when}){early}"


def compose_daily(conn, cfg: dict[str, Any], as_of: date) -> str:
    """
    Compose the /today reply and the daily 08:00 push: the headline number, a
    plain-English pool status, and where you are in the pay cycle.
    """
    s = controller.safe_to_spend(conn, cfg, as_of)
    badge = traffic_light(cfg, s["safe_today_cents"])
    remaining = s["remaining_cents"]
    if remaining >= 0:
        pool_status = f"{fmt_eur(remaining)} left in your pool · {_payday_line(s)}"
    else:
        pool_status = f"⚠️ {fmt_eur(-remaining)} over your pool · {_payday_line(s)}"
    spent_line = (f"Pool {fmt_eur(s['pool_cents'])} · {fmt_eur(s['discretionary_spent_cents'])} "
                  f"spent since {fmt_day(date.fromisoformat(s['cycle_start']))}")
    return (f"Safe to spend today: {fmt_eur(s['safe_today_cents'])}  {badge}\n\n"
            f"{pool_status}\n{spent_line}")


def status_text(conn, cfg: dict[str, Any], as_of: date) -> str:
    """
    Compose /status: the pay-cycle window, spend by bucket, the discretionary
    pool, any holds (unlabeled inflow / quarantine), and today's number.
    """
    s = controller.safe_to_spend(conn, cfg, as_of)
    cycle_start = date.fromisoformat(s["cycle_start"])
    early = "  ·  payday logged early" if s["cycle_start_source"] == "logged" else ""
    lines = [f"📊 This pay cycle{early}",
             f"{fmt_day(cycle_start)} → {fmt_day(as_of)}  ·  {_payday_line(s)}", ""]

    spent = [(BUCKET_LABELS.get(b, b), s["by_bucket_cents"].get(b, 0))
             for b in categorize.BUCKETS if s["by_bucket_cents"].get(b, 0)]
    if spent:
        width = max(len(label) for label, _ in spent)
        lines.append("Spent this cycle:")
        lines += [f"  {label:<{width}}  {fmt_eur(cents)}" for label, cents in spent]
        lines.append("")

    remaining = s["remaining_cents"]
    tail = (f"{fmt_eur(remaining)} left" if remaining >= 0
            else f"⚠️ {fmt_eur(-remaining)} over")
    lines.append(f"Discretionary pool: {fmt_eur(s['discretionary_spent_cents'])} spent of "
                 f"{fmt_eur(s['pool_cents'])} — {tail}")

    if s.get("unlabeled_inflow_count"):
        lines += ["",
                  f"⚠️ {fmt_eur(s['unlabeled_inflow_cents'])} unlabeled inflow "
                  f"({s['unlabeled_inflow_count']}) is held out of the pool.",
                  "   /recat it to Income/Transfers, or to its merchant category if it's a refund."]
    quarantined = db.quarantine_count(conn)
    if quarantined:
        lines += ["", f"⚠️ {plural(quarantined, 'row')} quarantined (non-EUR or "
                  "sign-ambiguous) — not counted in any total; see the ingest log."]

    lines += ["", f"Safe to spend today: {fmt_eur(s['safe_today_cents'])}  "
              f"{traffic_light(cfg, s['safe_today_cents'])}"]
    return "\n".join(lines)


def sync_reply(inserted: int, submitted: int, fired: int) -> str:
    """
    Plain-English summary of a /sync: how many new transactions landed (out of
    how many the bank returned) and how many alerts fired.
    """
    if inserted:
        lines = ["✅ Bank sync done.",
                 f"• {plural(inserted, 'new transaction')} (checked {submitted})"]
    else:
        lines = ["✅ Bank sync done — you're already up to date.",
                 f"• No new transactions (checked {submitted})"]
    lines.append(f"• {plural(fired, 'new alert')} — see above ⬆️" if fired
                 else "• No new alerts.")
    return "\n".join(lines)


# ── Category / transaction views ────────────────────────────────────────────


def resolve_category(name: str) -> str | None:
    wanted = name.strip().lower()
    for cat in categorize.TAXONOMY:
        if cat.lower() == wanted:
            return cat
    return None


def cat_text(conn, cfg: dict[str, Any], name: str, as_of: date) -> str:
    cat = resolve_category(name)
    if cat is None:
        return f"Unknown category {name!r}. Valid: {', '.join(categorize.TAXONOMY)}"
    month_start, _, _ = controller.month_bounds(as_of)
    rows = conn.execute(
        "SELECT id, booking_date, merchant_raw, amount_cents FROM v_transactions_categorized "
        "WHERE category = ? AND booking_date >= ? AND booking_date <= ? AND amount_cents < 0 "
        "ORDER BY booking_date DESC LIMIT 8",
        (cat, month_start.isoformat(), as_of.isoformat()),
    ).fetchall()
    total = conn.execute(
        "SELECT COALESCE(SUM(-amount_cents), 0) FROM v_transactions_categorized "
        "WHERE category = ? AND booking_date >= ? AND booking_date <= ? AND amount_cents < 0",
        (cat, month_start.isoformat(), as_of.isoformat()),
    ).fetchone()[0]
    lines = [f"{cat} ({categorize.bucket(cat)}): {fmt_eur(total)} this month"]
    lines += [f"{r['id'][:8]} · {r['booking_date']} · {display_merchant(r['merchant_raw']) or '—'} · "
              f"{fmt_eur(-r['amount_cents'])}" for r in rows]
    lines.append("(use the 8-char ref with /recat or /date)" if rows else "No transactions this month.")
    return "\n".join(lines)


# ── Alert keyboards ─────────────────────────────────────────────────────────


def alert_keyboard(txn_id: str) -> dict[str, Any]:
    ref = txn_id[:12]
    return {"inline_keyboard": [[{"text": "✓ fine", "callback_data": f"ok:{ref}"},
                                 {"text": "Reclassify…", "callback_data": f"rc:{ref}"}]]}


def reclass_keyboard(ref: str) -> dict[str, Any]:
    btns = [{"text": c, "callback_data": f"set:{ref}:{c}"} for c in RELABEL_CHOICES]
    return {"inline_keyboard": [btns[i:i + 3] for i in range(0, len(btns), 3)]}


# ── Weekly plan + digest ────────────────────────────────────────────────────


def compose_weekly_plan(conn, cfg: dict[str, Any], as_of: date) -> str:
    """
    Compose the Monday plan: this week's slice of the remaining discretionary
    pool.
    """
    s = controller.safe_to_spend(conn, cfg, as_of)
    weeks_left = max(1, (s["days_left"] + 6) // 7)
    week_budget = max(0, s["remaining_cents"] // weeks_left)
    return (f"🗓️ This week: {fmt_eur(week_budget)} discretionary "
            f"({fmt_eur(s['remaining_cents'])} of your pool left, {s['days_left']} days).")


def build_digest_aggregates(conn, cfg: dict[str, Any], as_of: date) -> dict[str, Any]:
    """
    Compute the pre-aggregated numbers for the weekly digest template.
    """
    exclude = controller.unlabeled_inflow_exclude_cents(cfg)
    week_start = as_of - timedelta(days=6)
    prev_start, prev_end = week_start - timedelta(days=7), week_start - timedelta(days=1)
    week = controller.spend_by_bucket(conn, week_start, as_of, inflow_exclude_cents=exclude)
    prev = controller.spend_by_bucket(conn, prev_start, prev_end, inflow_exclude_cents=exclude)
    top = conn.execute(
        "SELECT merchant_raw, category, -amount_cents AS spend_cents "
        "FROM v_transactions_categorized WHERE amount_cents < 0 "
        "AND category NOT IN ('Transfers', 'Income') "
        "AND booking_date >= ? AND booking_date <= ? ORDER BY amount_cents ASC LIMIT 3",
        (week_start.isoformat(), as_of.isoformat()),
    ).fetchall()
    status = controller.safe_to_spend(conn, cfg, as_of)
    return {
        "week": {"start": week_start.isoformat(), "end": as_of.isoformat(),
                 "spend_by_bucket_cents": week},
        "previous_week_spend_by_bucket_cents": prev,
        "delta_by_bucket_cents": {b: week.get(b, 0) - prev.get(b, 0)
                                  for b in sorted(set(week) | set(prev))},
        "top_3_largest_spends": [{"merchant": r["merchant_raw"], "category": r["category"],
                                  "amount_cents": r["spend_cents"]} for r in top],
        "safe_to_spend_today_cents": status["safe_today_cents"],
        "discretionary_spent_cents": status["discretionary_spent_cents"],
        "pool_cents": status["pool_cents"],
        "graduation": controller.graduation_surplus(conn, cfg, as_of),
    }


def _digest_extras(aggregates: dict[str, Any]) -> list[str]:
    """
    Return the graduation-surplus line for the digest.
    """
    grad = aggregates["graduation"]
    verdict = ("🎓 above target — the family transfer can start winding down"
               if grad["met"] else "below target")
    return [f"Last month ({grad['month']}) surplus: {fmt_eur(grad['surplus_cents'])} "
            f"(target {fmt_eur(grad['target_cents'])}) — {verdict}"]


def render_digest(aggregates: dict[str, Any], extras: list[str]) -> str:
    """
    Render the weekly digest as a template over the aggregates.
    """
    wk = aggregates["week"]
    week = wk["spend_by_bucket_cents"]
    prev = aggregates["previous_week_spend_by_bucket_cents"]
    week_spend = sum(v for v in week.values() if v > 0)
    delta = week_spend - sum(v for v in prev.values() if v > 0)
    arrow = "▲" if delta > 0 else ("▼" if delta < 0 else "▬")
    lines = [f"📊 Week {wk['start']} — {wk['end']}",
             f"Spent {fmt_eur(week_spend)}  ({arrow} {fmt_eur(abs(delta))} vs prior week)"]
    for b, d in sorted(aggregates["delta_by_bucket_cents"].items(), key=lambda kv: -abs(kv[1]))[:3]:
        if d:
            lines.append(f"  {b}: {'+' if d > 0 else '−'}{fmt_eur(abs(d))}")
    if aggregates["top_3_largest_spends"]:
        lines.append("Top spends:")
        lines += [f"  {fmt_eur(s['amount_cents'])} · {display_merchant(s['merchant']) or '—'} ({s['category']})"
                  for s in aggregates["top_3_largest_spends"]]
    lines.append(f"Safe to spend today: {fmt_eur(aggregates['safe_to_spend_today_cents'])}")
    if extras:
        lines.append("")
        lines += extras
    return "\n".join(lines)


def digest_text(conn, cfg: dict[str, Any], as_of: date) -> str:
    """
    Return the full weekly digest string (aggregates plus bills checklist).
    """
    aggregates = build_digest_aggregates(conn, cfg, as_of)
    text = render_digest(aggregates, _digest_extras(aggregates))
    return text + "\n\n" + bills.render_checklist(conn, cfg, as_of)
