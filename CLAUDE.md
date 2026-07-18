# CLAUDE.md — operating instructions for this repo

You are working on **Sentinel**, a personal, single-user finance
observability + control tool. `SPEC.md` is the single source of truth — read it
fully before writing code. If SPEC.md and this file ever conflict, SPEC.md wins.
If something is ambiguous, ask; do not invent scope.

## Prime directives
1. **No LLM, anywhere.** Categorization is `normalize → merchant map → regex
   rules`; the digest and reports are deterministic templates/arithmetic. There
   is no `llm.py`, no `openai`/`anthropic` dependency, no `llm_calls` table, and
   the `merchants.categorized_by` allowlist is `dict | regex | manual`. A
   never-seen merchant stays `Uncategorized` until the owner labels it.
2. **Money is integer cents.** No floats anywhere near amounts. `to_cents`
   refuses floats and ambiguous strings; STRICT tables refuse non-integers at the
   DB boundary. Currency EUR; non-EUR rows are quarantined at ingest. Timezone
   Europe/Dublin (compute date boundaries in Python, not SQLite `date('now')`,
   which is UTC). Dates are ISO-8601 strings.
3. **Idempotency everywhere.** Ingest, CSV import, categorization, alerts, and
   the pushes must be safely re-runnable. `INSERT OR IGNORE` on stable ids;
   deterministic hash fallback `sha256(booking_date|amount|merchant|account)`;
   the alert watermark and per-period push keys live in `state`.
4. **Invariants are tests.** Every correctness claim in `SPEC.md` (dedupe, the
   residual gate, cross-month bill lateness, the alert watermark, sender-id
   auth) is pinned by a pytest. Don't land a behavior change without the test
   that would fail on the old code.

## Hard prohibitions
- No scraping or automating AIB internet banking. No storing AIB credentials.
  Bank access is Enable Banking API + owner-exported CSVs, nothing else.
- No payment initiation of any kind.
- No secrets or PII in code or git. `.env` + `python-dotenv`; keep `.env.example`
  current. Private key referenced by path only. Owner identifiers live only in
  git-ignored `rules.local.yaml` / `bills.local.yaml` / `.pii-patterns`.
- No new dependencies beyond: `requests`, `matplotlib`, `python-dotenv`,
  `PyYAML`, `pytest` (+`pytest-cov`). No `pandas`, no `openai`/`anthropic`.
  Telegram = raw HTTPS to the Bot API. SQLite via stdlib `sqlite3`, raw SQL, no
  ORM. No async frameworks, no web server, no Docker in v1.
- Do not extend the category taxonomy in SPEC §3 without asking.

## Layout
```
sentinel/
  schema.sql  migrations/  db.py  state_keys.py  ingest.py  csv_import.py
  normalize.py  categorize.py  rules.yaml  policies.py  policies.yaml  bills.py  bills.yaml
  controller.py  reports.py  authorize.py
  telegram.py  render.py  alerts.py  commands.py  notify.py       # Telegram surface
merchant_map.json (repo root, git-ignored)  config.yaml  .env.example
scripts/pre-commit  .pii-patterns.example  tests/  data/backfill/  reports/  backups/  docs/
Makefile targets: init | hooks | secrets | backfill | categorize | relink | report | poll | notify | plan | digest | backup | test
```

## Conventions
- Small modules, pure functions where possible; side effects (DB, network) at
  the edges. The Telegram surface is split: transport (`telegram`), pure text
  (`render`), alert engine (`alerts`), command router (`commands`), cron
  orchestration (`notify`). `render.py` must stay side-effect-free.
- Every state-mutating module is runnable as `python -m sentinel.<module>` with
  `--dry-run`, which must not commit.
- Config in `config.yaml` (pool, thresholds, policy caps, cursor overlap, retry,
  PSU headers, inflow threshold). Nothing tunable is hardcoded.
- Tests use fixture CSVs in `tests/fixtures/` — synthetic data only, never real
  statements, never real account numbers or names.
- Logging: stdlib `logging`, INFO to stdout; no `print` in library code (CLI
  banners in `authorize.py` are the one edge exception).

## Definition of done
Code + tests green (`ruff`, `mypy`, `pytest` with coverage ≥ 80%) + docs
reconciled (a change that renames or deletes something greps for every reference
to it first) + the invariant it establishes pinned by a test.
