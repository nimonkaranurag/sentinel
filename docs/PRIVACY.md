# Sentinel — Privacy Policy

_Last updated: 2026-07-15_

Sentinel is a **personal, single-user** application operated by its owner to
observe **their own** bank account data. It is not a service offered to other
people. There are no other users, no accounts to create, and no data collected
from anyone but the operator.

## What data is accessed

- The operator's **own** AIB (ROI) account and transaction data, accessed
  **read-only** through the Enable Banking API under PSD2 Account Information
  Services, and only after the operator completes their bank's Strong Customer
  Authentication (SCA) to grant explicit consent.
- No login credentials for the bank are ever seen or stored. Access is via a
  short-lived token; consent is revocable at any time (see below).

## Where data is stored

- Locally, in a SQLite database on hardware the operator controls (their own
  computer or private server). It is **not** hosted, shared, sold, or published.

## Third parties that receive data

- **Enable Banking** — the regulated aggregator that provides read access to the
  operator's own bank data. Their privacy policy governs that access.
- **Telegram** — delivers notifications to the operator's own private chat.

Categorization, alerts, and all reports run **locally** — there is no LLM and no
third-party AI service. No merchant names, totals, or account data leave the
machine except to the two services above.

No data is transmitted to any other party, and none is used for advertising,
profiling of third parties, or resale.

## Payments

Sentinel is read-only. It **cannot** and does not initiate payments or move
money in any way.

## Retention and deletion

Data persists until the operator deletes it. The operator can revoke bank access
at any time from AIB internet banking (or by letting the consent lapse), and can
delete all stored data by removing the local database.

## Contact

For data-protection questions: **nimonkar.anurag2000@gmail.com**
