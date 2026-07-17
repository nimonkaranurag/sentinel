-- 005: retire two remnants of removed subsystems.
--   (a) DROP the `budgets` table — the envelope/budget subsystem is gone; the
--       discretionary pool is a single config number now (controller.py).
--   (b) Remove 'llm' from the merchants.categorized_by allowlist. SQLite cannot
--       ALTER a CHECK constraint, so the table is rebuilt. Row ids are preserved,
--       so transactions.merchant_id links stay valid (init_db runs migrations
--       with FK enforcement off and verifies integrity afterward).

DROP TABLE IF EXISTS budgets;

-- v_transactions_categorized (migration 002) references merchants; SQLite blocks
-- rebuilding a table a view depends on, so drop the view and recreate it below.
DROP VIEW IF EXISTS v_transactions_categorized;

CREATE TABLE merchants_new (
  id              INTEGER PRIMARY KEY,
  name_normalized TEXT UNIQUE NOT NULL,
  display_name    TEXT,
  category        TEXT NOT NULL DEFAULT 'Uncategorized',
  categorized_by  TEXT CHECK (categorized_by IN ('dict', 'regex', 'manual')),
  confidence      REAL,
  first_seen      TEXT
) STRICT;

INSERT INTO merchants_new (id, name_normalized, display_name, category,
                           categorized_by, confidence, first_seen)
  SELECT id, name_normalized, display_name, category,
         CASE WHEN categorized_by = 'llm' THEN 'dict' ELSE categorized_by END,
         confidence, first_seen
  FROM merchants;

DROP TABLE merchants;
ALTER TABLE merchants_new RENAME TO merchants;

-- Recreate the view dropped above (identical to migration 002).
CREATE VIEW v_transactions_categorized AS
SELECT
  t.*,
  COALESCE(t.category_override, m.category, 'Uncategorized') AS category
FROM transactions t
LEFT JOIN merchants m ON m.id = t.merchant_id;
