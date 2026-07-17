"""
Enable Banking authorization handshake (SPEC §6, Phase 0).

An interactive helper for the one-time authorization and each subsequent re-auth
(consent lasts at most 180 days). It:

  1. GET  /aspsps    — list banks and select AIB (ROI)
  2. POST /auth      — start an authorization session and print the SCA url
  3. the owner opens the url, completes AIB's SCA, and pastes the redirect back
  4. POST /sessions  — exchange the one-time code for a session and account uids
  5. writes eb_account_uids and consent_expiry into the `state` table

ingest.py assumes this step has already run: it reads `eb_account_uids` and
`consent_expiry` from state but never creates them. Re-running this after a
consent lapse overwrites them.

No bank credentials are entered here or stored: the owner authenticates directly
with AIB inside AIB's own redirect, and this module holds only the short-lived
authorization `code` and the resulting opaque account uids. Authentication to
Enable Banking uses a fresh RS256 JWT per call (via ingest.make_jwt), so a slow
SCA does not race the 1-hour token TTL.

CLI: python -m sentinel.authorize [--dry-run] [--db PATH] [--config PATH]
  --dry-run: authenticate, list the country's banks, and print the exact POST
             /auth body that would be sent, without starting a live session or
             writing state. A safe preflight that confirms the key works and
             shows AIB's exact ASPSP name.
Exit codes: 0 ok · 2 Phase 0 env/config incomplete · 3 API/HTTP error · 4 no accounts
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests
from dotenv import load_dotenv

from . import db, state_keys
from .ingest import DEFAULT_BASE_URL, HTTP_TIMEOUT, make_jwt

log = logging.getLogger(__name__)


# ── HTTP (JWT-authed, per Enable Banking auth docs) ───────────────────────


def _api_headers(app_id: str, key_path: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {make_jwt(app_id, key_path)}",
        "Content-Type": "application/json",
    }


def _raise_for_status(resp: requests.Response) -> None:
    """
    Log the Enable Banking error body before raising, since their 4xx bodies
    explain the underlying problem (bad aspsp name, expired token, and so on).
    """
    if not resp.ok:
        log.error("%s %s → HTTP %s: %s", resp.request.method, resp.url,
                  resp.status_code, (resp.text or "")[:800])
    resp.raise_for_status()


def get_aspsps(base_url: str, app_id: str, key_path: str) -> list[dict[str, Any]]:
    resp = requests.get(f"{base_url}/aspsps", headers=_api_headers(app_id, key_path),
                        timeout=HTTP_TIMEOUT)
    _raise_for_status(resp)
    body = resp.json()
    return body.get("aspsps", []) if isinstance(body, dict) else body


def start_authorization(base_url: str, body: dict[str, Any],
                        app_id: str, key_path: str) -> dict[str, Any]:
    resp = requests.post(f"{base_url}/auth", json=body,
                         headers=_api_headers(app_id, key_path), timeout=HTTP_TIMEOUT)
    _raise_for_status(resp)
    return resp.json()


def create_session(base_url: str, code: str,
                   app_id: str, key_path: str) -> dict[str, Any]:
    resp = requests.post(f"{base_url}/sessions", json={"code": code},
                         headers=_api_headers(app_id, key_path), timeout=HTTP_TIMEOUT)
    _raise_for_status(resp)
    return resp.json()


# ── Pure helpers (unit-tested; no I/O) ────────────────────────────────────


def aspsps_in_country(aspsps: list[dict[str, Any]], country: str) -> list[dict[str, Any]]:
    want = (country or "").upper()
    return [a for a in aspsps if (a.get("country") or "").upper() == want]


def find_named(aspsps: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for a in aspsps:
        if a.get("name") == name:
            return a
    return None


def compute_valid_until(now_utc: datetime, max_days: int,
                        bank_max_seconds: int | None = None) -> str:
    """
    Return the RFC3339 (Zulu) consent expiry, clamped to the bank's stated
    maximum.

    Enable Banking rejects a valid_until beyond the ASPSP's
    maximum_consent_validity, so the result is the smaller of the configured
    limit (180 days, SPEC §6) and what the bank allows.
    """
    days = int(max_days)
    if bank_max_seconds:
        days = min(days, int(bank_max_seconds) // 86400)
    days = max(days, 1)
    exp = (now_utc + timedelta(days=days)).astimezone(UTC)
    return exp.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_auth_request(valid_until: str, aspsp: dict[str, Any], redirect_url: str,
                       psu_type: str, state: str) -> dict[str, Any]:
    """
    Build the POST /auth body. A minimal `access` of just valid_until grants the
    default account-information scope.
    """
    return {
        "access": {"valid_until": valid_until},
        "aspsp": {"name": aspsp["name"], "country": aspsp["country"]},
        "redirect_url": redirect_url,
        "psu_type": psu_type,
        "state": state,
    }


def extract_code(pasted: str, expected_state: str | None = None) -> str:
    """
    Extract the `code` from a pasted redirect URL, verifying the `state` echo.

    When `expected_state` is set (the real handshake), the full redirect URL is
    required: a bare code carries no state, so accepting one would bypass the
    CSRF guard. An `error=` redirect is surfaced rather than failing opaquely
    downstream. A bare code is accepted only when no state is expected.
    """
    s = (pasted or "").strip()
    if not s:
        raise ValueError("nothing pasted")
    if s.lower().startswith("http"):
        query = parse_qs(urlparse(s).query)
        if "error" in query:
            detail = (query.get("error_description") or query.get("error") or ["error"])[0]
            raise ValueError(f"authorization was refused: {detail}")
        code = (query.get("code") or [""])[0]
        state = (query.get("state") or [""])[0]
        if not code:
            raise ValueError("no ?code= found in the pasted redirect URL")
        if expected_state and state != expected_state:
            raise ValueError("state missing or mismatched — possible CSRF; aborting")
        return code
    if expected_state:
        raise ValueError("paste the FULL redirect URL, not just the code — the state "
                         "parameter is required to verify it (CSRF guard)")
    return s  # a bare code, only when no state verification is expected


def account_uids(session: dict[str, Any]) -> list[str]:
    return [a["uid"] for a in session.get("accounts", []) if a.get("uid")]


# ── Interactive bits (CLI edge only) ──────────────────────────────────────


def _describe(index: int, aspsp: dict[str, Any]) -> str:
    bits = [f"[{index}]", str(aspsp.get("name"))]
    if aspsp.get("country"):
        bits.append(f"({aspsp['country']})")
    if aspsp.get("bic"):
        bits.append(str(aspsp["bic"]))
    psu = aspsp.get("psu_types")
    if psu:
        bits.append("psu:" + ",".join(psu))
    if aspsp.get("sandbox"):
        bits.append("[SANDBOX]")
    return "  " + " ".join(bits)


def _prompt_choice(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    print("\nBanks available for this country:")
    for i, a in enumerate(candidates):
        print(_describe(i, a))
    while True:
        raw = input(f"\nPick a bank [0-{len(candidates) - 1}] (AIB personal, ROI): ").strip()
        if raw.isdigit() and 0 <= int(raw) < len(candidates):
            return candidates[int(raw)]
        print("  not a valid choice; try again.")


def _print_sca_banner(url: str, redirect_url: str) -> None:
    line = "=" * 72
    print("\n" + line)
    print("1. Open this URL in your browser and complete AIB's login + SCA:\n")
    print("   " + url + "\n")
    print("2. Your browser will then land on a 'can't reach this page' at")
    print(f"   {redirect_url}?code=...  — that failure is EXPECTED (nothing runs there).")
    print("3. Copy the FULL address from the address bar and paste it below.")
    print(line)


# ── Orchestration ─────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Complete the Enable Banking authorization handshake (SPEC §6, Phase 0).")
    parser.add_argument("--dry-run", action="store_true",
                        help="authenticate + list banks + print the POST /auth body, "
                             "but start no session and write no state")
    parser.add_argument("--db", default=None, help="database path (default: config db_path)")
    parser.add_argument("--config", default=None, help="path to config.yaml")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    load_dotenv()
    cfg = db.load_config(args.config)
    eb = cfg.get("enable_banking", {})

    app_id = os.environ.get("ENABLE_BANKING_APP_ID")
    key_path = os.environ.get("ENABLE_BANKING_PRIVATE_KEY_PATH")
    if not app_id or not key_path:
        log.error("Phase 0 incomplete: set ENABLE_BANKING_APP_ID and "
                  "ENABLE_BANKING_PRIVATE_KEY_PATH in .env (SPEC §6 runbook)")
        return 2
    if not Path(key_path).is_file():
        log.error("private key not found at %s (path only, key stays outside the repo)", key_path)
        return 2

    redirect_url = eb.get("redirect_url")
    if not redirect_url:
        log.error("set enable_banking.redirect_url in config.yaml (must match the "
                  "URL registered with the app)")
        return 2
    base_url = eb.get("base_url", DEFAULT_BASE_URL).rstrip("/")
    country = eb.get("aspsp_country", "IE")
    aspsp_name = eb.get("aspsp_name")
    psu_type = eb.get("psu_type", "personal")
    max_days = int(eb.get("consent_max_days", 180))

    try:
        all_aspsps = get_aspsps(base_url, app_id, key_path)
        candidates = aspsps_in_country(all_aspsps, country)
        if not candidates:
            log.error("no ASPSPs for country %s (%d banks returned)", country, len(all_aspsps))
            return 4

        if aspsp_name:
            chosen = find_named(candidates, aspsp_name)
            if not chosen:
                log.error("aspsp_name %r not among %d %s banks; --dry-run lists them",
                          aspsp_name, len(candidates), country)
                return 2
        elif args.dry_run or len(candidates) == 1:
            chosen = candidates[0]  # dry-run preview uses the first as a placeholder
        else:
            chosen = _prompt_choice(candidates)

        bank_max = chosen.get("maximum_consent_validity")
        valid_until = compute_valid_until(datetime.now(UTC), max_days, bank_max)
        state = os.urandom(16).hex()
        auth_body = build_auth_request(valid_until, chosen, redirect_url, psu_type, state)

        if args.dry_run:
            print(f"\n{len(candidates)} bank(s) in {country}:")
            for i, a in enumerate(candidates):
                print(_describe(i, a))
            print("\nWould POST /auth with:\n" + json.dumps(auth_body, indent=2))
            print("\n(dry-run: no session started, no state written. Set "
                  "enable_banking.aspsp_name to the exact name above to skip the prompt.)")
            return 0

        auth = start_authorization(base_url, auth_body, app_id, key_path)
        _print_sca_banner(auth["url"], redirect_url)
        pasted = input("\nPaste the FULL redirect URL from the address bar: ")
        code = extract_code(pasted, expected_state=state)

        session = create_session(base_url, code, app_id, key_path)
    except requests.RequestException as exc:
        log.error("Enable Banking API call failed: %s", exc)
        return 3
    except ValueError as exc:
        log.error("%s", exc)
        return 3

    uids = account_uids(session)
    if not uids:
        log.error("session created but no accounts returned — nothing to ingest")
        return 4
    consent_expiry = (session.get("access") or {}).get("valid_until") or valid_until

    conn = db.connect(args.db or cfg.get("db_path", "ledger.db"))
    try:
        db.init_db(conn)
        db.set_state(conn, state_keys.EB_ACCOUNT_UIDS, json.dumps(uids))
        db.set_state(conn, state_keys.CONSENT_EXPIRY, consent_expiry)
        db.set_state(conn, state_keys.EB_SESSION_ID, session.get("session_id", ""))
        db.set_state(conn, state_keys.EB_ASPSP, json.dumps(
            {"name": chosen["name"], "country": chosen["country"]}))
        conn.execute("DELETE FROM state WHERE key = ?",
                     (state_keys.CONSENT_WARNED_ON,))  # re-arm the T−14d nag
        conn.commit()
    finally:
        conn.close()

    days_left = (datetime.fromisoformat(consent_expiry.replace("Z", "+00:00"))
                 - datetime.now(UTC)).days
    log.info("authorized %s (%s): %d account(s), consent valid until %s (~%d days)",
             chosen["name"], chosen["country"], len(uids), consent_expiry[:10], days_left)
    print(f"\n✅ Phase 0 auth done. {len(uids)} account uid(s) stored in state; "
          f"consent expires {consent_expiry[:10]}.\n   Next: `make poll` to pull, "
          f"categorize, and alert (or `make backfill` for the 12-month CSV first).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
