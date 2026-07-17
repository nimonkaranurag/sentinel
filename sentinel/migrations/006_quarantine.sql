-- 006: retain rows that cannot enter the ledger instead of dropping them.
-- Non-EUR, sign-ambiguous, and malformed rows were previously logged and lost, so
-- a recurring foreign-currency charge silently re-appeared and re-vanished on
-- every poll. This table keeps them (raw JSON + reason), deduped by fingerprint so
-- a re-fetched row lands once, and /status surfaces the count (SPEC §2).
CREATE TABLE IF NOT EXISTS quarantine (
  id          INTEGER PRIMARY KEY,
  source      TEXT NOT NULL,             -- 'api' | 'csv'
  reason      TEXT NOT NULL,             -- why it could not be booked
  raw         TEXT NOT NULL,             -- JSON of the offending row
  account_id  TEXT,
  seen_at     TEXT NOT NULL,
  fingerprint TEXT UNIQUE NOT NULL       -- sha256(source|raw) — idempotent re-quarantine
) STRICT;
