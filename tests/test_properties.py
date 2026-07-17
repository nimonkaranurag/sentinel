"""
Property sweeps over generated inputs.

Each property loops over its whole input space inside ONE test rather than a
per-input parametrization, so `pytest --collect-only` reports the number of
distinct properties, not thousands of preordained cases inflating the count. The
coverage is identical — every input below is still exercised; a failure reports
the offending input.
"""

from sentinel import db
from sentinel.normalize import normalize

# A spread of cent values: zero, tiny, sign, thousands-grouped, millions.
_CENTS = [0, 1, -1, 5, -5, 99, 100, -100, 4567, -4567, 100_099,
          1_368_00, 100_000_099, -100_000_099] + list(range(-250, 250, 7))

# Merchant-string vocabulary: prefixes × brands × trailing junk × cities.
_PREFIXES = ["", "SUMUP *", "SQ *", "VDP-", "POS ", "PAYPAL *", "CRV*"]
_BRANDS = ["TESCO", "COFFEE ANGEL", "LIDL", "NETFLIX.COM", "THE FALAFEL GUY", "BOOTS RETAIL"]
_TRAILERS = ["", " 4368", " 28JAN", " D04", " #12"]
_CITIES = ["", " DUBLIN", " CORK", " IE"]
_GENERATED = [f"{p}{b}{t}{c}"
              for p in _PREFIXES for b in _BRANDS for t in _TRAILERS for c in _CITIES]


def test_to_cents_round_trips_fmt_eur():
    # fmt_eur renders integer cents; to_cents must parse it back exactly (it
    # strips €, spaces and thousands commas). Money must survive the round-trip.
    for cents in _CENTS:
        assert db.to_cents(db.fmt_eur(cents)) == cents, cents


def test_normalize_is_idempotent_over_generated_inputs():
    for raw in _GENERATED:
        once = normalize(raw)
        assert normalize(once) == once, raw
        # And a real letter always survives (never an empty / letterless key).
        assert once == "" or any(ch.isalpha() for ch in once), raw


def test_hash_id_occurrence_is_permutation_stable():
    """Two identical coffees + one distinct row, in any input order, must yield
    the SAME SET of ids — occurrences are assigned by position but identical rows
    are interchangeable, so sums stay correct regardless of order."""
    a = {"account_id": "x", "booking_date": "2026-01-03", "amount_cents": -350,
         "merchant_raw": "COFFEE ANGEL", "source": "csv"}
    b = dict(a)  # byte-identical → occurrence disambiguates
    c = {"account_id": "x", "booking_date": "2026-01-03", "amount_cents": -500,
         "merchant_raw": "LIDL", "source": "csv"}
    ids_1 = {r["id"] for r in db.prepare_transactions([a, b, c])}
    ids_2 = {r["id"] for r in db.prepare_transactions([c, b, a])}
    assert ids_1 == ids_2 and len(ids_1) == 3
