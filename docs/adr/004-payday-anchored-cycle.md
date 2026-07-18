# ADR 004 — Payday-anchored safe-to-spend, with a manual `/paidtoday` override

**Status:** accepted · **Date:** 2026-07-18

## Context

Safe-to-spend (SPEC §4) originally spread the discretionary pool over the
**calendar month**: `days_left = days_in_month − day + 1`, resetting on the 1st.
But the owner's money does not arrive on the 1st — salary lands on a fixed
day-of-month (the 23rd), and the cash on hand between now and the next payday is
what actually has to last. A calendar-month cycle therefore counts the wrong
days (on the 18th it reported "14 days" to month-end when payday was 5 days out)
and resets the pool mid-cycle, so the one honest daily number was measuring the
wrong window.

Payday also moves: when the 23rd falls on a weekend or bank holiday the bank pays
early (a Sunday 23rd → the Friday 21st). Chasing that automatically would mean
shipping a real-time bank-holiday calendar for one jurisdiction and keeping it
current — fragile, and a new maintenance surface for a single-user tool. It also
edges toward the kind of external-calendar dependency the project avoids.

## Decision

**Anchor the cycle to payday, not the month.** `controller.cycle_for` computes
the cycle containing `as_of` from a nominal `payday.day_of_month` (default 23):
`cycle_start` is the payday on or before today, `next_payday` the one after, and
`days_left = next_payday − as_of` (always ≥ 1, so there is no divide-by-zero at
the boundary). Discretionary spend and unlabeled inflows are summed over
`cycle_start → as_of`, so the pool resets on payday and `safe_today = max(0,
(pool − cycle-to-date discretionary) ÷ days_left)`.

**Handle early/late pay by hand, not by calendar.** There is no weekday shifting
and no holiday table. When pay lands off its nominal day, the owner runs
`/paidtoday` (optionally `/paidtoday YYYY-MM-DD`), which writes a
`payday_actual:<YYYY-MM>` key in `state` for that cycle's month. `cycle_for`
honours the override in place of the nominal day, so the cycle — and the pool
reset — rolls to the real date. The mapping from a logged date to the cycle it
belongs to is "nearest scheduled payday" (`controller.cycle_month_for`), which is
unambiguous for the few-days-early/late reality it exists for.

`graduation_surplus` / `month_surplus` stay **calendar-month** — the monthly
report's surplus is a month-over-month figure and deliberately independent of the
daily cycle.

## Consequences

- **The daily number matches the real runway.** "Days left" counts to payday and
  the pool resets when money actually arrives, so safe-to-spend stops flattering
  or starving the owner around the 1st and the 23rd.
- **No calendar dependency.** No bank-holiday table to ship or maintain, no new
  dependency, and no wrong-guess risk — the single user corrects a moved payday
  in one message. This is the same "keep it local and deterministic" stance as
  ADR 001 (no LLM).
- **A new per-cycle state key.** `payday_actual:<YYYY-MM>` joins the `state`
  inventory (SPEC §5, `state_keys.py`). It is idempotent — re-logging the same
  day is a no-op write — and only ever overrides one cycle.
- **Config, not code.** `payday.day_of_month` is the single knob; nothing about
  the cycle is hardcoded (CLAUDE.md conventions).
- **SPEC change.** The §4 formula moves from "MTD discretionary ÷ days_left" to
  "cycle-to-date discretionary ÷ days-to-next-payday"; the pinning tests move with
  it (`tests/test_payday.py`, `tests/test_notify.py`).
