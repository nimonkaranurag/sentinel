# Hard rails — account topology (optional)

Sentinel senses and plans; **the account topology enforces**. Nothing here is
automated: Sentinel never initiates payments (no PISP — ever), never stores bank
credentials, and cannot move money. These rails are a ~15-minute manual setup,
fully reversible from AIB internet banking. They complement the safe-to-spend
number (SPEC §4) — you can run Sentinel without them.

## The idea

Discretionary spending moves to a separate card funded by a fixed weekly
standing order. When that card runs dry, discretionary spending stops — no
willpower required. The AIB account keeps paying fixed bills (rent, utilities,
direct debits) untouched, and the safe-to-spend push keeps you honest inside the
week.

## Setup

1. **Get the weekly amount.** Run `uv run python -m sentinel.controller` — it
   logs `suggested weekly rail: €X` (= your monthly discretionary pool
   `budgets.pool_monthly_cents` × 12 ÷ 52). Round DOWN to a tidy number.
2. **The discretionary card.** Use a Revolut account (or open one). Its card —
   physical and in the phone wallet — becomes the ONLY card you carry.
3. **The rail.** AIB internet banking → Payments → Standing order:
   - to: your Revolut IBAN
   - amount: €X from step 1
   - frequency: **weekly**, Friday morning
   - reference: **SENTINEL RAIL**
4. **Disarm the AIB card.** Remove it from Apple/Google Pay and leave it at
   home. It stays valid for the fixed direct debits.
5. **Teach Sentinel the rail.** Add this seed rule to `sentinel/rules.yaml` so
   the weekly transfer auto-categorizes as `Cash` (money converted to untracked
   discretionary spending — this keeps the surplus figure honest, since
   `Transfers` would be excluded from spend math):

   ```yaml
   - pattern: "SENTINEL RAIL"
     category: Cash
   ```

   Without this, rail money looks unspent and last month's surplus flatters you.
   (Alternative, more granular: export Revolut CSV monthly and import it — needs
   a column adapter, not in v1.)

## Upkeep

- The discretionary pool (`budgets.pool_monthly_cents`) is a single config
  number you tune by hand. As you improve, lower it and re-run the controller;
  set the standing order to the new suggested weekly rail. There is no automatic
  ratchet — you decide when to tighten.

## The finish line

The monthly report shows **last month's surplus = Income − total spend** against
a target (`controller.graduation_surplus_cents`; family transfers are categorized
`Transfers` and excluded by construction). When surplus clears the target, the
digest says so — that's the signal the family transfers can start winding down.
The figure is recomputed from the ledger every time, so backfills and `/recat`
corrections apply retroactively (no stored streak to drift out of sync).

## Rollback

Cancel the standing order in AIB internet banking; put the AIB card back in the
wallet. Sentinel keeps working either way — the rails are enforcement, not
sensing.
