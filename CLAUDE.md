# Claude Agent Instructions — Swing Trader

This file is the primary context document for any Claude Code CLI session
working on this project. Read this file first before reading or writing any code.

---

## Project Context

This is a personal automated swing-trading application. Read these docs in order:

1. `docs/project_overview.md` — what the system does and why
2. `docs/architecture.md` — how it is structured
3. `docs/implementation_phases.md` — where we are and what is next

The current phase is tracked in `implementation_phases.md` by checked boxes.
Before writing any code, locate the current phase and understand what the
immediate next unchecked task is.

### Current strategy — SMA-200 regime overlay (adopted 2026-08-01)

Equal-weight the symbols in `SYMBOLS`. Each sleeve is either fully on or fully
off, decided per symbol by a hysteresis band around its own 200-day SMA:

  * held, and close < 0.98 × SMA_200  → exit that sleeve (weight 0)
  * flat, and close > 1.02 × SMA_200  → enter that sleeve (weight 1/N)
  * otherwise                          → hold current state

Rebalance **weekly**, on completed daily bars. Cash sits idle when sleeves are off
(typically ~30% of the time).

**What this strategy is and is not.** It is a drawdown-control device. Validated
2018-11→2026-07 it cut maximum drawdown from −50.0% to −25.6% while reducing CAGR
from 41.1% to 28.9%, and beat a constant-exposure control at matched average
exposure on drawdown and MAR in every window tested. It showed **no** out-of-sample
Sharpe advantage (test half: 1.56 vs 1.57 buy-and-hold) and it beat buy-and-hold in
only 2 of 9 calendar years. Do not describe it as an alpha strategy, and do not
re-tune it toward higher returns — the drawdown reduction is the only effect that
replicated across four universes. See `docs/strategy_validation.md`.

---

## Git Workflow

All work goes through feature branches. Never commit directly to `main`.

Before starting any coding task, create a branch:
```
git checkout main && git pull
git checkout -b feature/<short-description>   # or fix/ or chore/
```

Open a PR to merge back into `main` when the task is complete.

---

## How to Work With Me

### Always Present a Plan Before Writing Code

Before implementing anything non-trivial, output a short plan:
- What file(s) you will create or modify
- What the function signatures will look like
- Any trade-offs or risks in the approach

Wait for a `proceed` or `yes` reply before writing code. For tiny changes
(fixing a typo, adding a missing import), you may proceed directly.

### Ask, Don't Assume

If a requirement is ambiguous, ask one targeted question. Do not make
assumptions that affect correctness, risk management, or money movement.
Specifically: never assume a default that could result in an order being
placed without explicit human intent.

### Minimal Diffs

Change only what is needed for the current task. Do not refactor unrelated
code, rename things for style preferences, or add features not in the
current phase. Scope creep in a financial system introduces bugs.

### Confirm Before Touching Sensitive Areas

Require explicit confirmation before modifying:
- `executor.py` — any function that places orders
- `config.py` — any change to how API credentials are loaded
- `.env` — never suggest writing secrets to this file; instruct the user to do it manually
- `database.py` — schema migrations (existing data may be lost)
- `main.py` — the scheduler loop (changes affect live behavior)

---

## Coding Standards

### Python Style
- Python 3.12+. Use type hints on all function signatures.
- `dataclasses` for structured data (positions, signal results, settings).
- No global mutable state. Pass dependencies explicitly.
- Functions should do one thing. If a function needs a comment to explain
  what it does, it should probably be split.
- Maximum function length: ~40 lines. Beyond that, decompose.

### Error Handling
- Never silently swallow exceptions in the trading loop.
- Network errors (Alpaca API, Telegram) must be caught, logged, and alerted.
- A failed data fetch must skip that symbol, not crash the entire scan.
- A failed order placement must alert via Telegram immediately.
- Use `logging` (stdlib), not `print()`. Log to both console and `logs/app.log`.

### Configuration
- All parameters come from `config.py` / `.env`. No magic numbers in logic files.
- Strategy thresholds (RSI level, EMA proximity %, ATR multipliers) must be
  configurable via env vars without touching code.

### Testing
- Every function in `indicators.py`, `portfolio.py`, and `risk.py` must have
  a unit test in `tests/`.
- Tests must not make real API calls. Mock Alpaca responses.
- A test that passes with fake data but would fail with real data is worse
  than no test.

---

## Risk Management Guardrails (Hard Rules)

These rules must never be violated by generated code, regardless of what
I ask in the moment. If I ask you to bypass one of these, refuse and explain why.

1. **Never place a live order when `ALPACA_PAPER=true`.**
   The executor must check this env var on every order call, not just at startup.

2. **Never place an order without validated target weights.**
   `compute_target_weights()` must be called and its result validated before
   any order is placed. The SMA-200 regime band *is* the risk control for this
   strategy — there is no per-trade stop-loss. A sleeve whose regime state is
   False must have a target weight of exactly 0.

3. **Exposure reductions execute immediately and unconditionally.**
   Any order that *reduces* exposure (sell to a lower target weight, or exit a
   sleeve whose regime turned False) must execute without human approval and
   without depending on Telegram succeeding. Only orders that *increase*
   exposure require the weekly YES. If the reply times out, reductions still
   execute and increases are skipped.

4. **Position size must be computed from account equity, not hardcoded.**
   Target notionals must be derived from equity fetched from Alpaca on every
   rebalance. Never use a cached or hardcoded equity value for sizing.

5. **One sleeve per symbol; each capped at `MAX_POSITION_PCT` of equity.**
   The rebalancer must never hold a symbol not in `SYMBOLS`, never open a
   second sleeve in the same symbol, and never let a computed target notional
   exceed `MAX_POSITION_PCT × equity`.

6. **Never modify `ALPACA_PAPER` or API credentials programmatically.**
   These must only be changed manually in the `.env` file by the user.

7. **A rebalance must never leave the portfolio in a partially-applied state
   silently.** If any order in a rebalance fails, log it, alert via Telegram,
   and persist which orders succeeded. Never assume the target state was
   reached — always re-derive current holdings from Alpaca on the next run.

---

## Alpaca API Notes

- SDK: `alpaca-py` (not the older `alpaca-trade-api`)
- Paper base URL: `https://paper-api.alpaca.markets`
- Live base URL: `https://api.alpaca.markets`
- Data API base URL: `https://data.alpaca.markets`
- Bar timeframe for strategy: `TimeFrame.Day`
- Market orders only. The weekly rebalance places notional (dollar-amount)
  market orders; fractional quantities are expected and `shares` is a float.
- Always check order status after placement — Alpaca paper fills are fast
  but not instant.
- Rate limits: 200 requests/min on free tier. Our 8-symbol scan is well within this.

## Telegram Bot Notes

- Library: `python-telegram-bot` (async version, v20+)
- Bot token and chat ID come from `config.py`
- Reply polling: use `getUpdates` with a short timeout. Do not use webhooks
  in Phase 1 (VPS firewall complexity not worth it for one user).
- Always include the symbol and key signal values in alert messages.
  The user must be able to make an informed YES/NO decision from the
  Telegram message alone, without opening a laptop.

---

## What Not to Build (Yet)

Do not add any of the following unless I explicitly ask in a later phase:

- Web dashboard or UI of any kind
- Crypto trading support
- Multi-user or multi-account support
- Options, futures, or leveraged instruments
- Intraday bars or real-time tick processing
- ML/AI-based signal generation
- Auto-execute of exposure *increases* on timeout (see hard rule 3 — reductions
  are always automatic, increases always need the weekly YES)
- Automatic parameter optimization or walk-forward testing

---

## Session Startup Checklist

When starting a new Claude Code session on this project, do the following
before writing any code:

1. Read this file (`CLAUDE.md` at the project root)
2. Read `docs/implementation_phases.md` and identify the next unchecked task
3. Confirm with the user: "The next task is X. Shall I proceed?"
4. Read any source files relevant to that task
5. Present a plan, wait for approval, then implement
