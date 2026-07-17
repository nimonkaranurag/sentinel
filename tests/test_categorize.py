import json
from datetime import date, timedelta

import pytest

from sentinel import categorize, db

ACCOUNT = "acc-uid-1"

# (merchant_raw, amount_cents, days_ago)
LEDGER = [
    ("VDP-TESCO STORES 4368 DUBLIN", -4_567, 2),   # regex -> Groceries
    ("LIDL DUBLIN", -2_345, 5),                    # regex -> Groceries
    ("SUMUP *BJJ ACADEMY", -12_000, 8),            # regex -> Health/Fitness
    ("ATM WITHDRAWAL MAIN ST", -10_000, 12),       # regex -> Cash
    ("SALARY ACME LTD", 330_000, 15),              # regex -> Income
    ("FAMILY TRANSFER", 100_000, 20),              # regex -> Transfers
    ("NETFLIX.COM", -1_799, 25),                   # regex -> Subscriptions
    ("ANTHROPIC", -2_000, 30),                     # regex -> Tools
    ("COFFEE ANGEL", -350, 3),                     # no regex hit; owner-map only
    ("COFFEE ANGEL", -350, 40),                    # same merchant, second txn
    ("THE FALAFEL GUY", -1_050, 45),               # no regex hit
    ("AVOCA STORE", -8_000, 60),                   # no regex hit
]
REGEX_MERCHANTS = 8
# The owner-written merchant map is now the ONLY way a non-regex merchant gets a
# label (no LLM). `by` is recorded as 'dict' when the cascade applies it.
OWNER_MAP = {
    "COFFEE ANGEL": {"category": "Coffee/Snacks", "by": "manual", "confidence": 1.0},
    "THE FALAFEL GUY": {"category": "Dining", "by": "manual", "confidence": 1.0},
    "AVOCA STORE": {"category": "Shopping", "by": "manual", "confidence": 1.0},
}


# Fixed reference date, not date.today(): categorization is date-independent, so
# pinning it keeps the ledger deterministic and free of any midnight-boundary
# flake (the old date.today() wasn't even Dublin-aware).
REF_DATE = date(2026, 7, 1)


def build_ledger(conn):
    rows = [{"account_id": ACCOUNT,
             "booking_date": (REF_DATE - timedelta(days=days_ago)).isoformat(),
             "amount_cents": cents, "merchant_raw": raw, "description": raw,
             "source": "api"}
            for raw, cents, days_ago in LEDGER]
    inserted, submitted = db.insert_transactions(conn, rows)
    assert inserted == submitted == len(LEDGER)
    conn.commit()


def make_cfg(tmp_path, merchant_map=None):
    map_path = tmp_path / "merchant_map.json"
    map_path.write_text(json.dumps(merchant_map or {}))
    return {"db_path": str(tmp_path / "ledger.db"),
            "categorize": {"merchant_map_path": str(map_path), "rules_path": None}}


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "ledger.db")
    db.init_db(connection)
    build_ledger(connection)
    yield connection
    connection.close()


# ── The local cascade ─────────────────────────────────────────────────────


def test_regex_rules_categorize_and_novel_stays_uncategorized(conn, tmp_path):
    stats = categorize.run(conn, make_cfg(tmp_path))  # empty map
    assert stats["linked"] == len(LEDGER)
    assert stats["by_regex"] == REGEX_MERCHANTS
    assert stats["by_dict"] == 0
    assert stats["novel_unresolved"] == 3  # the 3 non-regex merchants
    uncat = conn.execute(
        "SELECT COUNT(*) FROM merchants WHERE category = 'Uncategorized'").fetchone()[0]
    assert uncat == 3


def test_owner_map_labels_non_regex_merchants(conn, tmp_path):
    stats = categorize.run(conn, make_cfg(tmp_path, merchant_map=OWNER_MAP))
    assert stats["by_regex"] == REGEX_MERCHANTS
    assert stats["by_dict"] == len(OWNER_MAP)
    assert stats["novel_unresolved"] == 0
    row = conn.execute("SELECT category, categorized_by FROM merchants "
                       "WHERE name_normalized = 'COFFEE ANGEL'").fetchone()
    # The map entry says by='manual' (an owner /recat wrote it), and the cascade
    # honors that provenance rather than flattening it to 'dict'.
    assert tuple(row) == ("Coffee/Snacks", "manual")


def test_relink_is_atomic_and_preserves_manual_labels(conn, tmp_path):
    """--relink rebuilds in one transaction and restores owner 'manual' labels
    from the map, instead of committing an unlabeled ledger mid-rebuild."""
    cfg = make_cfg(tmp_path, merchant_map=OWNER_MAP)
    categorize.run(conn, cfg)
    # An owner correction NOT in the map, but mirrored into the map (as /recat does):
    import json as _json
    conn.execute("UPDATE merchants SET category = 'Dates', categorized_by = 'manual' "
                 "WHERE name_normalized = 'THE FALAFEL GUY'")
    conn.commit()
    m = _json.loads(open(cfg["categorize"]["merchant_map_path"]).read())
    m["THE FALAFEL GUY"] = {"category": "Dates", "by": "manual", "confidence": 1.0}
    open(cfg["categorize"]["merchant_map_path"], "w").write(_json.dumps(m))

    categorize.relink(conn, cfg)
    # Every transaction is re-linked (none orphaned) and the manual label survived.
    orphans = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE merchant_id IS NULL AND merchant_raw != ''"
    ).fetchone()[0]
    assert orphans == 0
    row = conn.execute("SELECT category, categorized_by FROM merchants "
                       "WHERE name_normalized = 'THE FALAFEL GUY'").fetchone()
    assert tuple(row) == ("Dates", "manual")


def test_second_run_is_idempotent(conn, tmp_path):
    cfg = make_cfg(tmp_path, merchant_map=OWNER_MAP)
    categorize.run(conn, cfg)
    stats2 = categorize.run(conn, cfg)
    assert stats2["by_regex"] == 0 and stats2["by_dict"] == 0  # nothing left to do
    assert stats2["novel_unresolved"] == 0


def test_manual_categorization_is_never_overwritten(conn, tmp_path):
    cfg = make_cfg(tmp_path, merchant_map=OWNER_MAP)
    categorize.run(conn, cfg)
    conn.execute("UPDATE merchants SET category = 'Dates', categorized_by = 'manual' "
                 "WHERE name_normalized = 'COFFEE ANGEL'")
    conn.commit()
    categorize.run(conn, cfg)
    row = conn.execute("SELECT category, categorized_by FROM merchants "
                       "WHERE name_normalized = 'COFFEE ANGEL'").fetchone()
    assert tuple(row) == ("Dates", "manual")


def test_dry_run_writes_nothing(conn, tmp_path):
    stats = categorize.run(conn, make_cfg(tmp_path), dry_run=True)
    assert stats["by_regex"] == REGEX_MERCHANTS  # reported…
    assert conn.execute("SELECT COUNT(*) FROM merchants").fetchone()[0] == 0  # …rolled back
    assert conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE merchant_id IS NOT NULL").fetchone()[0] == 0


def test_migrations_apply_and_view_exists(tmp_path):
    conn = db.connect(tmp_path / "ledger.db")
    # schema v1 + 002 view + 003 drop llm_calls + 004 events + 005 drop budgets/llm-check
    assert db.init_db(conn) == 5
    assert conn.execute("SELECT COUNT(*) FROM v_transactions_categorized").fetchone()[0] == 0
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "budgets" not in tables, "migration 005 must drop the dead budgets table"
    conn.close()


def test_no_llm_subsystem(tmp_path):
    conn = db.connect(tmp_path / "ledger.db")
    db.init_db(conn)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "llm_calls" not in tables
    conn.close()
    with pytest.raises(ImportError):
        __import__("sentinel.llm")


def test_seed_rules_load_and_are_taxonomy_valid():
    rules = categorize.load_rules()
    assert rules, "seed rules.yaml must not be empty"
    assert all(cat in categorize.TAXONOMY for _, cat in rules)
