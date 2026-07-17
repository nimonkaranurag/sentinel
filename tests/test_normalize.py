import pytest

from sentinel.normalize import display_merchant, normalize

CASES = [
    # processor prefixes (SPEC §3)
    ("SUMUP *BJJ ACADEMY", "BJJ ACADEMY"),
    ("SQ *COFFEE ANGEL", "COFFEE ANGEL"),
    ("ZETTLE_FALAFEL GUY", "FALAFEL GUY"),
    ("PAYPAL *SPOTIFY", "SPOTIFY"),
    ("GOOGLE *YOUTUBE", "YOUTUBE"),
    ("CRV*AMAZON", "AMAZON"),
    ("VDP-TESCO STORES 4368", "TESCO STORES"),
    ("POS LIDL DUBLIN", "LIDL DUBLIN"),
    ("POS SUMUP*CAFE X", "CAFE X"),  # stacked prefixes
    ("APPLE.COM/BILL ITUNES", "ITUNES"),
    ("APPLE.COM/BILL", "APPLE.COM/BILL"),  # alone it IS the merchant
    # trailing store numbers / dates / cities
    ("TESCO STORES 4368 DUBLIN", "TESCO STORES"),
    ("COFFEE ANGEL DUBLIN 2", "COFFEE ANGEL"),
    ("RYANAIR 28JAN", "RYANAIR"),
    ("ALDI 847 SWORDS", "ALDI"),
    ("CENTRA MAYNOOTH", "CENTRA MAYNOOTH"),  # F8: single brand + city stays distinct
    ("SUPERVALU D04", "SUPERVALU"),
    # cities are stripped only from the tail; leading tokens stay
    ("DUBLIN BUS", "DUBLIN BUS"),
    ("DUBLIN", "DUBLIN"),  # never strip the last remaining token
    # uppercase + whitespace collapse
    ("  lidl   dublin ", "LIDL DUBLIN"),
    ("Irish Rail", "IRISH RAIL"),
    # over-strip guard: never return a letterless key
    ("APPLE.COM/BILL 5.99", "APPLE.COM/BILL 5.99"),
    # Enable Banking / AIB enrichment blob (embeds a per-txn timestamp) is cut,
    # so the same merchant on different days collapses to ONE key — dedup and the
    # recurring detector depend on this.
    ("NETFLIX.COM { TRANSACTIONSUBTYPE : PURCHASE, PAYMENTINITIATIONDATETIME : 2026-05-04T09:00:00+01:00 }", "NETFLIX.COM"),
    ("NETFLIX.COM { TRANSACTIONSUBTYPE : PURCHASE, PAYMENTINITIATIONDATETIME : 2026-06-04T09:00:00+01:00 }", "NETFLIX.COM"),
    ("PEARL BRASSERI { CATEGORYDETAIL : LEISURE & ENTERTAINMENT }", "PEARL BRASSERI"),
    # VDC-/VDA- AIB card prefixes (alongside VDP-)
    ("VDC-BOOTS RETAIL", "BOOTS RETAIL"),
    ("VDA-POINT CASH ATM", "POINT CASH ATM"),
]


@pytest.mark.parametrize(("raw", "expected"), CASES)
def test_normalize(raw, expected):
    assert normalize(raw) == expected


@pytest.mark.parametrize(("raw", "_"), CASES)
def test_normalize_is_idempotent(raw, _):
    once = normalize(raw)
    assert normalize(once) == once


def test_normalize_empty_inputs():
    assert normalize(None) == ""
    assert normalize("") == ""
    assert normalize("   ") == ""


def test_display_merchant_strips_the_enrichment_blob_but_keeps_case():
    raw = ("NETFLIX.COM { TRANSACTIONSUBTYPE : PURCHASE, "
           "PAYMENTINITIATIONDATETIME : 2026-05-15T23:37:13+01:00 }")
    assert display_merchant(raw) == "NETFLIX.COM"
    assert display_merchant("Tesco Stores 4368 Dublin") == "Tesco Stores 4368 Dublin"  # case kept
    assert display_merchant(None) == "" and display_merchant("") == ""
