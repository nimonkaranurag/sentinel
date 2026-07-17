-- Effective category per transaction (SPEC §2/§3: categorize -> SQL view).
-- Transaction-level override wins over the merchant's category (SPEC §5).
CREATE VIEW IF NOT EXISTS v_transactions_categorized AS
SELECT
  t.*,
  COALESCE(t.category_override, m.category, 'Uncategorized') AS category
FROM transactions t
LEFT JOIN merchants m ON m.id = t.merchant_id;
