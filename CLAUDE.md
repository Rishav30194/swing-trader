# Claude Agent Instructions — Swing Trader

Read this before touching any code. Then read `README.md` for what the app is
and `docs/architecture.md` for how it is built.

---

## Status

Built and tested. **Not deployed and holding no money.** There is no server to
update and no live position to protect.

The README states plainly that buying the 8 stocks and leaving them alone beat
this app over 2018–2026. That is not a bug to be fixed by tuning — it is the
measured result, and it must stay in the README.

---

## The strategy

Equal-weight 8 symbols. Each sleeve is either fully on or fully off, decided per
symbol by a hysteresis band around its own 200-day SMA:

- held, and close < 0.98 × SMA_200 → exit that sleeve (weight 0)
- flat, and close > 1.02 × SMA_200 → enter that sleeve (weight 1/N)
- otherwise → keep the current state

Rebalance **weekly**, on completed daily bars. Cash sits idle when sleeves are
off (~26% of the time).

**This is a drawdown-control device, not an alpha strategy.** It reduces the
worst peak-to-trough loss by roughly half and gives up return to do it. It has
no out-of-sample risk-adjusted advantage and beat buy-and-hold in only 2 of 9
calendar years. Do not describe it as market-beating, and do not re-tune it
toward higher returns — drawdown reduction is the only effect that held up
across four different universes.

**This targets a general TAXABLE account.** Every sell realises a capital gain.
Any change that increases turnover must be argued on after-tax terms.
`DRIFT_TOLERANCE` (default 5%) is effectively a tax dial: at 0.1% drift trades
were 94% of all orders and bought nothing.

---

## Git workflow

All work goes through feature branches. Never commit directly to `main`.

```
git checkout main && git pull
git checkout -b feature/<short-description>   # or fix/ or chore/
```

Open a PR to merge back. Commit messages: short imperative subject, no period.
**Do not add Co-Authored-By trailers.**

---

## How to work with me

### Present a plan before writing code

For anything non-trivial, output a short plan first — files to touch, function
signatures, trade-offs — and wait for `proceed` or `yes`. Typos and missing
imports you may just fix.

### Ask, don't assume

If a requirement is ambiguous, ask one targeted question. Never assume a default
that could result in an order being placed without explicit human intent.

### Minimal diffs

Change only what the task needs. Do not refactor unrelated code or add features
not asked for. Scope creep in a financial system introduces bugs.

### Confirm before touching sensitive areas

Require explicit confirmation before modifying:
- `executor.py` — anything that places orders
- `config.py` — credential loading
- `.env` — never write to it; instruct the user to edit it manually
- `database.py` — schema changes
- `main.py` — the rebalance flow

---

## Risk guardrails (hard rules)

Never violate these, regardless of what I ask in the moment. If I ask you to
bypass one, refuse and explain why.

1. **Never place a live order when `ALPACA_PAPER=true`.**
   The executor checks this env var on every order call, not just at startup.

2. **Never place an order without validated target weights.**
   `compute_target_weights()` must run and `validate_target_weights()` must pass
   first. The SMA-200 band *is* the risk control — there is no per-trade
   stop-loss. A sleeve whose regime is False must have a target weight of
   exactly 0.

3. **Exposure reductions execute immediately and unconditionally.**
   Any order that *reduces* exposure must execute without human approval and
   without depending on Telegram succeeding. Only *increases* require the weekly
   YES. On timeout, reductions still execute and increases are skipped.

4. **Position size comes from STRATEGY capital, not the account balance.**
   Sizing uses `compute_strategy_equity()` — managed sleeve value plus the
   strategy's own cash ledger, seeded from `TRADING_CAPITAL`. A $100,000 account
   must still trade the $1,000 allocated to it. Never size off
   `get_account_equity()` directly; account equity and cash are ceilings only.
   `TRADING_CAPITAL` is required — the app must refuse to start rather than
   guess how much money to deploy.

5. **One sleeve per symbol, each capped at `MAX_POSITION_PCT`.**
   Never hold a symbol outside `SYMBOLS`, never open a second sleeve in the same
   symbol, never let a target notional exceed `MAX_POSITION_PCT × equity`.

6. **Never modify `ALPACA_PAPER` or API credentials programmatically.**
   Only the user changes those, by hand, in `.env`.

7. **A rebalance must never leave the portfolio partially applied silently.**
   If an order fails, log it, alert via Telegram, and record what succeeded.
   Never assume the target state was reached — always re-derive holdings from
   Alpaca on the next run.

---

## Coding standards

### Python
- Python 3.12+. Type hints on all function signatures.
- `dataclasses` for structured data.
- No global mutable state. Pass dependencies explicitly.
- Functions do one thing. Max ~40 lines; beyond that, decompose.
- `logging`, never `print()`. Console and `logs/app.log`.

### Errors
- Never silently swallow exceptions in the rebalance path.
- Network errors must be caught, logged, and alerted.
- A failed data fetch skips that symbol — it must never liquidate the sleeve.
- A failed order must alert via Telegram immediately.

### Configuration
- All parameters come from `config.py` / `.env`. No magic numbers in logic files.
- Strategy thresholds must be configurable without touching code.

### Testing
- Every function in `indicators.py`, `portfolio.py`, `data.py` and the
  orchestration in `main.py` must have unit tests.
- No real API calls in tests. Mock Alpaca and Telegram.
- Test the failure modes, not the happy path: unfilled orders, partial fills,
  Telegram unreachable during a sell, data outages, approval timeouts.
- A test that passes with fake data but would fail with real data is worse than
  no test.

---

## Alpaca notes

- SDK: `alpaca-py` (not the older `alpaca-trade-api`)
- Paper: `https://paper-api.alpaca.markets` · Live: `https://api.alpaca.markets`
- Bar timeframe: `TimeFrame.Day`
- Market orders only, notional (dollar-amount). Fractional quantities expected;
  `shares` is a float.
- Always check order status after placement — submitting without an exception is
  not the same as filling.
- Rate limit: 200 requests/min on the free tier. An 8-symbol weekly run is
  nowhere near it.

## Telegram notes

- `python-telegram-bot` v20+ (async), wrapped with `asyncio.run()`
- Reply polling via `getUpdates`. No webhooks.
- Alert messages must carry enough detail to decide from the phone alone —
  symbol, close, SMA_200, and the gap between them.
- Every send function returns `bool` and never raises. Hard rule 3 depends on it.

---

## Do not build

Unless explicitly asked:

- Web dashboard or UI
- Crypto, options, futures, or leveraged instruments
- Multi-user or multi-account support
- Intraday bars or real-time tick processing
- ML/LLM-based signal generation
- Auto-execution of exposure *increases* on timeout (see hard rule 3)
- Automatic parameter optimisation
