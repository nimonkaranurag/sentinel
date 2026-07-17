-- Append-only audit + exactly-once ledger for Telegram alert interactions
-- (the relabel loop). `events` records each policy alert and its resolution;
-- `processed_callbacks` makes a retried/duplicate inline-keyboard tap a no-op.
CREATE TABLE IF NOT EXISTS events (
  id         INTEGER PRIMARY KEY,
  kind       TEXT NOT NULL,     -- 'policy_alert'
  txn_id     TEXT,
  message_id INTEGER,           -- telegram message id, for in-place edits
  status     TEXT,              -- 'sent' | 'fine' | 'reclassified'
  detail     TEXT,
  created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS processed_callbacks (
  callback_id  TEXT PRIMARY KEY,
  processed_at TEXT NOT NULL
) STRICT;
