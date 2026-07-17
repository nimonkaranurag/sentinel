# Sentinel

Real-time-ish **spending discipline over Telegram** for one person's own bank
account. It watches new transactions as they book, alerts when a spending policy
is breached, lets you re-label a charge in two taps, and tracks recurring bills.
Everything runs **locally** — no LLM, no third-party AI. `SPEC.md` is the source
of truth.

## What it does (the five features)

1. **Policy alerts** (`policies.py` + `alerts.py`) — each new charge is checked
   against `policies.yaml`; a match over its monthly cap fires escalating,
   templated copy: `🔴 Deliveroo €24.90 — 9th this month. €231.00 of your €150.00
   food-delivery cap. €2,772.00/yr at this pace.` A durable watermark in `state`
   means a crash between ingest and send replays, never silently drops, the batch.
2. **Two-tap relabel** (`commands.py`) — every alert carries `[✓ fine]
   [Reclassify…]` inline buttons. Reclassifying writes a `category_override` +
   teaches the merchant map, recomputes, and edits the message in place. Backed
   by an append-only `events` table and `processed_callbacks`: at-least-once
   delivery with idempotent effects (a retried tap changes nothing twice).
3. **Bills checklist** (`bills.py`) — a registry of expected recurring charges;
   alerts on **late** (past the due date + grace → bounced direct debit) and
   **drift** (amount outside tolerance → quiet price hike). Lateness is measured
   against a real due date that rolls across month boundaries, so end-of-month
   bills are detectable. Rendered in the weekly report.
4. **Safe-to-spend** (`controller.py`) — one daily number: `(discretionary pool
   − month-to-date discretionary spend) ÷ days left`. Small refunds net against
   spend; a large unlabeled inflow (an unmapped transfer) is held out of the pool
   until you label it, so it can't inflate the number.
5. **Weekly report + daily push** (`notify.py`) — the 08:00 safe-to-spend push,
   a Monday plan, and a deterministic Sunday digest (week vs prior, top spends,
   surplus line, bills checklist). Monthly `EXPENSE_REPORT.md` via `reports.py`.

## Pipeline

```
Enable Banking API ─┐
AIB CSV (fallback) ─┼─► ingest / csv_import ─► ledger.db ─► normalize → categorize ─► policies ─► telegram
authorize (1-time consent) ┘            (money core: STRICT, integer cents, idempotent)  (alerts + relabel + bills)
```

The Telegram surface is split by responsibility: `telegram.py` (transport +
token redaction), `render.py` (pure text/keyboards), `alerts.py` (policy
alerts), `commands.py` (command + callback router), `notify.py` (the cron
orchestration + CLI).

**Categorization** is a two-tier partition: fine **sub-labels** (Dining,
Coffee/Snacks, …) roll up into 6 **buckets** (`Income · Transfers · Fixed ·
Groceries · FoodDelivery · Other`) used for all money math. No LLM — a never-seen
merchant stays Uncategorized (the discretionary pool) until you label it via the
relabel loop or `rules.local.yaml`.

## Setup

1. **Install the git hook (once):** `make hooks` symlinks `scripts/pre-commit`
   into `.git/hooks`, then `cp .pii-patterns.example .pii-patterns` and fill in
   your own identifiers (that file is git-ignored — never commit it).
2. **Access (one-time):** `python -m sentinel.authorize` completes the Enable
   Banking consent handshake and writes the account uids to `state` (re-run for
   the ≤180-day re-auth). See `docs/RAILS.md`.
3. **Secrets** live in `.env` (git-ignored): `ENABLE_BANKING_APP_ID`,
   `ENABLE_BANKING_PRIVATE_KEY_PATH` (path only), `TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_CHAT_ID`. Copy `.env.example`.
4. **Owner-specific config** goes in git-ignored local files: `rules.local.yaml`
   (employer/landlord/family patterns → categories) and `bills.local.yaml`
   (your recurring bills). Tunables — the discretionary `pool_monthly_cents`,
   policy caps, thresholds, the unlabeled-inflow threshold — live in
   `config.yaml` / `policies.yaml`.

## Make targets

`make init` · `hooks` · `backfill` · `categorize` (`relink` after a normalizer
change) · `report` · **`poll`** (ingest + categorize + policy alerts — the cron
path) · `notify` (daily push + commands) · **`plan`** (Monday) · `digest`
(weekly) · `backup` (safe `sqlite .backup`, never `cp` a live WAL DB) · `test`.
There is deliberately no alert-less `ingest` target — `poll` is the one ingest
path so a charge can't book without being checked.

## Commands (Telegram, owner chat only)

`/today` `/status` `/cat <name>` `/sync` `/recat <ref> <category>` `/date <ref>`,
plus the inline-keyboard relabel flow on every alert. Every command and tap is
authorized by the **sender's** id, not the chat id.

## Money & safety

Integer cents everywhere (STRICT tables refuse non-integers); ingest quarantines
malformed rows per-row (API and CSV) and rejects sign-ambiguous and non-EUR ones;
the CSV backfill clips to the API's coverage window so the overlap can't be
double-counted; refunds net against spend; the bot token is redacted from every
error path (one send seam). `.env`, `ledger.db`, `merchant_map.json`,
`rules.local.yaml`, `bills.local.yaml`, `.pii-patterns`, `CODE-REVIEW.md` are
git-ignored, and the portable pre-commit hook (gitleaks + a grep against the
git-ignored `.pii-patterns`) blocks secrets and PII before they enter history.

## Quality gate

`ruff`, `mypy`, `pytest` (+coverage, gated at 80%), and `gitleaks` run in CI
(`.github/workflows/ci.yml`) and locally: `uv run ruff check . && uv run mypy
sentinel && uv run pytest -q`.
