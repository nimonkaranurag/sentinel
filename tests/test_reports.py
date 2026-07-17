from datetime import date, timedelta

import pytest

from sentinel import db, reports

AS_OF = date(2026, 7, 14)  # window: 2026-04-15 → 2026-07-14
TARGET_RESIDUAL = 12_000   # €120 engineered residual for full months
FULL_MONTH_INFLOWS = 430_000          # salary 3300 + family transfer 1000
FULL_MONTH_FIXED = 169_100            # rent + utilities + tools + bjj
FULL_MONTH_VARIABLE = FULL_MONTH_INFLOWS - FULL_MONTH_FIXED - TARGET_RESIDUAL  # 248,900
COFFEE_TXNS = 19  # 8 (May) + 8 (Jun) + 3 (Jul) — the only <€5 spend

MERCHANTS = {
    "SALARY ACME": "Income",
    "FAMILY TRANSFER": "Transfers",
    "LANDLORD SO": "Rent",
    "ELECTRIC IRELAND": "Utilities",
    "ANTHROPIC": "Tools",
    "BJJ ACADEMY": "Health/Fitness",
    "NETFLIX.COM": "Subscriptions",
    "TESCO STORES": "Groceries",
    "COFFEE ANGEL": "Coffee/Snacks",
    "THE FALAFEL GUY": "Dining",
    "LEAP TOP-UP": "Transport",
    "ATM WITHDRAWAL": "Cash",
    "AVOCA": "Shopping",
    "MYSTERY POS": "Uncategorized",
    "RANDOM BAZAAR": "Shopping",
    "BALANCER SHOP": "Shopping",
    "LAUNDRETTE": "Other",
}


def _laundrette_dates():
    d = date(2026, 4, 18)
    while d <= AS_OF:
        yield d
        d += timedelta(days=7)


def build_ledger(conn):
    ids = {}
    for name, category in MERCHANTS.items():
        cur = conn.execute(
            "INSERT INTO merchants (name_normalized, category, categorized_by, first_seen) "
            "VALUES (?, ?, 'dict', '2026-04-01')", (name, category))
        ids[name] = cur.lastrowid

    rows = []

    def txn(day: date, merchant: str, cents: int):
        rows.append({"account_id": "acc-uid-1", "booking_date": day.isoformat(),
                     "amount_cents": cents, "merchant_raw": merchant,
                     "merchant_id": ids[merchant], "source": "api"})

    for month, random_day, random_cents in ((5, 3, -5_000), (6, 25, -12_000)):
        def d(day):
            return date(2026, month, day)
        txn(d(1), "SALARY ACME", 330_000)
        txn(d(15), "FAMILY TRANSFER", 100_000)
        txn(d(2), "LANDLORD SO", -136_800)
        txn(d(3), "ELECTRIC IRELAND", -18_000)
        txn(d(4), "ANTHROPIC", -2_300)
        txn(d(5), "BJJ ACADEMY", -12_000)
        txn(d(5), "NETFLIX.COM", -1_799)
        for day in (2, 7, 12, 17, 22, 27):
            txn(d(day), "TESCO STORES", -4_500)
        for day in (1, 4, 8, 11, 15, 18, 22, 25):
            txn(d(day), "COFFEE ANGEL", -350)
        for day in (6, 13, 20):
            txn(d(day), "THE FALAFEL GUY", -1_850)
        for day in (9, 23):
            txn(d(day), "LEAP TOP-UP", -2_000)
        txn(d(10), "ATM WITHDRAWAL", -6_000)
        txn(d(12), "AVOCA", -9_900)
        txn(d(14), "MYSTERY POS", -7_500)
        txn(d(random_day), "RANDOM BAZAAR", random_cents)
        laundrette_month = sum(1_200 for x in _laundrette_dates() if x.month == month)
        base_variable = (1_799 + 6 * 4_500 + 8 * 350 + 3 * 1_850 + 2 * 2_000
                         + 6_000 + 9_900 + abs(random_cents) + laundrette_month)
        txn(d(20), "BALANCER SHOP", -(FULL_MONTH_VARIABLE - base_variable))

    # Recurring history + partial months at the window edges.
    txn(date(2026, 4, 5), "NETFLIX.COM", -1_799)   # before window; detector history
    txn(date(2026, 4, 20), "RANDOM BAZAAR", -1_000)
    for day in _laundrette_dates():
        txn(day, "LAUNDRETTE", -1_200)
    txn(date(2026, 7, 1), "SALARY ACME", 330_000)
    txn(date(2026, 7, 2), "LANDLORD SO", -136_800)
    txn(date(2026, 7, 5), "NETFLIX.COM", -1_799)
    for day in (3, 7, 11):
        txn(date(2026, 7, day), "COFFEE ANGEL", -350)

    inserted, submitted = db.insert_transactions(conn, rows)
    assert inserted == submitted == len(rows)
    conn.commit()


def make_cfg(tmp_path):
    return {
        "db_path": str(tmp_path / "ledger.db"),
        "reports": {
            "output_dir": str(tmp_path / "reports"),
            "window_days": 90,
            "monthly_budget_cents": None,
            "recurring": {"min_occurrences": 3, "amount_cv_max": 0.10,
                          "monthly_days": [28, 32], "weekly_days": [6, 8],
                          "mad_max_days": 3},
        },
    }


@pytest.fixture()
def diagnosed(tmp_path):
    conn = db.connect(tmp_path / "ledger.db")
    db.init_db(conn)
    build_ledger(conn)
    data = reports.run_reports(conn, make_cfg(tmp_path), as_of=AS_OF)
    yield conn, data, tmp_path / "reports"
    conn.close()


# ── The gate itself ───────────────────────────────────────────────────────


def test_gate_full_month_residual_under_200(diagnosed):
    _, data, _ = diagnosed
    full = {m["month"]: m for m in data["months"] if m["full_month"]}
    assert set(full) == {"2026-05", "2026-06"}
    for m in full.values():
        assert m["gate_pass"], m
        assert m["residual_cents"] == TARGET_RESIDUAL
        assert abs(m["residual_cents"]) < reports.RESIDUAL_GATE_CENTS
        assert m["inflows_cents"] == FULL_MONTH_INFLOWS
        assert m["fixed_cents"] == FULL_MONTH_FIXED
        assert m["variable_cents"] == FULL_MONTH_VARIABLE


def test_reconciliation_identity_is_exact(diagnosed):
    """residual == uncategorized + transfers_out + net saved (integer-exact)."""
    _, data, _ = diagnosed
    assert data["months"], "window must contain months"
    for m in data["months"]:
        assert (m["residual_cents"]
                == m["uncategorized_cents"] + m["transfers_out_cents"] + m["net_cents"]), m


# ── Required report sections ──────────────────────────────────────────────


def test_category_pareto_properties(diagnosed):
    _, data, _ = diagnosed
    cats = data["categories"]
    assert sum(c["spend_cents"] for c in cats) == data["spend_cents"]
    names = [c["category"] for c in cats]
    assert "Transfers" not in names and "Income" not in names
    assert dict((c["category"], c["spend_cents"]) for c in cats)["Uncategorized"] == 15_000
    cums = [c["cum_pct"] for c in cats]
    assert cums == sorted(cums) and abs(cums[-1] - 100.0) < 1e-6
    assert cats[0]["spend_cents"] == max(c["spend_cents"] for c in cats)


def test_merchant_pareto_top25(diagnosed):
    _, data, _ = diagnosed
    merchants = data["merchants"]
    assert len(merchants) <= 25
    assert merchants[0]["merchant"] == "LANDLORD SO"  # 3 months of rent
    assert merchants[0]["spend_cents"] == 3 * 136_800


def test_size_bands_small_taps(diagnosed):
    _, data, _ = diagnosed
    bands = {b["band"]: b for b in data["size_bands"]}
    assert list(bands) == ["<€5", "€5–15", "€15–40", "€40–100", ">€100"]
    assert bands["<€5"]["count"] == COFFEE_TXNS
    assert bands["<€5"]["total_cents"] == COFFEE_TXNS * 350
    assert sum(b["count"] for b in bands.values()) == data["txns"]
    assert sum(b["total_cents"] for b in bands.values()) == data["spend_cents"]


def test_weekday_profile_sums(diagnosed):
    _, data, _ = diagnosed
    profile = data["weekday"]
    assert len(profile["days"]) == 7
    assert profile["weekday_cents"] + profile["weekend_cents"] == data["spend_cents"]
    weekend = sum(d["spend_cents"] for d in profile["days"] if d["day"] in ("Sat", "Sun"))
    assert profile["weekend_cents"] == weekend


def test_recurring_detector(diagnosed):
    _, data, _ = diagnosed
    subs = {s["merchant"]: s for s in data["recurring"]}
    netflix = subs["NETFLIX.COM"]
    assert netflix["period"] == "monthly"
    assert netflix["amount_cents"] == 1_799
    assert netflix["annualized_cents"] == 1_799 * 12
    assert netflix["next_expected"] == "2026-08-04"  # Jul 5 + median 30d
    assert netflix["occurrences"] == 4
    laundrette = subs["LAUNDRETTE"]
    assert laundrette["period"] == "weekly"
    assert laundrette["annualized_cents"] == 1_200 * 52
    assert "RANDOM BAZAAR" not in subs  # wild amounts/intervals must not flag
    assert data["recurring"] == sorted(data["recurring"],
                                       key=lambda s: -s["annualized_cents"])


def test_burn_budget_is_full_month_average(diagnosed):
    _, data, _ = diagnosed
    assert data["budget_cents"] == FULL_MONTH_FIXED + FULL_MONTH_VARIABLE + 7_500
    assert data["budget_source"] == "trailing full-month average"


# ── Files, zero-LLM, dry-run, empty-DB ────────────────────────────────────


def test_files_written_with_expected_content(diagnosed):
    _, data, outdir = diagnosed
    expected = {"EXPENSE_REPORT.md", "subscriptions.md", "category_pareto.png",
                "merchant_pareto.png", "size_bands.png", "weekday_profile.png",
                "burn_rate.png"}
    assert set(data["files"]) == expected
    for name in expected:
        assert (outdir / name).stat().st_size > 0
    diagnosis = (outdir / "EXPENSE_REPORT.md").read_text(encoding="utf-8")
    assert "GATE PASS" in diagnosis
    assert "## 7. Gap reconciliation" in diagnosis
    assert "2026-04 (partial)" in diagnosis
    subs = (outdir / "subscriptions.md").read_text(encoding="utf-8")
    assert "NETFLIX.COM" in subs and "2026-08-04" in subs


def test_no_llm_dependency():
    assert "llm" not in vars(reports), "reports.py must not import an LLM gateway"


def test_dry_run_writes_nothing(tmp_path):
    conn = db.connect(tmp_path / "ledger.db")
    db.init_db(conn)
    build_ledger(conn)
    data = reports.run_reports(conn, make_cfg(tmp_path), as_of=AS_OF, dry_run=True)
    assert data["months"], "computation still happens on dry-run"
    assert data["files"] == []
    assert not (tmp_path / "reports").exists()
    conn.close()


def test_empty_database_does_not_crash(tmp_path):
    conn = db.connect(tmp_path / "ledger.db")
    db.init_db(conn)
    data = reports.run_reports(conn, make_cfg(tmp_path), as_of=AS_OF)
    assert data["months"] == [] and data["recurring"] == []
    assert set(data["files"]) == {"EXPENSE_REPORT.md", "subscriptions.md"}  # charts skipped
    text = (tmp_path / "reports" / "EXPENSE_REPORT.md").read_text(encoding="utf-8")
    assert "residual verdict pending" in text
    conn.close()
