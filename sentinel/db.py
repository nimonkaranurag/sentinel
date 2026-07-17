"""
SQLite layer: connection, schema/migrations, and shared ledger helpers.

The money parser and transaction-id helpers live here because ingest.py (API)
and csv_import.py (CSV) must produce byte-identical hash ids for the
cross-source dedupe required by SPEC §2.

Money is integer cents end to end: `to_cents` refuses floats, and the STRICT
schema refuses non-integer amounts at the database boundary.

CLI: python -m sentinel.db --init [--db PATH] [--config PATH] [--dry-run]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

log = logging.getLogger(__name__)

PKG_DIR = Path(__file__).resolve().parent
REPO_ROOT = PKG_DIR.parent
SCHEMA_PATH = PKG_DIR / "schema.sql"
MIGRATIONS_DIR = PKG_DIR / "migrations"
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"

TZ = ZoneInfo("Europe/Dublin")

# schema.sql is version 1; migrations/00N_*.sql start at 002.
SCHEMA_VERSION = 1

VALID_SOURCES = ("api", "csv")

# How long a writer waits on a locked WAL DB before raising OperationalError.
# The four daily polls, /sync, and the callback loop can briefly contend for the
# write lock; a bounded wait lets the loser retry instead of crashing outright.
BUSY_TIMEOUT_MS = 5000

# Amount grammar: plain decimal or thousands-grouped, max 2 decimal places.
# Anything else (decimal commas, >2 decimals, stray text) raises rather than
# risking silent money corruption.
_AMOUNT_PLAIN = re.compile(r"^[+-]?\d+(?:\.\d{1,2})?$")
_AMOUNT_GROUPED = re.compile(r"^[+-]?\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?$")


# ── Config ────────────────────────────────────────────────────────────────


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """
    Load configuration from config.yaml (repository root by default).
    """
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    with open(cfg_path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    if not isinstance(cfg, dict):
        raise ValueError(f"config root must be a mapping: {cfg_path}")
    return cfg


# ── Connection / schema ───────────────────────────────────────────────────


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")  # wait, don't crash, on lock contention
    return conn


def backup(conn: sqlite3.Connection, dest_path: str | Path) -> None:
    """
    Write a consistent online backup using the SQLite backup API.

    Safe on a live WAL database, unlike a filesystem copy, which can capture a
    torn or stale image missing the -wal segment.
    """
    dest = sqlite3.connect(str(dest_path))
    try:
        conn.backup(dest)
    finally:
        dest.close()


def _migration_files() -> list[tuple[int, Path]]:
    if not MIGRATIONS_DIR.is_dir():
        return []
    out = []
    for path in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql")):
        out.append((int(path.name[:3]), path))
    return out


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def init_db(conn: sqlite3.Connection) -> int:
    """
    Create or upgrade the schema. Idempotent; returns the final version.
    """
    version = schema_version(conn)
    if version == 0:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        version = SCHEMA_VERSION
        log.info("applied schema.sql (version %d)", version)

    pending = [(n, p) for n, p in _migration_files() if n > version]
    if pending:
        # Migrations may rebuild a table to change a constraint SQLite can't
        # ALTER (e.g. 005 tightens merchants.categorized_by). Dropping a table
        # that other tables reference would trip inbound FKs, so run the whole
        # batch with enforcement off, then verify integrity before re-enabling.
        # PRAGMA foreign_keys is a no-op inside a transaction, hence the commit.
        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            for number, path in pending:
                if number != version + 1:
                    raise RuntimeError(
                        f"migration gap: at version {version}, next file is {path.name}"
                    )
                conn.executescript(path.read_text(encoding="utf-8"))
                conn.execute(f"PRAGMA user_version = {number}")
                version = number
                log.info("applied migration %s (version %d)", path.name, version)
            conn.commit()
            broken = conn.execute("PRAGMA foreign_key_check").fetchall()
            if broken:
                raise RuntimeError(f"migration left dangling foreign keys: {broken!r}")
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()
    return version


# ── State (cursors, counters, consent expiry, …) ─────────────────────────


def get_state(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


# ── Money / ids ───────────────────────────────────────────────────────────


def to_cents(text: str) -> int:
    """
    Parse a decimal-euro string (e.g. '1,368.00', '-12.3', '5') to integer cents.

    Accepts strings only. Integers are ambiguous (euros or cents) and floats are
    disallowed near money; both raise TypeError.
    """
    if not isinstance(text, str):
        raise TypeError(f"amount must be a decimal string, got {type(text).__name__}")
    s = text.strip().replace("€", "").replace(" ", "")
    if not s:
        raise ValueError("empty amount string")
    if _AMOUNT_GROUPED.match(s):
        s = s.replace(",", "")
    elif not _AMOUNT_PLAIN.match(s):
        raise ValueError(f"unparseable amount: {text!r}")
    sign = -1 if s.startswith("-") else 1
    s = s.lstrip("+-")
    euros, _, frac = s.partition(".")
    frac = (frac + "00")[:2]
    return sign * (int(euros) * 100 + int(frac))


def cents_to_str(cents: int) -> str:
    """
    Format integer cents as a sign-prefixed decimal string, for logs only.
    """
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def fmt_eur(cents: int) -> str:
    """
    Format integer cents as a euro display string (e.g. €1,368.00), using
    integer math only.
    """
    sign = "-" if cents < 0 else ""
    c = abs(int(cents))
    return f"{sign}€{c // 100:,}.{c % 100:02d}"


def txn_hash_id(
    booking_date: str,
    amount_cents: int,
    merchant_raw: str | None,
    account_id: str,
    occurrence: int = 0,
) -> str:
    """
    Compute the deterministic fallback id
    sha256(booking_date|amount_cents|merchant_raw|account).

    `occurrence` disambiguates byte-identical rows within a single batch.
    Occurrence 0 is the plain SPEC §2 formula, so ids match across sources; later
    occurrences append `|n`.
    """
    base = f"{booking_date}|{amount_cents}|{merchant_raw or ''}|{account_id}"
    if occurrence:
        base += f"|{occurrence}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


# ── Transactions ──────────────────────────────────────────────────────────

_OPTIONAL_FIELDS = ("value_date", "merchant_raw", "merchant_id", "category_override", "description")

_INSERT_TXN_SQL = """
INSERT OR IGNORE INTO transactions
  (id, account_id, booking_date, value_date, amount_cents, currency,
   merchant_raw, merchant_id, category_override, description, source, inserted_at)
VALUES
  (:id, :account_id, :booking_date, :value_date, :amount_cents, :currency,
   :merchant_raw, :merchant_id, :category_override, :description, :source, :inserted_at)
"""


def prepare_transactions(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """
    Validate rows and assign ids (the bank id when present, otherwise a hash
    with occurrence).

    Occurrence counting is per batch in input order, so re-running the same file
    or API page reproduces the same ids, which is what makes INSERT OR IGNORE
    idempotent.
    """
    prepared: list[dict[str, Any]] = []
    occurrences: dict[tuple, int] = {}
    for raw in rows:
        row = dict(raw)
        amount = row.get("amount_cents")
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise TypeError(f"amount_cents must be int, got {amount!r}")
        date.fromisoformat(row["booking_date"])  # raises on non-ISO dates
        if row.get("source") not in VALID_SOURCES:
            raise ValueError(f"source must be one of {VALID_SOURCES}: {row.get('source')!r}")
        if not row.get("account_id"):
            raise ValueError("account_id is required")
        row.setdefault("currency", "EUR")
        for field in _OPTIONAL_FIELDS:
            row.setdefault(field, None)
        if not row.get("id"):
            key = (row["booking_date"], amount, row["merchant_raw"] or "", row["account_id"])
            occ = occurrences.get(key, 0)
            occurrences[key] = occ + 1
            row["id"] = txn_hash_id(*key, occurrence=occ)
        row.setdefault("inserted_at", now_iso())
        prepared.append(row)
    return prepared


def insert_transactions(
    conn: sqlite3.Connection, rows: Iterable[Mapping[str, Any]]
) -> tuple[int, int]:
    """
    INSERT OR IGNORE prepared rows. Returns (inserted, submitted).

    Does not commit; callers commit on success or roll back for --dry-run.
    """
    prepared = prepare_transactions(rows)
    before = conn.total_changes
    conn.executemany(_INSERT_TXN_SQL, prepared)
    return conn.total_changes - before, len(prepared)


# ── Quarantine (rows that cannot enter the ledger) ────────────────────────


def quarantine_row(conn: sqlite3.Connection, source: str, reason: str,
                   raw: Mapping[str, Any], account_id: str | None = None) -> None:
    """
    Record a row that could not be booked (non-EUR, sign-ambiguous, malformed) so
    it is retained and countable instead of vanishing into a log line.

    Idempotent: the fingerprint over source + raw payload makes the same row,
    re-fetched inside the cursor overlap window every poll, quarantine once rather
    than accumulating a duplicate every run. Does not commit.
    """
    payload = json.dumps(raw, sort_keys=True, default=str)
    fingerprint = hashlib.sha256(f"{source}|{payload}".encode()).hexdigest()
    conn.execute(
        "INSERT OR IGNORE INTO quarantine (source, reason, raw, account_id, seen_at, fingerprint) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (source, reason, payload, account_id, now_iso(), fingerprint),
    )


def quarantine_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0]


# ── CLI ───────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Initialize/upgrade the Sentinel ledger DB.")
    parser.add_argument("--init", action="store_true", help="create/upgrade the schema")
    parser.add_argument("--db", default=None, help="database path (default: config db_path)")
    parser.add_argument("--config", default=None, help="path to config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="report pending work, change nothing")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if not args.init:
        parser.error("nothing to do (use --init)")

    cfg = load_config(args.config)
    db_path = Path(args.db or cfg.get("db_path", "ledger.db"))

    if args.dry_run:
        if db_path.exists():
            conn = connect(db_path)
            current = schema_version(conn)
            conn.close()
        else:
            current = 0
        pending = [p.name for n, p in _migration_files() if n > max(current, SCHEMA_VERSION)]
        if current == 0:
            pending.insert(0, "schema.sql")
        log.info("dry-run: %s at version %d; would apply: %s",
                 db_path, current, ", ".join(pending) or "nothing")
        return 0

    conn = connect(db_path)
    try:
        version = init_db(conn)
    finally:
        conn.close()
    log.info("%s ready at schema version %d", db_path, version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
