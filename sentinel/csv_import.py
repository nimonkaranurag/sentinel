"""
AIB CSV export to ledger (source='csv'): the 12-month backfill path (SPEC §2).

The columns are AIB's real online-banking export (confirmed against a live
export in data/backfill/): a single `Description`, no `Posted Currency`, plus
`Balance` and `Transaction Type`:

  Posted Account, Posted Transactions Date, Description, Debit Amount,
  Credit Amount, Balance, Transaction Type

Header resolution is case-insensitive and also accepts the older multi-column
layout (`Description1..3`, `Posted Currency`). It fails loudly, printing the
header it found, so a format change surfaces immediately.

Dates are DD/MM/YY (AIB's real export) or DD/MM/YYYY. Amounts stay integer cents,
parsed as strings rather than floats. A debit is negative, a credit positive.

Cross-source safety (SPEC §2): AIB CSVs carry no transaction id, so every row
takes the hash id sha256(booking_date|amount_cents|merchant_raw|account). This
dedupes re-imports of the same file but does not reliably dedupe against the API,
because the two sources derive merchant_raw differently and the hashes usually
differ. The guard is to clip the import to dates the API has not reached: rows on
or after the earliest API booking date are dropped (config
csv_import.clip_to_cursor). For the clip and any hash overlap to align,
csv_import.account_map must map the CSV "Posted Account" label to the canonical
API account uid.

Malformed rows are quarantined per row, matching the API path, so a bad date does
not abort the backfill.

CLI: python -m sentinel.csv_import FILE [FILE ...] [--dry-run] [--db PATH] [--config PATH]
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from . import db, state_keys

log = logging.getLogger(__name__)

# Header resolution: our field -> acceptable column names (lowercased).
COLUMN_CANDIDATES: dict[str, tuple[str, ...]] = {
    "account": ("posted account", "account", "masked account"),
    "date": ("posted transactions date", "transaction date", "date"),
    "debit": ("debit amount", "debit"),
    "credit": ("credit amount", "credit"),
    "currency": ("posted currency", "currency"),
    "type": ("transaction type", "type"),
    "balance": ("balance",),
}
DESCRIPTION_COLUMNS = ("description1", "description2", "description3", "description")
REQUIRED_FIELDS = ("account", "date", "debit", "credit")


def parse_aib_date(raw: str) -> str:
    """
    Parse an AIB date ('DD/MM/YY' or 'DD/MM/YYYY') or an ISO 'YYYY-MM-DD' string
    to an ISO date string.

    AIB's real export uses a 2-digit year (e.g. '13/07/26'); the format is chosen
    by the year token's width so '%Y' cannot misread '26' as the year 0026. '%y'
    maps 00-68 to 2000-2068, which covers AIB's date range.
    """
    text = raw.strip()
    if "/" in text:
        parts = text.split("/")
        if len(parts) == 3 and parts[2].isdigit():
            fmt = "%d/%m/%y" if len(parts[2]) == 2 else "%d/%m/%Y"
            try:
                return datetime.strptime(text, fmt).date().isoformat()
            except ValueError:
                pass
    else:
        try:
            return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
        except ValueError:
            pass
    raise ValueError(f"unrecognized date: {raw!r}")


def _resolve_header(fieldnames: list[str]) -> dict[str, str]:
    """
    Map field names to the file's actual column names, raising if a required
    column is missing.
    """
    by_lower = {name.strip().lower(): name for name in fieldnames if name}
    resolved: dict[str, str] = {}
    for field, candidates in COLUMN_CANDIDATES.items():
        for candidate in candidates:
            if candidate in by_lower:
                resolved[field] = by_lower[candidate]
                break
    missing = [f for f in REQUIRED_FIELDS if f not in resolved]
    if missing:
        raise ValueError(
            f"CSV header missing required column(s) {missing}; "
            f"header found: {fieldnames}. If AIB changed its export format, "
            f"update COLUMN_CANDIDATES in sentinel/csv_import.py."
        )
    resolved["descriptions"] = [by_lower[c] for c in DESCRIPTION_COLUMNS if c in by_lower]  # type: ignore[assignment]
    return resolved


def row_to_transaction(
    row: dict[str, str],
    columns: dict[str, Any],
    account_map: dict[str, str],
    currency: str = "EUR",
) -> dict[str, Any] | None:
    """
    Convert one CSV row to a ledger row dict. Returns None to skip (no amount, or
    a pending row).
    """
    # A pending/authorization row has no stable identity yet (mirrors the API
    # path skipping non-BOOKED rows). AIB exports are historical/booked, but guard
    # anyway in case a future export includes them.
    type_col = columns.get("type")
    txn_type = (row.get(type_col) or "").strip().lower() if type_col else ""
    if "pending" in txn_type or "authoris" in txn_type or "authoriz" in txn_type:
        return None

    debit_raw = (row.get(columns["debit"]) or "").strip()
    credit_raw = (row.get(columns["credit"]) or "").strip()
    if not debit_raw and not credit_raw:
        return None

    cents = 0
    if credit_raw:
        cents += abs(db.to_cents(credit_raw))
    if debit_raw:
        cents -= abs(db.to_cents(debit_raw))
    if debit_raw and credit_raw:
        log.warning("row has both debit and credit; using net %s: %r", db.cents_to_str(cents), row)
    if cents == 0:
        return None  # €0 line (e.g. AIB's "8.60 USD@" FX annotation) — not a spend

    description = (
        " ".join(part for part in ((row.get(col) or "").strip() for col in columns["descriptions"]) if part).strip()
        or None
    )

    csv_account = (row.get(columns["account"]) or "").strip()
    account_id = account_map.get(csv_account, csv_account)
    if not account_id:
        raise ValueError(f"row has empty account column: {row!r}")

    currency_col = columns.get("currency")
    row_currency = ((row.get(currency_col) or "").strip().upper() if currency_col else "") or currency
    if row_currency != currency:  # a EUR account summed with a non-EUR row is corrupt
        raise ValueError(f"non-{currency} row currency {row_currency!r}")

    return {
        "id": None,  # AIB CSVs have no txn id → hash fallback (db layer)
        "account_id": account_id,
        "booking_date": parse_aib_date(row[columns["date"]]),
        "value_date": None,
        "amount_cents": cents,
        "currency": currency,
        "merchant_raw": description,
        "description": description,
        "source": "csv",
    }


def compute_clip_before(conn) -> str | None:
    """
    Return the date at or after which CSV rows are already covered by the API and
    so must be clipped to avoid double-counting.

    This is a fact about *coverage*, so it is anchored to the recorded API
    coverage start (the first pull's date_from, api_coverage_start) rather than to
    MIN(booking_date): a single backdated reversal booked months inside the window
    would otherwise drag MIN back and clip — silently — almost the entire 12-month
    backfill. Normally MIN(api) sits at or after the coverage start and is the
    tighter, correct boundary; only when it precedes the coverage start (the
    backdated-outlier case) do we fall back to the recorded start. Returns None
    when no API rows exist yet, allowing a full import.
    """
    row = conn.execute("SELECT MIN(booking_date) AS lo FROM transactions WHERE source = 'api'").fetchone()
    min_api = row["lo"] if row and row["lo"] else None
    if min_api is None:
        return None
    coverage_start = db.get_state(conn, state_keys.API_COVERAGE_START)
    if coverage_start and min_api < coverage_start[:10]:
        log.warning(
            "earliest API booking %s precedes coverage start %s (backdated row?) — "
            "clipping at the coverage start so one old row can't amputate the backfill",
            min_api,
            coverage_start[:10],
        )
        return coverage_start[:10]
    return min_api


def _read_csv_rows(path: Path) -> csv.DictReader:
    """
    Decode the whole file up front (utf-8-sig, then cp1252) and hand back a reader.

    AIB occasionally emits Windows-1252 — an accented merchant name arrives as a
    lone 0xE9, say. Python's open() decodes lazily *during iteration*, so wrapping
    open() in a try/except never catches the decode; the bytes must be decoded
    before the CSV reader ever sees them.
    """
    data = path.read_bytes()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = data.decode("cp1252")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{path}: could not decode as utf-8 or cp1252: {exc}") from None
    return csv.DictReader(io.StringIO(text))


def import_file(
    conn, path: str | Path, account_map: dict[str, str], clip_before: str | None = None, currency: str = "EUR"
) -> tuple[int, int, int]:
    """
    Import one CSV. Returns (inserted, submitted, skipped). Does not commit.

    Rows are quarantined per row: a malformed date or amount is logged and
    skipped rather than aborting the whole file. Rows on or after `clip_before`
    (the API's coverage start) are clipped out to avoid cross-source
    double-counting.
    """
    path = Path(path)
    rows: list[dict[str, Any]] = []
    skipped = quarantined = clipped = 0
    reader = _read_csv_rows(path)
    if not reader.fieldnames:
        raise ValueError(f"{path}: empty file, no CSV header")
    columns = _resolve_header(list(reader.fieldnames))
    for line_no, row in enumerate(reader, 2):  # line 1 is the header
        try:
            mapped = row_to_transaction(row, columns, account_map, currency)
        except Exception as exc:  # one poisoned row must not abort the backfill
            quarantined += 1
            db.quarantine_row(conn, "csv", str(exc), row, None)
            log.warning("%s line %d: quarantined malformed row: %s", path.name, line_no, exc)
            continue
        if mapped is None:
            skipped += 1
        elif clip_before is not None and mapped["booking_date"] >= clip_before:
            clipped += 1
        else:
            rows.append(mapped)
    inserted, submitted = db.insert_transactions(conn, rows)
    log.info(
        "%s: %d rows, %d new, %d duplicate, %d skipped, %d quarantined, %d clipped(>=API %s)",
        path.name,
        submitted,
        inserted,
        submitted - inserted,
        skipped,
        quarantined,
        clipped,
        clip_before or "-",
    )
    return inserted, submitted, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import AIB CSV exports into the ledger.")
    parser.add_argument("files", nargs="+", help="CSV file(s), e.g. data/backfill/*.csv")
    parser.add_argument("--dry-run", action="store_true", help="parse and report, write nothing")
    parser.add_argument("--db", default=None, help="database path (default: config db_path)")
    parser.add_argument("--config", default=None, help="path to config.yaml")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    cfg = db.load_config(args.config)
    csv_cfg = cfg.get("csv_import", {})
    account_map = {str(k): str(v) for k, v in (csv_cfg.get("account_map") or {}).items()}
    currency = cfg.get("currency", "EUR")

    conn = db.connect(args.db or cfg.get("db_path", "ledger.db"))
    try:
        db.init_db(conn)
        clip_before = compute_clip_before(conn) if csv_cfg.get("clip_to_cursor", True) else None
        if clip_before:
            log.info("clipping CSV rows on/after %s (API already covers that window)", clip_before)
        total_inserted = total_submitted = 0
        for file_path in args.files:
            inserted, submitted, _ = import_file(conn, file_path, account_map, clip_before, currency)
            total_inserted += inserted
            total_submitted += submitted
        if args.dry_run:
            conn.rollback()
            log.info("dry-run: rolled back %d would-be inserts", total_inserted)
        else:
            conn.commit()
        log.info(
            "backfill %s: %d new / %d parsed across %d file(s)",
            "dry-run" if args.dry_run else "done",
            total_inserted,
            total_submitted,
            len(args.files),
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
