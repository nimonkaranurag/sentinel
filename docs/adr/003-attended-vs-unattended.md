# ADR 003 — Attended `/sync` vs unattended polls, and the single getUpdates reader

**Status:** accepted · **Date:** 2026-07-15

## Context

PSD2 (RTS Art. 36(5)) limits *unattended* account-information access to roughly
four calls per day, but exempts *attended* access where the PSU is present and
their IP and user-agent are forwarded. Sentinel wants both: a background pulse
that catches charges as they book, and an on-demand "pull now" the owner can
trigger while watching. Separately, Telegram allows exactly one long-poll
`getUpdates` reader per bot; two readers 409 against each other.

## Decision

**Two ingest modes, one allowance.** The four daily cron polls
(`make poll`) are unattended: each consumes one unit of a per-day allowance
counter in `state`, serialized under `BEGIN IMMEDIATE`, and sends no PSU headers.
Owner-initiated `/sync` is attended: it forwards `Psu-Ip-Address` (this host's
real LAN IP, never a loopback fiction) and `Psu-User-Agent`, so the ASPSP exempts
it from the counter. Both paths categorize and fire policy + bill alerts, so a
charge cannot enter the ledger without being checked.

**One getUpdates reader.** Command answering and inline-tap handling live in an
always-on `--listen` process — the sole getUpdates reader. In production that is
`sentinel-listen.service` (systemd, `deploy/systemd/`); on a laptop it is the
`@reboot` flock'd line in `deploy/crontab.txt`. Every scheduled job uses explicit
push-only flags and never calls getUpdates; bare `notify` is push-only for the
same reason. This is what makes the two-tap relabel work in real time instead of
23 hours later.

## Consequences

- **Compliance and headroom.** The unattended budget is never blown by manual
  pulls, and the PSU-present exemption is used exactly as intended.
- **No 409s.** Exactly one process reads updates; the crons cannot collide with
  the listener. The trade-off is that the listener must actually be deployed —
  hence the shipped plist, not a phantom reference.
- **Consent lifecycle.** Consent lasts ≤180 days; the poll runs the T−14d expiry
  nag (idempotent per day) and turns a bank 401/403 into a re-auth message, so
  the product cannot silently go dark over frozen data.
- **Failure honesty.** A 4xx (expired/revoked consent) fails fast instead of
  burning three retries times four polls a day; only connection errors and 5xx
  are retried.
