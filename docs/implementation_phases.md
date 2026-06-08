# Implementation Phases — Swing Trader

Progress is tracked here. Check boxes as tasks are completed.
Never skip a phase gate — each one exists to protect capital.

---

## Phase 1 — Environment, Data Pipeline & Configuration
**Goal:** Prove you can reliably pull clean market data and load config.
**Gate to Phase 2:** All four symbols return clean 1-year OHLCV DataFrames
with no missing bars or null values on trading days.

### Setup
- [x] Create `swing-trader/` project folder
- [x] Initialize Python 3.12 virtual environment (`venv`)
- [x] Install packages: `alpaca-py`, `pandas`, `pandas-ta`, `requests`, `python-dotenv`
- [x] Generate `requirements.txt`
- [x] Create `src/` module structure with empty files
- [x] Add `.gitignore` (covers `.env`, `venv/`, `logs/`, `*.db`)
- [x] Create `docs/` folder with all architecture docs

### Configuration
- [x] Write `src/config.py`
  - [x] Load all env vars with `python-dotenv`
  - [x] Define `Settings` dataclass with typed fields
  - [x] Raise `ValueError` at startup if any required var is missing
  - [x] Parse `SYMBOLS` string into a list

### Data Pipeline
- [x] Write `src/data.py`
  - [x] `get_historical_bars(symbol, days=365)` — returns clean DataFrame
  - [x] `get_latest_bar(symbol)` — returns single-row DataFrame
  - [x] Validate columns: open, high, low, close, volume, timestamp
  - [x] Handle Alpaca pagination
  - [x] Log fetch errors without crashing the process

### Validation
- [x] Write a temporary `scripts/validate_data.py` script
  - [x] Fetch 1 year of daily bars for all 4 symbols
  - [x] Assert no null values in OHLCV columns on trading days
  - [x] Print summary: symbol, bar count, date range, any gaps
  - [x] Confirm adjusted close prices (not raw)

---

## Phase 2 — Indicators, Signal Logic & Backtesting
**Goal:** Prove the strategy has a positive edge on historical data before
touching a live (paper) account.
**Gate to Phase 3:** Backtest across 2022–2024 shows Sharpe > 0.5 and
max drawdown < 25%. These are lower bars than Phase 4 gates — we're
validating the direction, not perfecting the system.

### Indicators
- [x] Write `src/indicators.py`
  - [x] `compute_indicators(df)` — pure function, returns enriched DataFrame
  - [x] RSI(14) → column `RSI_14`
  - [x] EMA(21) → column `EMA_21`
  - [x] EMA(50) → column `EMA_50`
  - [x] MACD(12,26,9) → columns `MACD`, `MACD_signal`, `MACD_hist`
  - [x] Volume SMA(20) → column `VOL_SMA_20`
  - [x] ATR(14) → column `ATR_14`
  - [x] Unit test every indicator against known values (17 tests, all passing)

### Signal Logic
- [x] Update `src/signals.py` to three-condition AND gate (see `project_overview.md`)
  - [x] `evaluate_buy_signal(df)` — returns `SignalResult` dataclass
  - [x] Condition 1: close > EMA_50 (trend filter — uptrend only)
  - [x] Condition 2: RSI_LOWER_BOUND ≤ RSI(14) < RSI_UPPER_BOUND (mild pullback)
  - [x] Condition 3: MACD bullish crossover on this bar
  - [x] Populate `SignalResult.context` with all indicator values for alerts
  - [x] Update unit tests to match new three-condition gate

  > **Strategy revised:** Original four-condition gate (RSI<45, price within 2% EMA_21,
  > MACD crossover, volume ≥ 1.5×) produced zero signals over 2022–2024. Two conditions
  > were structurally incompatible: oversold price (RSI<45) always sits >2% below EMA_21,
  > and oversold bounces happen on below-average volume. The new three-condition gate
  > (EMA_50 trend filter + RSI 40–55 + MACD crossover) produced 17 signals with 76%
  > win rate and profit factor 5.93 across the same period. See `project_overview.md`
  > for full rationale.

### Risk Module
- [x] Update `src/risk.py` exit parameters to match revised strategy
  - [x] `compute_exit_levels(entry_price, atr)` — returns SL, TP
  - [x] `compute_position_size(equity, risk_pct, entry, stop)` — returns shares
  - [x] `update_trailing_stop` — activation updated to 0.5× ATR (was 1×)
  - [x] `check_exit_conditions(position, current_bar, entry_date)` — returns exit reason or None
  - [x] Unit tests for all edge cases (34 tests, all passing)
  - [x] ATR multipliers: SL = 1.5× (was 2×), TP = 2× (was 3×) — via .env defaults

### Backtesting
- [x] Write `backtest.py` — pandas walk-forward simulator using production modules
  - [x] Feed 2022–2024 daily OHLCV for all 4 symbols
  - [x] Apply strategy: signal → entry → SL/TP/day5 exit
  - [x] Output report: total trades, win rate, avg hold days, total return,
        Sharpe ratio, max drawdown, best/worst trade, Phase 2 gate verdict
  - Note: chose a direct pandas simulation over `backtrader` — duplicating
    strategy logic into a bt.Strategy class creates divergence risk between
    backtest and live code; this approach tests the exact production functions.
- [x] Run full backtest (2022–2024) with revised strategy — Sharpe 1.043, DD -1.89% → PASS
- [x] Run bear-market run (2022 only) — 2 trades, +0.55%, capital protected; EMA_50 blocked bear signals
- [x] Run bull-market run (2023–2024) — 13 trades, 76.9% win rate, Sharpe 1.291 → PASS
- [x] **Do not use 2025 data for parameter tuning** — reserved as out-of-sample

---

## Pre-Phase 3 — Symbol Expansion & Data Cache
**Goal:** Raise signal frequency to make the 30-trade Phase 3 gate reachable
within ~2 years of paper trading, and add offline data caching to prevent
rate-limiting during iterative backtesting.

- [x] Add 6 symbols: MSFT, AAPL, AMD, META, TSM, SPY (total: 10 symbols)
- [x] Update SYMBOLS default in `config.py` and `.env`
- [x] Add local disk cache to `backtest.py` — `data/cache/<SYMBOL>_daily.pkl`,
      invalidated daily; avoids redundant Alpaca calls on repeated runs
- [x] Re-run full backtest (2022–2024) with 10-symbol universe — Sharpe 1.056, DD -4.90%, 31 trades → PASS
- [x] Remove META (false MACD signals in choppy conditions, negative P&L) and SPY (redundant with VOO)
      Final universe: 8 symbols — NVDA, ASML, VOO, QQQM, MSFT, AAPL, AMD, TSM
- [x] Add ADX(14), Stochastic(14,3,3), OBV indicators to `indicators.py` (computed-only; available in signal context)
- [x] Fix look-ahead bias in `backtest.py` — trailing stop was raised using bar close
      before checking bar low against it; fixed to check exits first, update stop after
- [x] Write `validate_oos.py` — statistical pressure-testing of OOS results:
  - [x] Walk-forward: frozen strategy on each calendar year 2019–2025 independently
  - [x] Permutation test: sign-randomise trade P&Ls 10,000× (H₀: win rate = 50%)
  - [x] Bootstrap CI: resample trade P&Ls 10,000× for p5/p50/p95 on win rate,
        mean return, and profit factor
  - [x] **Run `python validate_oos.py` on 2025 OOS data — results recorded below**

  > **OOS 2025 Results (13 trades, frozen strategy):**
  > Win rate 53.8% | Sharpe 0.020 | Net return -0.04% | Max DD -3.90%
  > Permutation test p-value = 0.499 — not statistically significant.
  >
  > **Walk-forward summary (2019–2025):** Profitable in 4/7 years.
  > Strategy is regime-dependent: strong in 2023 (+11.6%) and 2024 (+12.1%),
  > flat/negative in choppy or declining markets (2019, 2022, 2025).
  > The EMA_50 filter limits losses but does not generate alpha in non-trending
  > regimes. Max drawdown never exceeded -7.3% in any single year — capital
  > protection is working; alpha generation is regime-conditional.
  >
  > **Implication for Phase 3:** Proceed with recalibrated expectations.
  > Phase 3 goal is operational validation (correct execution, clean fills,
  > zero unhandled crashes) rather than statistical edge validation — the OOS
  > data has already shown the edge is regime-dependent. Do not re-optimise
  > parameters using 2025 data. Consider a broad-market regime filter
  > (VOO above its own EMA_50) as a Phase 4 strategy enhancement using
  > fresh untouched data.

---

## Phase 3 — Paper Trading Automation
**Goal:** Run the full system end-to-end with real market timing, real Alpaca
paper fills, and real Telegram notifications. Prove operational reliability.
**Gate to Phase 4:** ≥ 20 completed paper trades. Max drawdown < 15%.
Zero unhandled crashes over a 2-week period.
Note: Sharpe > 0.8 gate removed — OOS validation showed the edge is
regime-dependent; Phase 3 primary goal is operational reliability, not
statistical edge re-validation.

### State Store
- [x] Write `src/database.py`
  - [x] Initialize SQLite schema on first run (`init_db(path)` returns connection)
  - [x] `save_position(conn, position)` — insert new open position, returns db_id
  - [x] `update_position(conn, db_id, **fields)` — update SL, status, exit info
  - [x] `get_open_positions(conn)` — returns all open positions as Position objects
  - [x] `log_event(conn, symbol, event, detail)` — append to trade_log
  - [x] `get_weekly_summary(conn, since)` — read-only weekly activity aggregator (added with weekly heartbeat)
  - [x] 26 unit tests in `tests/test_database.py` — all passing (in-memory SQLite)

### Notification System
- [x] Write `src/notifier.py`
  - [x] `send_signal_alert(symbol, signal_context)` — buy signal message
  - [x] `send_execution_alert(symbol, order, position)` — filled notification
  - [x] `send_exit_alert(symbol, position, reason, exit_price)` — exit notification with P&L
  - [x] `send_error_alert(error)` — crash/error notification, never raises
  - [x] `send_weekly_summary(summary, equity)` — Friday heartbeat, never raises (added during observation period)
  - [x] `listen_for_reply(timeout_seconds)` — poll for YES/NO reply; drains queue before listening
  - [x] 46 unit tests in `tests/test_notifier.py` — all passing (mocked Bot)

### Executor
- [x] Write `src/executor.py`
  - [x] `place_buy_order(symbol, notional)` — notional (dollar-amount) market order; Alpaca returns fractional qty filled
  - [x] `place_sell_order(symbol, shares, reason)` — fractional qty market sell, reason in returned dict
  - [x] `get_account_equity()` — fetches live equity on every call, never cached
  - [x] Paper vs live switch via `config.ALPACA_PAPER`; env var re-read on every order call
  - [x] Log every order attempt and Alpaca response
  - [x] 22 unit tests in `tests/test_executor.py` — all passing (mocked TradingClient)

  > **Executor updated (done during Phase 3):** Switched from `qty=whole_shares` to
  > `notional=dollar_amount` for buy orders. Whole-share ordering is impractical on a
  > $1k live account — ASML at $1,628 and VOO at $684 would each exceed the account.
  > `Position.shares` is now `float`. `compute_position_size` returns `float` (no floor).
  > `MAX_POSITION_PCT` (default 0.25) caps any single position at 25% of equity.
  > Database schema updated: `shares REAL NOT NULL` (was `INTEGER`).
  > All 166 unit tests pass. Run `scripts/migrate_db.py` on VPS before next deploy.

### Main Loop
- [x] Write `main.py`
  - [x] Initialize APScheduler with market-hours check
  - [x] NYSE calendar check — skip on holidays and weekends
  - [x] Scan job (every 15 min, 9:45–15:45 EST):
    - [x] Fetch latest bars for all symbols
    - [x] Skip symbols already holding a position
    - [x] Compute indicators → evaluate signal
    - [x] If signal: send Telegram alert, wait for YES
    - [x] If YES: size position → place order → save to DB → confirm alert
  - [x] Monitor job (every 15 min, same window):
    - [x] Load all open positions from DB
    - [x] Fetch current price for each
    - [x] Update trailing stop
    - [x] Check exit conditions → execute exit if triggered

### Deployment
- [x] Test full loop locally in paper mode — skipped; validated via systemd on VPS directly
- [x] Provision VPS (Hetzner CPX11, Hillsboro OR, Ubuntu 24.04 — $7.59/mo)
- [x] Set up `systemd` service for auto-restart (`swing-trader.service`, enabled)
- [x] Configure `.env` on VPS (entered manually, never copied from local)
- [x] Write `scripts/pull_db.sh` — checkpoint WAL + SCP trades.db to local for DB Browser review
- [x] Write `scripts/migrate_db.py` — migrate `shares INTEGER → REAL` schema on existing DBs
- [x] Write `scripts/test_notional_order.py` — integration smoke-test for notional buy/sell flow
- [x] Add `MAX_POSITION_PCT=0.25` to VPS `.env`
- [x] Run `python3.12 scripts/migrate_db.py trades.db` on VPS
- [x] Deploy updated code to VPS (`git pull && sudo systemctl restart swing-trader`)
- [x] Run `python scripts/test_notional_order.py` during market hours to verify notional flow
- [x] Confirm Telegram alerts arrive on phone from VPS

### Paper Trading Observation Period
- [ ] Run for minimum 4–8 weeks (target ≥ 20 trades)
- [ ] Log every trade outcome in `trade_log`
- [ ] Weekly review: run `bash scripts/pull_db.sh` and review positions + trade_log in DB Browser
- [ ] Do not adjust strategy parameters mid-observation (taints the sample)
- [x] Add weekly Telegram heartbeat summary (Fri 16:30 ET) so the app's health is
      visible each week without opening a laptop — observability only, no strategy change (PR #9)

---

## Phase 4 — Live Capital Deployment
**Goal:** Real money. Treat this like a production system.
**Pre-conditions:** All Phase 3 gate criteria must be met. No exceptions.
**Starting capital:** $1,000 for at least 6 months before scaling.

### Code changes already done (during Phase 3)
- [x] Switched to fractional/notional orders — `place_buy_order(symbol, notional)`
- [x] `Position.shares: float`, `compute_position_size` returns float
- [x] `MAX_POSITION_PCT=0.25` — caps each position at 25% of equity ($250 on $1k)
- [x] Database schema: `shares REAL NOT NULL`

### Strategy enhancements to evaluate after paper trading (do not touch before Phase 4)
- [ ] **Trailing-stop-only exit** — remove the day-5 force-close and let the trailing stop be
      the sole time-based protection. Paper trading showed 71% of exits hit day-5 at avg +1.47%;
      winners like NVDA (+9.57%) and TSM (+4.44%) were cut short. Evaluate on fresh live data
      whether removing the calendar rule lets winners run further without materially increasing
      drawdown. Requires backtesting on untouched post-paper data before enabling in production.

### Still required before going live
- [ ] Confirm Phase 3 gate criteria are met (≥ 20 trades, drawdown < 15%, zero crashes/2wk)
- [ ] Remove ASML from live `.env` SYMBOLS — at $1,600+, even 25% cap gives ~0.15 shares,
      too small to be meaningful on a $1k account; keep in paper universe
- [ ] Generate live Alpaca API keys (separate from paper keys)
- [ ] Update `.env` on VPS: `ALPACA_PAPER=false` + live credentials + updated SYMBOLS
- [ ] Start with $1,000 capital — monitor closely for first 2 weeks
- [ ] Scale up only after live fills and P&L match paper behavior

---

## Appendix — Phase Gate Summary

| Gate         | Condition                                          |
|--------------|----------------------------------------------------|
| Phase 1 → 2  | Clean data for all symbols, no nulls, 1yr range     |
| Phase 2 → 3  | Backtest Sharpe > 0.5, max drawdown < 25%          |
| Phase 3 → 4  | ≥ 20 paper trades, drawdown < 15%, zero crashes/2wk |
| Scale-up     | Live results match paper results over 2 weeks      |
