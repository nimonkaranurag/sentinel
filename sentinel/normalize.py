"""
Merchant-name normalization: step 1 of the categorizer cascade (SPEC §3).

Pure functions only. The pipeline uppercases the input, strips processor
prefixes, strips trailing store numbers, dates, and city suffixes, and collapses
whitespace. It is deterministic and idempotent: normalize(normalize(x)) equals
normalize(x).
"""

from __future__ import annotations

import re

# Processor prefixes (SPEC §3), stripped repeatedly from the start.
_PREFIX_PATTERNS = [
    re.compile(r"^SUMUP\s*\*\s*"),
    re.compile(r"^SQ\s*\*\s*"),
    re.compile(r"^ZETTLE_\s*"),
    re.compile(r"^PAYPAL\s*\*\s*"),
    re.compile(r"^GOOGLE\s*\*\s*"),
    re.compile(r"^APPLE\.COM/BILL\s+"),  # only when followed by more text; alone it IS the merchant
    re.compile(r"^CRV\s*\*\s*"),
    re.compile(r"^VD[PCA]-\s*"),  # AIB card prefixes: VDP-/VDC-/VDA-
    re.compile(r"^POS\s+"),
]

# Trailing junk: store numbers, dates ("28JAN", "28/01"), Dublin routing codes.
_TRAILING_TOKEN_PATTERNS = [
    re.compile(r"^#?\d+$"),
    re.compile(r"^\d{1,2}[A-Z]{3}\d{0,4}$"),
    re.compile(r"^\d{1,2}/\d{1,2}(?:/\d{2,4})?$"),
    re.compile(r"^D\d{1,2}$"),
]

# Common Irish city/area suffixes seen on card statements. Trailing tokens only,
# so "DUBLIN BUS" is untouched while "LIDL DUBLIN" → "LIDL".
CITY_SUFFIXES = frozenset({
    "DUBLIN", "CORK", "GALWAY", "LIMERICK", "WATERFORD", "KILKENNY",
    "BRAY", "SWORDS", "TALLAGHT", "DUNDRUM", "BLANCHARDSTOWN", "MAYNOOTH",
    "IE", "IRL", "IRELAND",
})

_HAS_LETTER = re.compile(r"[A-Z]")


def _strip_enrichment_blob(name: str) -> str:
    """
    Cut the Enable Banking / AIB enrichment blob, e.g. "NETFLIX.COM {
    TRANSACTIONSUBTYPE : PURCHASE, ... PAYMENTINITIATIONDATETIME :
    2026-05-15T23:37:13+01:00, ... }". Its embedded per-transaction timestamp
    makes every charge look like a distinct merchant — defeating dedup, the
    recurring detector, and any human reading the name — so drop everything from
    the first brace.
    """
    return name.split("{", 1)[0].strip()


def display_merchant(merchant_raw: str | None) -> str:
    """
    A human-facing merchant label: the raw name with the enrichment blob removed.

    Unlike normalize(), this preserves case and store/city tokens; it is the one
    seam every outbound surface (alerts, digest, /cat) uses so no push ever leaks
    the raw {…} blob.
    """
    return _strip_enrichment_blob(merchant_raw) if merchant_raw else ""


def normalize(merchant_raw: str | None) -> str:
    """
    Normalize a raw merchant string to its canonical lookup key.
    """
    if not merchant_raw:
        return ""
    collapsed = " ".join(merchant_raw.upper().split())

    before_blob = _strip_enrichment_blob(collapsed)
    if before_blob:
        collapsed = before_blob

    s = collapsed
    stripped = True
    while stripped:
        stripped = False
        for pattern in _PREFIX_PATTERNS:
            new = pattern.sub("", s).strip()
            if new and new != s:
                s = new
                stripped = True

    tokens = s.split()
    while len(tokens) > 1:
        tail = tokens[-1]
        if any(p.match(tail) for p in _TRAILING_TOKEN_PATTERNS):
            tokens.pop()                        # store numbers / dates: always strip
        elif tail in CITY_SUFFIXES and len(tokens) > 2:
            tokens.pop()                        # city: only when ≥2 tokens remain, so
        else:                                   # "CAFE DUBLIN" ≢ "CAFE CORK"
            break
    result = " ".join(tokens)

    # Over-stripped to pure digits/punctuation ("APPLE.COM/BILL 5.99" → "5.99"):
    # fall back to the collapsed original rather than a meaningless key.
    if not _HAS_LETTER.search(result):
        return collapsed
    return result
