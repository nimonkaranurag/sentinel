"""
Categorizer cascade (SPEC §3): normalize, merchant map, regex rules.

The cascade stops at the first hit and uses no LLM. A merchant with no map or
rule hit stays Uncategorized (the discretionary pool) until the owner labels it,
either through the Telegram relabel loop (which writes the manual merchant map)
or through rules.local.yaml. Manual categorizations are never overwritten by this
module.

CLI: python -m sentinel.categorize [--dry-run] [--relink] [--db PATH] [--config PATH]
     --dry-run rolls back all writes.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from . import db
from .normalize import normalize

log = logging.getLogger(__name__)

# Two-tier taxonomy: fine SUB-LABELS for display/relabeling roll up into 6 coarse
# BUCKETS (see bucket()) used for all money math.
TAXONOMY = (
    "Rent",
    "Utilities",
    "Groceries",
    "Dining",
    "FoodDelivery",
    "Coffee/Snacks",
    "Transport",
    "Subscriptions",
    "Health/Fitness",
    "Tools",
    "Shopping",
    "Cash",
    "Dates",
    "Travel",
    "Gifts",
    "Fees",
    "Transfers",
    "Income",
    "Other",
    "Uncategorized",
)

BUCKETS = ("Income", "Transfers", "Fixed", "Groceries", "FoodDelivery", "Other")

# Sub-label → bucket. Unmapped (incl. Dining, Coffee/Snacks, Shopping, and
# Uncategorized) falls to Other = the discretionary pool. FoodDelivery and
# Groceries are split out as watched leaks; Fixed is committed/recurring spend.
_BUCKET_OF = {
    "Income": "Income",
    "Transfers": "Transfers",
    "Rent": "Fixed",
    "Utilities": "Fixed",
    "Tools": "Fixed",
    "Health/Fitness": "Fixed",
    "Subscriptions": "Fixed",
    "Fees": "Fixed",
    "Groceries": "Groceries",
    "FoodDelivery": "FoodDelivery",
}


def bucket(category: str | None) -> str:
    """
    Map a sub-label (or None) to one of the six math buckets.
    """
    return _BUCKET_OF.get(category or "", "Other")


# Buckets that count against the monthly discretionary pool (safe-to-spend).
DISCRETIONARY_BUCKETS = ("Groceries", "FoodDelivery", "Other")

# Category sets referenced by reports — derived from _BUCKET_OF so the
# reconciliation's "fixed" can never disagree with the Fixed bucket that
# /status, the digest, and the month-over-month table show.
FIXED_CATEGORIES = tuple(c for c, b in _BUCKET_OF.items() if b == "Fixed")
NON_SPEND_CATEGORIES = tuple(c for c, b in _BUCKET_OF.items() if b in ("Income", "Transfers"))

DEFAULT_RULES_PATH = Path(__file__).resolve().parent / "rules.yaml"
DEFAULT_MERCHANT_MAP_PATH = db.REPO_ROOT / "merchant_map.json"


# ── Rules ─────────────────────────────────────────────────────────────────


def _compile_rules_file(rules_path: Path) -> list[tuple[re.Pattern, str]]:
    if not rules_path.exists():
        return []
    with open(rules_path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    compiled = []
    for i, entry in enumerate(raw.get("rules", [])):
        # A malformed rules file kills every poll (categorize runs before the
        # alert pass), so fail with the file and rule number, not a bare
        # KeyError/AttributeError/re.error deep in a cron log.
        if not isinstance(entry, dict) or "pattern" not in entry:
            raise ValueError(f"{rules_path} rule #{i + 1}: each rule needs 'pattern' and 'category'")
        category = entry.get("category")
        if category not in TAXONOMY:
            raise ValueError(f"{rules_path} rule #{i + 1}: category {category!r} is not in the SPEC §3 taxonomy")
        try:
            pattern = re.compile(entry["pattern"])
        except re.error as exc:
            raise ValueError(f"{rules_path} rule #{i + 1}: invalid regex {entry['pattern']!r}: {exc}") from None
        compiled.append((pattern, category))
    return compiled


def load_rules(path: str | Path | None = None) -> list[tuple[re.Pattern, str]]:
    """
    Load and merge categorization rules, placing the optional, git-ignored
    `rules.local.yaml` ahead of the shared seed rules so personal patterns match
    first (SPEC §3).
    """
    base_path = Path(path) if path else DEFAULT_RULES_PATH
    return _compile_rules_file(base_path.with_name("rules.local.yaml")) + _compile_rules_file(base_path)


def match_rules(rules: list[tuple[re.Pattern, str]], name_normalized: str) -> str | None:
    for pattern, category in rules:
        if pattern.search(name_normalized):
            return category
    return None


# ── Merchant map (learned, grows forever; git-ignored) ────────────────────


def load_merchant_map(path: str | Path) -> dict[str, dict[str, Any]]:
    map_path = Path(path)
    if not map_path.exists():
        return {}
    with open(map_path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        # SPEC §3 calls the map owner-written, so a hand-edit can break it; an
        # unusable map must degrade to the rules tier loudly, not silently.
        log.warning("%s root is %s, not an object — ignoring the merchant map", map_path, type(data).__name__)
        return {}
    return data


def save_merchant_map(path: str | Path, mapping: dict[str, dict[str, Any]]) -> None:
    """
    Write the merchant map atomically (temp file plus rename), with keys sorted
    for stable diffs.

    Single-writer by design: only the interactive relabel path (a /recat or a
    callback tap, processed one Telegram update at a time) writes the map; the
    crons only read it. The atomic rename prevents a torn file but does not merge
    concurrent writers, so two processes must never write at once.
    """
    map_path = Path(path)
    fd, tmp_name = tempfile.mkstemp(dir=map_path.parent or Path("."), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(mapping, fh, indent=2, sort_keys=True, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp_name, map_path)
    except BaseException:
        os.unlink(tmp_name)
        raise


# ── Cascade ───────────────────────────────────────────────────────────────


def link_transactions(conn) -> int:
    """
    Ensure a merchants row per normalized name and set transactions.merchant_id.

    Idempotent: only rows with merchant_id IS NULL are touched.
    """
    rows = conn.execute(
        "SELECT merchant_raw, MIN(booking_date) AS first_seen FROM transactions "
        "WHERE merchant_id IS NULL AND merchant_raw IS NOT NULL AND merchant_raw != '' "
        "GROUP BY merchant_raw"
    ).fetchall()
    linked = 0
    raw_for_name: dict[str, str] = {}
    for row in rows:
        name = normalize(row["merchant_raw"])
        if not name:
            continue
        if name in raw_for_name and raw_for_name[name] != row["merchant_raw"]:
            log.warning(
                "merchant-key collision: %r and %r both normalize to %r — policies "
                "and labels will treat them as one merchant",
                raw_for_name[name],
                row["merchant_raw"],
                name,
            )
        raw_for_name.setdefault(name, row["merchant_raw"])
        conn.execute(
            "INSERT OR IGNORE INTO merchants (name_normalized, first_seen) VALUES (?, ?)",
            (name, row["first_seen"]),
        )
        cursor = conn.execute(
            "UPDATE transactions SET merchant_id = "
            "(SELECT id FROM merchants WHERE name_normalized = ?) "
            "WHERE merchant_raw = ? AND merchant_id IS NULL",
            (name, row["merchant_raw"]),
        )
        linked += cursor.rowcount
    return linked


def _set_merchant_category(conn, name: str, category: str, by: str, confidence: float | None) -> None:
    # 'manual' rows (owner /recat corrections) are never overwritten here.
    conn.execute(
        "UPDATE merchants SET category = ?, categorized_by = ?, confidence = ? "
        "WHERE name_normalized = ? AND (categorized_by IS NULL OR categorized_by != 'manual')",
        (category, by, confidence, name),
    )


def coverage_stats(conn, as_of: date, window_days: int = 90) -> dict[str, float]:
    """
    Return the share of trailing-window transactions with a real category, by
    count and by absolute euro volume.

    The window boundary is computed in Europe/Dublin and passed as a bound
    parameter, because SQLite's date('now') is UTC and would shift the boundary
    between 23:00 and midnight local time.
    """
    boundary = (as_of - timedelta(days=window_days)).isoformat()
    row = conn.execute(
        "SELECT COUNT(*) AS n, "
        "  SUM(CASE WHEN category != 'Uncategorized' THEN 1 ELSE 0 END) AS n_cat, "
        "  COALESCE(SUM(ABS(amount_cents)), 0) AS vol, "
        "  COALESCE(SUM(CASE WHEN category != 'Uncategorized' THEN ABS(amount_cents) ELSE 0 END), 0) AS vol_cat "
        "FROM v_transactions_categorized WHERE booking_date >= ?",
        (boundary,),
    ).fetchone()
    n, n_cat, vol, vol_cat = row["n"], row["n_cat"] or 0, row["vol"], row["vol_cat"]
    return {
        "window_txns": n,
        "count_pct": 100.0 if n == 0 else 100.0 * n_cat / n,
        "value_pct": 100.0 if vol == 0 else 100.0 * vol_cat / vol,
    }


def _apply_cascade(conn, cfg: dict[str, Any]) -> dict[str, Any]:
    """
    Apply the normalize, merchant-map, regex-rules cascade over pending
    merchants.

    Does not commit; callers own the transaction (run or relink).
    """
    cat_cfg = cfg.get("categorize") or {}
    rules = load_rules(cat_cfg.get("rules_path"))
    map_path = Path(cat_cfg.get("merchant_map_path") or DEFAULT_MERCHANT_MAP_PATH)
    merchant_map = load_merchant_map(map_path)
    stale_keys = sorted(k for k in merchant_map if normalize(k) != k)
    if stale_keys:
        # Lookup is by exact normalized name, so a key that is not in normalized
        # form (a hand-typed lowercase entry, or one written by an older
        # normalizer) can never match — surface it instead of silently ignoring.
        preview = ", ".join(repr(k) for k in stale_keys[:5])
        log.warning(
            "%d merchant_map key(s) are not normalized and can never match (fix or --relink after "
            "a normalizer change): %s%s",
            len(stale_keys),
            preview,
            "…" if len(stale_keys) > 5 else "",
        )

    stats: dict[str, Any] = {"linked": link_transactions(conn), "by_dict": 0, "by_regex": 0, "novel_unresolved": 0}

    pending = conn.execute(
        "SELECT name_normalized FROM merchants "
        "WHERE category = 'Uncategorized' AND (categorized_by IS NULL OR categorized_by != 'manual') "
        "ORDER BY name_normalized"
    ).fetchall()

    novel: list[str] = []
    for row in pending:
        name = row["name_normalized"]
        entry = merchant_map.get(name)  # 2. exact learned lookup
        if entry is not None and not isinstance(entry, dict):
            # One poisoned hand-edit must not crash the poll (categorize runs
            # before every alert pass) — degrade this merchant to the rules tier.
            log.warning("merchant_map entry for %r is not an object — ignoring it", name)
            entry = None
        if entry is not None:
            category = entry.get("category", "Uncategorized")
            # Honor the map entry's provenance: an owner /recat writes
            # by='manual', and that must survive a --relink rebuild, not be
            # flattened to 'dict'. Unknown values fall back to 'dict'.
            by = entry.get("by")
            by = by if by in ("dict", "manual", "regex") else "dict"
            # A manual 'Uncategorized' is an explicit unlabel (typed
            # /recat <ref> uncategorized): apply it so the rules tier — and a
            # future --relink — cannot override the owner's decision.
            if category in TAXONOMY and (category != "Uncategorized" or by == "manual"):
                _set_merchant_category(conn, name, category, by, entry.get("confidence"))
                stats["by_dict"] += 1
                continue
            if category not in TAXONOMY:
                log.warning(
                    "merchant_map entry for %r has category %r, not in the SPEC §3 taxonomy — "
                    "falling through to the rules tier",
                    name,
                    category,
                )
        category = match_rules(rules, name)  # 3. seed regex rules
        if category:
            _set_merchant_category(conn, name, category, "regex", 1.0)
            stats["by_regex"] += 1
            continue
        novel.append(name)  # 4. no map/rule hit — stays Uncategorized (discretionary pool)

    stats["novel_unresolved"] = len(novel)
    if novel:
        log.info(
            "%d merchant(s) unresolved (no map/rule hit) — discretionary pool "
            "until labeled via Telegram or rules.local.yaml",
            len(novel),
        )
    return stats


def _log_cascade(conn, cfg: dict[str, Any], as_of: date, stats: dict[str, Any]) -> dict[str, Any]:
    window = int((cfg.get("categorize") or {}).get("coverage_window_days", 90))
    stats.update(coverage_stats(conn, as_of, window))
    log.info(
        "cascade: %(linked)d linked · %(by_dict)d dict · %(by_regex)d regex · "
        "%(novel_unresolved)d unresolved · "
        "90d coverage %(count_pct).1f%% by count / %(value_pct).1f%% by value",
        stats,
    )
    return stats


def run(conn, cfg: dict[str, Any], dry_run: bool = False, as_of: date | None = None) -> dict[str, Any]:
    """
    Run the local cascade (normalize, merchant map, regex rules) and return
    stats. Commits unless dry_run, which rolls back.

    Never-seen merchants stay Uncategorized (the discretionary pool) until the
    owner labels them.
    """
    as_of = as_of or datetime.now(db.TZ).date()
    stats = _apply_cascade(conn, cfg)
    # Read coverage before commit/rollback (as relink does), so a --dry-run
    # reports the coverage the run WOULD produce, not the pre-run state.
    result = _log_cascade(conn, cfg, as_of, stats)
    if dry_run:
        conn.rollback()
        log.info("dry-run: rolled back all categorization writes")
    else:
        conn.commit()
    return result


def relink(conn, cfg: dict[str, Any], as_of: date | None = None, dry_run: bool = False) -> dict[str, Any]:
    """
    Clear every merchant link and row and rebuild from scratch in a single
    transaction. Run after a normalizer or rule change.

    The clear and rebuild share one transaction, so a crash mid-rebuild (for
    example on a malformed rules.local.yaml) leaves the existing labels intact.
    Owner 'manual' labels survive because _apply_cascade re-applies them from the
    merchant map with their original provenance. Under dry_run the whole
    clear-and-rebuild is rolled back, so `--relink --dry-run` is a real preview of
    what the rebuild would produce rather than a silently downgraded plain run.
    """
    as_of = as_of or datetime.now(db.TZ).date()
    cleared = conn.execute("SELECT COUNT(*) FROM merchants").fetchone()[0]
    conn.execute("UPDATE transactions SET merchant_id = NULL")
    conn.execute("DELETE FROM merchants")
    stats = _apply_cascade(conn, cfg)
    result = _log_cascade(conn, cfg, as_of, stats)  # read the rebuilt state before commit/rollback
    if dry_run:
        conn.rollback()
        log.info(
            "dry-run relink: would clear %d merchant row(s) and rebuild %d link(s) — rolled back",
            cleared,
            stats["linked"],
        )
    else:
        conn.commit()
        log.info("relink: cleared %d merchant row(s), rebuilt %d link(s)", cleared, stats["linked"])
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Categorize transactions via the SPEC §3 cascade.")
    parser.add_argument("--dry-run", action="store_true", help="report what would change; write nothing")
    parser.add_argument(
        "--relink",
        action="store_true",
        help="clear merchants + merchant_id and rebuild from scratch, "
        "atomically, preserving owner labels (run after a "
        "normalizer/rule change)",
    )
    parser.add_argument("--db", default=None)
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    from dotenv import load_dotenv

    load_dotenv()

    cfg = db.load_config(args.config)
    conn = db.connect(args.db or cfg.get("db_path", "ledger.db"))
    try:
        db.init_db(conn)
        if args.relink:
            relink(conn, cfg, dry_run=args.dry_run)
        else:
            run(conn, cfg, dry_run=args.dry_run)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
