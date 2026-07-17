-- Sentinel ledger schema, version 1 (SPEC §5, data model).
-- STRICT tables require SQLite >= 3.37: amount columns physically cannot
-- hold non-integer values (money is integer cents).
-- Schema changes after v1 ship as migrations/00N_*.sql (N >= 2), never edits here.
-- v1 still creates `budgets` (and an 'llm' provenance value); migration 005 drops
-- `budgets` and tightens the CHECK, so a fresh DB ends up without them.

CREATE TABLE IF NOT EXISTS merchants (
  id              INTEGER PRIMARY KEY,
  name_normalized TEXT UNIQUE NOT NULL,
  display_name    TEXT,
  category        TEXT NOT NULL DEFAULT 'Uncategorized',
  categorized_by  TEXT CHECK (categorized_by IN ('dict', 'regex', 'llm', 'manual')),
  confidence      REAL,
  first_seen      TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS transactions (
  id                TEXT PRIMARY KEY,   -- bank txn id; fallback: sha256(booking_date|amount_cents|merchant_raw|account)
  account_id        TEXT NOT NULL,
  booking_date      TEXT NOT NULL,      -- ISO date
  value_date        TEXT,
  amount_cents      INTEGER NOT NULL,   -- negative = outflow
  currency          TEXT NOT NULL DEFAULT 'EUR',
  merchant_raw      TEXT,
  merchant_id       INTEGER REFERENCES merchants(id),
  category_override TEXT,               -- transaction-level correction; wins over merchant category
  description       TEXT,
  source            TEXT CHECK (source IN ('api', 'csv')),
  inserted_at       TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS idx_transactions_booking_date ON transactions (booking_date);
CREATE INDEX IF NOT EXISTS idx_transactions_merchant_id ON transactions (merchant_id);

CREATE TABLE IF NOT EXISTS budgets (
  category            TEXT,
  monthly_limit_cents INTEGER,
  floor_cents         INTEGER,
  effective_from      TEXT
) STRICT;

-- String-keyed grab-bag: cursors, consent_expiry, per-day API counter,
-- idempotency markers, the alert watermark. Keys are defined in state_keys.py.
CREATE TABLE IF NOT EXISTS state (
  key   TEXT PRIMARY KEY,
  value TEXT
) STRICT;
