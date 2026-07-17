from datetime import UTC, datetime

import pytest

from sentinel.authorize import (
    account_uids,
    aspsps_in_country,
    build_auth_request,
    compute_valid_until,
    extract_code,
    find_named,
)

ASPSPS = [
    {"name": "AIB", "country": "IE", "maximum_consent_validity": 90 * 86400},
    {"name": "Bank of Ireland", "country": "IE"},
    {"name": "Nordea", "country": "FI"},
]

NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)


# ── ASPSP selection ───────────────────────────────────────────────────────


def test_aspsps_in_country_is_case_insensitive():
    got = aspsps_in_country(ASPSPS, "ie")
    assert [a["name"] for a in got] == ["AIB", "Bank of Ireland"]


def test_find_named_hit_and_miss():
    assert find_named(ASPSPS, "AIB")["country"] == "IE"
    assert find_named(ASPSPS, "Ulster Bank") is None


# ── Consent window (clamped to the bank's stated maximum, SPEC §3) ────────


def _days_until(valid_until: str) -> int:
    parsed = datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
    return (parsed - NOW).days


def test_valid_until_is_zulu_rfc3339():
    assert compute_valid_until(NOW, 180).endswith("Z")


def test_valid_until_clamps_to_bank_max():
    # bank allows only 90d, config wants 180d → 90 wins
    assert _days_until(compute_valid_until(NOW, 180, 90 * 86400)) == 90


def test_valid_until_config_wins_when_bank_allows_more():
    # bank allows 200d, config caps at 180d → 180 wins
    assert _days_until(compute_valid_until(NOW, 180, 200 * 86400)) == 180


def test_valid_until_without_bank_limit_uses_config():
    assert _days_until(compute_valid_until(NOW, 180, None)) == 180


# ── POST /auth body ───────────────────────────────────────────────────────


def test_build_auth_request_shape():
    body = build_auth_request(
        "2026-07-15T12:00:00Z", {"name": "AIB", "country": "IE"},
        "https://localhost:8080/callback", "personal", "st8",
    )
    assert body == {
        "access": {"valid_until": "2026-07-15T12:00:00Z"},
        "aspsp": {"name": "AIB", "country": "IE"},
        "redirect_url": "https://localhost:8080/callback",
        "psu_type": "personal",
        "state": "st8",
    }


# ── Redirect-code extraction (the CSRF-sensitive bit) ─────────────────────


def test_extract_code_from_full_url_verifies_state():
    url = "https://localhost:8080/callback?code=ABC123&state=xyz"
    assert extract_code(url, expected_state="xyz") == "ABC123"


def test_extract_code_accepts_bare_code():
    assert extract_code("  ABC123  ") == "ABC123"


def test_extract_code_rejects_state_mismatch():
    with pytest.raises(ValueError, match="CSRF"):
        extract_code(
            "https://localhost:8080/callback?code=A&state=evil",
            expected_state="good",
        )


def test_extract_code_requires_state_when_expected():
    # Stripping the state param must NOT bypass the CSRF check.
    with pytest.raises(ValueError, match="CSRF"):
        extract_code("https://localhost:8080/callback?code=A", expected_state="good")


def test_extract_code_rejects_bare_code_when_state_expected():
    # R26: a bare code carries no state, so it can't be CSRF-verified. In the real
    # handshake (expected_state set) it must be rejected, not silently accepted.
    with pytest.raises(ValueError, match="FULL redirect URL"):
        extract_code("ABC123", expected_state="xyz")


def test_extract_code_surfaces_error_redirect():
    with pytest.raises(ValueError, match="refused"):
        extract_code(
            "https://localhost:8080/callback?error=access_denied"
            "&error_description=user%20cancelled"
        )


def test_extract_code_missing_code():
    with pytest.raises(ValueError, match="code"):
        extract_code("https://localhost:8080/callback?state=xyz")


def test_extract_code_empty():
    with pytest.raises(ValueError, match="nothing pasted"):
        extract_code("   ")


# ── Account uids out of the session ───────────────────────────────────────


def test_account_uids_extracts_and_skips_missing():
    session = {"accounts": [{"uid": "u1"}, {"uid": "u2"}, {"name": "no uid here"}]}
    assert account_uids(session) == ["u1", "u2"]


def test_account_uids_empty_session():
    assert account_uids({}) == []
