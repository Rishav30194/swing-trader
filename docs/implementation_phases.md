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
- [ ] Update `src/signals.py` to three-condition AND gate (see `project_overview.md`)
  - [ ] `evaluate_buy_signal(df)` — returns `SignalResult` dataclass
  - [ ] Condition 1: close > EMA_50 (trend filter — uptrend only)
  - [ ] Condition 2: RSI_LOWER_BOUND ≤ RSI(14) < RSI_UPPER_BOUND (mild pullback)
  - [ ] Condition 3: MACD bullish crossover on this bar
  - [ ] Populate `SignalResult.context` with all indicator values for alerts
  - [ ] Update unit tests to match new three-condition gate

  > **Strategy revised:** Original four-condition gate (RSI<45, price within 2% EMA_21,
  > MACD crossover, volume ≥ 1.5×) produced zero signals over 2022–2024. Two conditions
  > were structurally incompatible: oversold price (RSI<45) always sits >2% below EMA_21,
  > and oversold bounces happen on below-average volume. The new three-condition gate
  > (EMA_50 trend filter + RSI 40–55 + MACD crossover) produced 17 signals with 76%
  > win rate and profit factor 5.93 across the same period. See `project_overview.md`
  > for full rationale.

### Risk Module
- [ ] Update `src/risk.py` exit parameters to match revised strategy
  - [x] `compute_exit_levels(entry_price, atr)` — returns SL, TP
  - [x] `compute_position_size(equity, risk_pct, entry, stop)` — returns shares
  - [ ] `update_trailing_stop` — update activation from 1× ATR to 0.5× ATR
  - [x] `check_exit_conditions(position, current_bar, entry_date)` — returns exit reason or None
  - [x] Unit tests for all edge cases (34 tests, all passing)
  - [ ] Update ATR multipliers: SL = 1.5× (was 2×), TP = 2× (was 3×)

### Backtesting
- [x] Write `backtest.py` — pandas walk-forward simulator using production modules
  - [x] Feed 2022–2024 daily OHLCV for all 4 symbols
  - [x] Apply strategy: signal → entry → SL/TP/day5 exit
  - [x] Output report: total trades, win rate, avg hold days, total return,
        Sharpe ratio, max drawdown, best/worst trade, Phase 2 gate verdict
  - Note: chose a direct pandas simulation over `backtrader` — duplicating
    strategy logic into a bt.Strategy class creates divergence risk between
    backtest and live code; this approach tests the exact production functions.
- [ ] Run full backtest (2022–2024) with revised strategy — validate Phase 2 gate
- [ ] Run bear-market run (2022 only) — trend filter should block most/all signals
- [ ] Run bull-market run (2023–2024) — expect majority of signals here
- [ ] **Do not use 2025 data for parameter tuning** — reserve it as out-of-sample

---

## Phase 3 — Paper Trading Automation
**Goal:** Run the full system end-to-end with real market timing, real Alpaca
paper fills, and real Telegram notifications. Prove operational reliability.
**Gate to Phase 4:** ≥ 30 completed paper trades. Sharpe > 0.8.
Max drawdown < 15%. Zero unhandled crashes over a 2-week period.

### State Store
- [ ] Write `src/database.py`
  - [ ] Initialize SQLite schema on first run
  - [ ] `save_position(position)` — insert new open position
  - [ ] `update_position(id, fields)` — update SL, status, exit info
  - [ ] `get_open_positions()` — returns all open positions
  - [ ] `log_event(symbol, event, detail)` — append to trade_log

### Notification System
- [ ] Write `src/notifier.py`
  - [ ] `send_signal_alert(symbol, signal_context)` — buy signal message
  - [ ] `send_execution_alert(symbol, order, position)` — filled notification
  - [ ] `send_exit_alert(symbol, position, reason)` — exit notification
  - [ ] `send_error_alert(error)` — crash/error notification
  - [ ] `listen_for_reply(timeout_seconds)` — poll for YES/NO reply

### Executor
- [ ] Write `src/executor.py`
  - [ ] `place_buy_order(symbol, shares)` — market order
  - [ ] `place_sell_order(symbol, shares, reason)` — market order
  - [ ] `get_account_equity()` — for position sizing
  - [ ] Paper vs live switch via `config.ALPACA_PAPER`
  - [ ] Log every order attempt and Alpaca response

### Main Loop
- [ ] Write `main.py`
  - [ ] Initialize APScheduler with market-hours check
  - [ ] NYSE calendar check — skip on holidays and weekends
  - [ ] Scan job (every 15 min, 9:45–15:45 EST):
    - [ ] Fetch latest bars for all symbols
    - [ ] Skip symbols already holding a position
    - [ ] Compute indicators → evaluate signal
    - [ ] If signal: send Telegram alert, wait for YES
    - [ ] If YES: size position → place order → save to DB → confirm alert
  - [ ] Monitor job (every 15 min, same window):
    - [ ] Load all open positions from DB
    - [ ] Fetch current price for each
    - [ ] Update trailing stop
    - [ ] Check exit conditions → execute exit if triggered

### Deployment
- [ ] Test full loop locally in paper mode for 1 week
- [ ] Provision VPS (Hetzner CX11 or DigitalOcean)
- [ ] Set up `systemd` service for auto-restart
- [ ] Configure `.env` on VPS (never copy from local, re-enter manually)
- [ ] Confirm Telegram alerts arrive on phone from VPS

### Paper Trading Observation Period
- [ ] Run for minimum 4–8 weeks (target ≥ 30 trades)
- [ ] Log every trade outcome in `trade_log`
- [ ] Weekly review: are exits happening correctly? Any missed signals?
- [ ] Do not adjust strategy parameters mid-observation (taints the sample)

---

## Phase 4 — Live Capital Deployment
**Goal:** Real money. Treat this like a production system.
**Pre-conditions:** All Phase 3 gate criteria must be met. No exceptions.

- [ ] Confirm Phase 3 gate criteria are met (Sharpe, drawdown, trade count)
- [ ] Generate live Alpaca API keys (separate from paper keys)
- [ ] Update `.env` on VPS: `ALPACA_PAPER=false` + live credentials
- [ ] Start with 10–20% of intended capital allocation
- [ ] Monitor closely for first 2 weeks — compare to paper behavior
- [ ] Scale to full allocation only after live results match paper results

---

## Appendix — Phase Gate Summary

| Gate         | Condition                                          |
|--------------|----------------------------------------------------|
| Phase 1 → 2  | Clean data for all 4 symbols, no nulls, 1yr range  |
| Phase 2 → 3  | Backtest Sharpe > 0.5, max drawdown < 25%          |
| Phase 3 → 4  | ≥ 30 paper trades, Sharpe > 0.8, drawdown < 15%   |
| Scale-up     | Live results match paper results over 2 weeks      |
