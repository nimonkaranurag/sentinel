# ADR 002 — Money is integer cents, end to end

**Status:** accepted · **Date:** 2026-07-15

## Context

Amounts arrive as decimal strings from two sources (the Enable Banking API and
AIB CSV exports), flow through categorization and aggregation, and surface as
euro figures in pushes and reports. Binary floating point cannot represent most
decimal cent values exactly, so `0.1 + 0.2 != 0.3`; summed over a month of
transactions, float drift silently corrupts totals — and this tool's entire job
is to be a trustworthy sensor on real money.

## Decision

Money is integer cents everywhere, and floats are refused near amounts.

- `db.to_cents` parses decimal *strings* only. It rejects `float` and bare `int`
  (ambiguous euros-or-cents), decimal commas, and >2 decimal places, rather than
  guessing.
- Amount columns live in SQLite **STRICT** tables, so the database boundary
  physically refuses a non-integer amount.
- All math — safe-to-spend, the residual gate, Paretos, month-over-month — is
  integer arithmetic. Floats appear only in dimensionless statistics (percentages,
  coefficients of variation) and at the matplotlib display boundary.
- Currency is EUR; a non-EUR row is quarantined at ingest rather than summed at
  face value.

## Consequences

- **Exactness.** Totals are correct to the cent, and the reconciliation identity
  (`residual = uncategorized + transfers_out + net_saved`) holds exactly, which
  is what lets the residual gate be a real test rather than an approximation.
- **Loud failure.** An unparseable or ambiguous amount raises at ingest instead
  of silently coercing — money corruption surfaces immediately.
- **Ergonomic cost.** Callers must format for display (`fmt_eur`) and never do
  arithmetic on the display string. Property tests pin the `to_cents ∘ fmt_eur`
  round-trip so this stays true.
