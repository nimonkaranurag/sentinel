# ADR 001 — No LLM anywhere in the pipeline

**Status:** accepted · **Date:** 2026-07-15

## Context

Sentinel categorizes bank transactions and generates a daily digest, reports, and
alerts. The obvious modern reflex is to reach for an LLM to categorize merchants
and to write copy. This tool watches one person's real money over PSD2 account
access, and every euro figure it shows drives a spending decision.

## Decision

No LLM, no third-party AI, anywhere. Categorization is a deterministic cascade —
`normalize → owner merchant map → regex rules` — and a never-seen merchant stays
`Uncategorized` until the owner labels it (two taps in Telegram). The digest,
reports, and alert copy are templates over integer arithmetic. There is no
`llm.py`, no `openai`/`anthropic` dependency, no `llm_calls` table, and
`merchants.categorized_by` is constrained to `dict | regex | manual`.

## Consequences

- **Determinism and auditability.** The same ledger always produces the same
  numbers and the same labels; every figure is traceable to rows and rules, not
  to a sampled model. Correctness claims are pinnable by tests, and they are.
- **No hallucinated money.** A model that silently miscategorizes or fabricates a
  merchant would corrupt safe-to-spend and the residual gate. A regex that does
  not match simply leaves the row `Uncategorized` — a visible, correctable state.
- **Privacy.** No merchant names, amounts, or account data leave the machine
  except to Enable Banking and Telegram. There is no inference provider in scope.
- **Cost of the choice.** Novel merchants need a one-time human label. This is
  acceptable: the merchant map grows monotonically, and labeling is two taps.
- **Reversal cost.** Introducing an LLM later would mean a new dependency, a new
  data-egress path, and new non-determinism in the money core — a deliberate,
  documented reversal, never an incidental import.
