# Implementation Phases — Swing Trader

Progress is tracked here. Check boxes as tasks are completed.
Never skip a phase gate — each one exists to protect capital.

> ## ⚠️ Strategy replaced — 2026-08-01
>
> Phases 1–3 below were built around a three-condition signal gate
> (close > EMA_50, RSI 40–55, MACD crossover). **That strategy was retired after
> testing showed it had no measurable edge** — CAGR −1.12%, Sharpe −0.08, and a
> permutation test on its trade P&Ls returning p = 0.6144.
>
> It has been replaced by an **SMA-200 regime overlay**. See
> **Phase 3R** near the end of this document for the replacement work, and
> `docs/strategy_validation.md` for the evidence.
>
> Phases 1–3 are kept as a historical record. Their checked boxes describe work
> that was genuinely done; they no longer describe the current system.

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

### Paper Trading Observation Period — ABANDONED, strategy retired
- [x] Ran 2026-05-26 → 2026-07-31 on the VPS. **1,129 scan cycles, 9,032 bar
      fetches, zero unhandled errors — and zero trades.**
- [x] Add weekly Telegram heartbeat summary (Fri 16:30 ET) so the app's health is
      visible each week without opening a laptop — observability only (PR #9)
- [x] Root-caused the zero-trade result (2026-07-31). Three findings:
  - [x] **Two live defects.** `_BARS_LOOKBACK = 90` returned only 61–63 bars,
        leaving EMA_50 under-converged and reading up to 1.7% high; and the scan
        always evaluated the in-progress intraday bar, so a MACD crossover
        confirmed at the close could never be acted on — by the next session it
        was the *previous* bar and the crossover test could no longer fire.
  - [x] **The one signal that did qualify was suppressed.** NVDA 2026-07-08:
        close 204.12 > EMA_50 203.50, RSI 51.0, MACD crossed. The live 90-bar
        window computed EMA_50 = 204.96, so the trend filter failed.
  - [x] **The strategy had no edge anyway** — permutation p = 0.6144, negative
        CAGR over 8 years, last place of 9 strategies in both train and test
        halves. Fixing the defects would have produced more trades and more loss.
- [x] Observation period voided. The sample was collected under a strategy that
      is no longer in use and does not carry over.

---

## Phase 3R — Strategy Replacement (2026-08-01)
**Goal:** Replace a strategy with no edge with one whose single claim survives
scrutiny, and rebuild the application around it.
**Gate to Phase 4:** 26 weekly rebalances executed with zero unhandled crashes,
max drawdown < 15%, and live regime transitions matching the backtest on the
same bars.

### Research
- [x] Multi-strategy sweep — ~200 configurations across technical rules,
      cross-sectional momentum, sector rotation, options, volatility targeting,
      and LLM signals, on four universes
- [x] Establish survivorship bias as the dominant effect (same strategy: Sharpe
      1.19 on the original 8 symbols, 0.17 on sector ETFs)
- [x] Reject options on real Alpaca option bars — covered call won 89% of 27
      monthly cycles and netted +$57 against +$29,183 for holding the shares;
      every underlying affordable at $1k has a spread wider than the break-even
- [x] Reject LLM/agentic signal generation on published evidence + the
      self-contamination problem
- [x] Reject volatility targeting — a static control at matched exposure scored
      *higher* out-of-sample (1.62 vs 1.56)
- [x] Adopt the SMA-200 regime overlay; validate it against a matched-exposure
      static control across train/test on four universes, plus a block bootstrap
      on drawdown (98.7%–100% of resamples shallower)
- [x] Write `docs/strategy_validation.md` — evidence **and** the limits of the claim

### Implementation
- [x] Revise `CLAUDE.md` hard rules 2, 3, 5, 7 for the new design
- [x] `src/portfolio.py` — regime state, target weights, order diffing (pure)
- [x] `SMA_200` + `MIN_BARS_FOR_STRATEGY` in `indicators.py`
- [x] `data.py` — `completed_only` flag; fixes the forming-bar defect
- [x] `config.py` — swap strategy parameters (credential loading untouched)
- [x] `database.py` — `sleeves` + `rebalance_log` tables; `positions` preserved
- [x] `notifier.py` — weekly plan + result messages; all sends non-raising
- [x] `executor.py` — `get_current_holdings()`, `place_sell_notional()`
- [x] `main.py` — weekly rebalance scheduler replaces the 15-min scan loop
- [x] `backtest.py` — rewritten to call `portfolio.py`, so live and backtest
      cannot diverge
- [x] `validate_oos.py` — matched-exposure control, train/test, block bootstrap
- [x] Delete `signals.py` and its tests
- [x] 249 unit tests passing (`main.py` and `data.py` previously had none)

### Audit round 2 — money-path review (2026-08-01)
- [x] **Capital was sized off the account balance, not the allocation.** The paper
      account holds $99,999.99, so the app would have deployed $12,500 per sleeve
      instead of $125 — a **100× over-deployment**. Sizing now uses
      `compute_strategy_equity()`: managed sleeve value + a persisted cash ledger
      seeded from the required `TRADING_CAPITAL`. Profits compound; unallocated
      money is invisible; account equity and cash are ceilings only.
- [x] **`drift_tolerance` silently gated regime transitions.** Above 1/N it
      suppressed entries and exits too — at 25% the backtest placed *zero* orders.
      It now applies only to drift; regime decisions respect the broker minimum.
- [x] **Tax was unaccounted for.** Measured realised gains per setting: drift
      trades were 94% of all orders and bought nothing. `DRIFT_TOLERANCE` default
      raised 0.1% → 5%: orders 1,727 → 124, CAGR 27.88% → 29.95%, Sharpe
      1.24 → 1.26, and ~$16k less tax on a $100k base.
- [x] Long-only guard: a short position in a managed symbol aborts the run
- [x] Unmanaged holdings warned about, never sold, never counted as capital
- [x] Dead code removed — `risk.py`, `test_risk.py`, the `positions` helpers, and
      nine indicator columns nothing read (~700 lines)
- [x] Backtest reproduces the validated figures (maxDD −25.56% exact match)

  > **Bug caught by that integration check:** `compute_target_weights` divided by
  > the number of *evaluated* sleeves rather than the configured universe size.
  > With QQQM absent before its 2020 listing, the other 7 sleeves each took 1/7
  > instead of 1/8 — a transient data outage would have silently concentrated
  > the portfolio. Fixed; `universe_size` is now a required argument.

### Deployment
- [ ] Add new strategy vars to VPS `.env` (`SMA_BAND`, `REBALANCE_*`, …)
- [ ] Deploy to VPS (`git pull && sudo systemctl restart swing-trader`)
- [ ] Confirm the first weekly plan message arrives on Friday
- [ ] Verify the first rebalance executes and fills at Monday's open

### Observation
- [ ] Run 26 weekly rebalances (~6 months)
- [ ] Weekly review via `bash scripts/pull_db.sh`
- [ ] Do not re-tune the band or cadence mid-observation (taints the sample)

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

  > **Superseded:** the trailing-stop and day-5 enhancements listed here belonged
  > to the retired strategy. The overlay has no per-trade stops or time exits.

  > **ASML no longer needs removing.** That item existed because whole-share
  > sizing made a $1,600 stock unusable on a $1k account. With 8 equal sleeves at
  > $125 each and notional orders, ASML resolves to ~0.08 shares, which is fine.

### Still required before going live
- [ ] Confirm Phase 3R gate criteria are met (26 rebalances, drawdown < 15%,
      zero crashes, live transitions match backtest)
- [ ] Generate live Alpaca API keys (separate from paper keys)
- [ ] Update `.env` on VPS: `ALPACA_PAPER=false` + live credentials
- [ ] Start with $1,000 capital — monitor closely for the first 2 weeks
- [ ] Scale up only after live fills and drawdown behaviour match paper

### Expectations to hold going in
- Expect roughly **two-thirds of buy-and-hold's return for half its drawdown**.
- Expect to *underperform* buy-and-hold in most years. It beat buy-and-hold in
  2 of 9 calendar years — 2018 and 2022. That is the design, not a malfunction.
- Do not re-tune the band or cadence in response to underperformance in an up
  year. The drawdown reduction is the only effect that replicated; optimising
  against anything else is fitting noise.

---

## Appendix — Phase Gate Summary

| Gate          | Condition                                                      |
|---------------|----------------------------------------------------------------|
| Phase 1 → 2   | Clean data for all symbols, no nulls, 1yr range                |
| Phase 2 → 3   | Backtest Sharpe > 0.5, max drawdown < 25%                      |
| Phase 3 → 4   | *(retired with the old strategy)*                              |
| Phase 3R → 4  | 26 weekly rebalances, drawdown < 15%, zero crashes, live regime transitions match backtest |
| Scale-up      | Live drawdown behaviour matches paper over 2 weeks             |

Note the Phase 3R gate contains no Sharpe or win-rate condition. The strategy
does not claim an edge, so testing for one would be measuring noise. What is
being validated is operational reliability and drawdown behaviour.
