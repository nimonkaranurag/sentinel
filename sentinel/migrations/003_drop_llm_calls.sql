-- 003: drop llm_calls from any pre-existing DB. Categorization is regex + an
-- owner-written merchant map; there is no LLM call log to keep.
DROP TABLE IF EXISTS llm_calls;
