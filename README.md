# Swing Trader

A personal, automated portfolio application that holds a fixed basket of assets and manages **exposure** rather than trying to pick winners. Each sleeve is invested only while it trades above its own 200-day average; otherwise its capital sits in cash. One Telegram message a week carries the rebalance plan — increases need approval, reductions happen automatically.

> **Current status: Phase 3R — regime overlay implemented, paper trading restarting.** The original signal strategy was retired on 2026-08-01 after testing showed it had no measurable edge.

**This system does not claim to beat the market.** It trades roughly a third of buy-and-hold's return for half its drawdown. See [Honest performance expectations](#honest-performance-expectations).

---

## Why the strategy was replaced

The original three-condition signal gate (close > EMA_50, RSI 40–55, MACD crossover) ran on a VPS for nine weeks and produced **zero trades**. Investigation found three things:

1. **Two live defects.** A 90-day lookback returned only 61–63 bars, leaving EMA_50 under-converged and reading up to 1.7% too high; and the 15-minute scan always evaluated the *in-progress* daily bar, so a crossover confirmed at the close could never be acted on.
2. **One real signal was suppressed by them.** NVDA on 2026-07-08 qualified on completed bars — the live window's inflated EMA_50 rejected it.
3. **The strategy had no edge regardless.** Permutation test on its trade P&Ls: **p = 0.6144**. Negative CAGR over 8 years. Last place of 9 strategies in *both* train and test halves. Fixing the defects would have produced more trades and larger losses.

A search across ~200 configurations — technical rules, cross-sectional momentum, sector rotation, options on real Alpaca option bars, volatility targeting, LLM signals — found nothing that reliably improved risk-adjusted return once survivorship bias was removed. Exposure management reliably reduced drawdown, so that is what the system now does.

Full evidence, including what *failed* and why: [`docs/strategy_validation.md`](docs/strategy_validation.md).

---

## Strategy Overview

### Asset Universe

Eight sleeves, equal weight at 1/8 of **allocated capital** each — see [Capital allocation](#capital-allocation).

| Symbol | Type         | Rationale                                          |
|--------|--------------|-----------------------------------------------------|
| NVDA   | Single stock | High-volatility, high-liquidity                    |
| ASML   | Single stock | Semiconductor equipment, lower NVDA correlation    |
| VOO    | ETF          | S&P 500 tracker                                    |
| QQQM   | ETF          | Nasdaq-100, tech-heavy moderate volatility         |
| MSFT   | Single stock | Large-cap tech, strong trend structure             |
| AAPL   | Single stock | Highest US market liquidity                        |
| AMD    | Single stock | High-beta semiconductor                            |
| TSM    | Single stock | Semiconductor, non-US, lower correlation           |

> These symbols were chosen partly *because* they had already performed well. The same strategies scored Sharpe 1.19 here and 0.17 on a universe not selected for past performance. Backtest figures on this universe are not forward expectations.

### The rule — per sleeve, hysteresis band around SMA-200

Evaluated weekly on the most recent **completed** daily bar:

| current state | condition                    | new state |
|---------------|------------------------------|-----------|
| held          | close < 0.98 × SMA_200       | exit      |
| flat          | close > 1.02 × SMA_200       | enter     |
| either        | anything between those lines | unchanged |

The band is hysteresis, not a threshold — the same price gives a different answer depending on whether the sleeve is currently held. That is what keeps turnover near 5×/yr instead of ~20×/yr when price oscillates around the average.

A sleeve that is off, or that could not be priced, leaves its capital **in cash**. Weight is never redistributed to the remaining sleeves.

### Risk management

The regime band *is* the risk control. No ATR stops, no take-profits, no trailing stops, no day-5 force close — those belonged to the retired strategy. The remaining guardrail is `MAX_POSITION_PCT`, which caps any single sleeve.

### Rebalance cadence

Weekly, after Friday's close. Orders queue to Monday's open, reproducing the one-bar execution lag the strategy was validated under. Weekly scored Sharpe 1.28 against daily's 1.33 at a third of the turnover — which is why there is no intraday scanner.

---

## Honest performance expectations

Validated 2018-11 → 2026-07 on the eight-symbol universe:

|                | overlay | buy & hold |
|----------------|---------|------------|
| CAGR           | 30.0%   | 39.1%      |
| Sharpe         | 1.26    | 1.15       |
| Max drawdown   | **−27.7%** | −50.0%  |
| MAR            | 1.08    | 0.78       |

**What survived validation:** the drawdown reduction. In a block bootstrap the overlay produced the shallower drawdown in 98.7%–100% of resamples across four different universes, and it beat a *matched-exposure static control* in every window tested.

**What did not:** any risk-adjusted return advantage. In the out-of-sample half the overlay scored Sharpe 1.56 against buy-and-hold's 1.57 and a constant-50%-exposure control's 1.62 — a dead heat. It beat buy-and-hold in **2 of 9 calendar years**, both of them down years.

The backtest window contains no 2000- or 2008-style bear market, so treat the absolute figures as optimistic. If the objective were maximum wealth rather than a tolerable drawdown, buy-and-hold with no application at all would be the honest recommendation.

---

## Capital allocation

**Sizing uses the capital you allocate, not your account balance.** Set `TRADING_CAPITAL` and the strategy trades that:

    strategy capital = market value of managed sleeves + the strategy's own cash ledger

A paper account funded with $100,000 still trades the $1,000 you allocated. Without this the app would deploy the whole balance — a 100× over-deployment. Profits compound: once the sleeves are worth $1,100 it sizes off $1,100. Money deposited into the account but never allocated stays invisible to it. Account equity and cash act as ceilings only.

`TRADING_CAPITAL` is **required** — the app refuses to start rather than guess how much money to deploy.

The account is assumed dedicated to this strategy. Positions held outside `SYMBOLS` are never sold and never counted as strategy capital, but each run warns about them. A short position in a managed symbol aborts the run: this strategy is long-only.

---

## Costs, fees and tax

**Trading costs are modelled. Tax is not.** The 5 bps/side charged in the backtest covers Alpaca's $0 commission, the SEC fee (~0.00278% on sells), the FINRA TAF, and the bid-ask spread on these liquid names.

**Every rebalance sell is a taxable event in a taxable account.** Buy-and-hold defers tax indefinitely — a structural advantage the pre-tax table above does not show. Over the 2018–2026 backtest on a $100,000 base:

| DRIFT_TOLERANCE | orders | realised gains | short-term | est. tax¹ |
|---|---|---|---|---|
| 0.1% | 1,727 | $435,740 | $197,746 | $98,978 |
| **5% (default)** | **124** | $426,109 | $110,294 | **$82,666** |
| regime-only | 97 | $269,195 | $9,813 | $42,047 |

¹ at 32% short-term / 15% long-term; your rates differ, state tax is extra.

This is why `DRIFT_TOLERANCE` defaults to 5%: drift trades were 94% of all orders and bought nothing. At 5% the backtest returns a *higher* CAGR and Sharpe with 93% fewer trades.

**Tax makes this strategy's position versus buy-and-hold worse, not better.** In a tax-advantaged account (IRA/Roth/401k) rebalancing is tax-free and this section is moot — which materially changes where the strategy belongs.

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│              Data Ingestion Layer                   │
│   Alpaca API — completed daily bars only            │
└────────────────────┬────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────┐
│              Strategy Layer                         │
│  indicators.py → portfolio.py                       │
│  SMA_200 → regime state → target weights → orders   │
│  Pure functions; shared by live AND backtest        │
└────────────────────┬────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────┐
│         Human-in-the-Loop Gate (Telegram)           │
│  One weekly plan message → waits for YES/NO         │
│  INCREASES need YES · REDUCTIONS bypass entirely    │
└────────────────────┬────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────┐
│              Execution Layer                        │
│  executor.py — Alpaca notional market orders        │
│  Holdings re-derived from Alpaca every run          │
└────────────────────┬────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────┐
│              State & Scheduler Layer                │
│  SQLite — sleeves, strategy_state, rebalance_log     │
│  APScheduler — rebalance Fri 16:15 ET               │
│              + heartbeat Sat 09:00 ET               │
└─────────────────────────────────────────────────────┘
```

`backtest.py` calls the same `portfolio.py` functions `main.py` calls. The retired strategy failed partly because the live path and the backtest disagreed about which bar to evaluate; sharing the strategy module removes that class of bug.

### Tech Stack

| Component     | Choice                    |
|---------------|---------------------------|
| Language      | Python 3.12+              |
| Broker / Data | Alpaca Markets API        |
| Indicators    | `pandas-ta`               |
| Backtesting   | Direct pandas simulation  |
| Scheduler     | `APScheduler`             |
| Notifications | `python-telegram-bot`     |
| State store   | SQLite                    |
| Deployment    | Ubuntu VPS + systemd      |

---

## Project Structure

```
swing-trader/
├── src/
│   ├── config.py       # Loads .env, exposes typed Settings dataclass
│   ├── data.py         # Alpaca data fetching; drops the forming bar
│   ├── indicators.py   # SMA_200 (the only column any code reads)
│   ├── portfolio.py    # THE STRATEGY — regime, weights, order diffing
│   ├── database.py     # SQLite — sleeves, strategy_state, rebalance_log
│   ├── notifier.py     # Telegram plan/result alerts + reply handler
│   └── executor.py     # Alpaca orders + holdings fetch
├── tests/              # 249 unit tests
├── docs/               # Architecture, phases, and strategy validation
├── scripts/            # Data validation, DB pull/migrate, order smoke-test
├── main.py             # Entry point — weekly rebalance scheduler
├── backtest.py         # Regime overlay backtester
├── validate_oos.py     # Matched-exposure control, train/test, bootstrap
└── requirements.txt
```

---

## Setup

### 1. Clone and install dependencies

```bash
git clone https://github.com/Rishav30194/swing-trader.git
cd swing-trader
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment variables

Create a `.env` file in the project root (never commit this):

```env
# Alpaca — Paper Trading
ALPACA_API_KEY=your_paper_key_here
ALPACA_API_SECRET=your_paper_secret_here
ALPACA_PAPER=true

# Telegram
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here

# Universe
SYMBOLS=NVDA,ASML,VOO,QQQM,MSFT,AAPL,AMD,TSM

# Capital — REQUIRED. Sizing uses THIS, not your account balance.
TRADING_CAPITAL=1000       # a $100k paper account still trades $1k

# Strategy
SMA_BAND=0.02              # exit <0.98×SMA200, enter >1.02×
BARS_LOOKBACK_DAYS=365     # must comfortably exceed 200 trading days

# Rebalancing
DRIFT_TOLERANCE=0.05       # skip drift trades below 5% of capital; a TAX dial
MIN_ORDER_NOTIONAL=1.0
REBALANCE_DAY=fri
REBALANCE_HOUR=16
REBALANCE_MINUTE=15
REPLY_TIMEOUT_SECS=14400   # 4h; orders queue to Monday's open anyway
MAX_POSITION_PCT=0.25
```

Get your Alpaca paper trading keys at [alpaca.markets](https://alpaca.markets). Create a Telegram bot via [@BotFather](https://t.me/BotFather).

### 3. Validate the data pipeline

```bash
python scripts/validate_data.py
```

### 4. Run the backtester

```bash
python backtest.py --benchmark                       # full history vs buy & hold
python backtest.py --start 2022-01-01 --end 2022-12-31 --benchmark   # bear market
python backtest.py --band 0.0 --rebalance 1          # daily, no hysteresis
```

### 5. Pressure-test the strategy

```bash
python validate_oos.py
```

Runs the matched-exposure control, the train/test split, and the block bootstrap on drawdown — the three tests that decided this strategy and rejected the alternatives.

### 6. Run the live paper trading loop

```bash
python main.py
```

The scheduler rebalances every Friday at 16:15 ET and sends a heartbeat every Saturday at 09:00 ET.

---

## Implementation Phases

| Phase | Description                        | Status                          |
|-------|------------------------------------|---------------------------------|
| 1     | Environment, data pipeline, config | Complete                        |
| 2     | Indicators, signals, backtesting   | Complete (strategy since retired) |
| 3     | Paper trading automation           | Complete; observation voided    |
| 3R    | Strategy replacement — regime overlay | Code complete, deploying     |
| 4     | Live capital deployment            | Not started                     |

**Phase gate before live trading:** 26 weekly rebalances, max drawdown < 15%, zero unhandled crashes, live regime transitions matching the backtest on the same bars. Deliberately no Sharpe or win-rate gate — the strategy does not claim an edge, so testing for one would be measuring noise.

---

## Risk Guardrails

Hard constraints that cannot be bypassed:

- Live orders are blocked when `ALPACA_PAPER=true`
- No order is placed without validated target weights; a sleeve whose regime is off has a target weight of exactly 0
- **Exposure reductions execute immediately and unconditionally**, including when Telegram is unreachable. Only increases require the weekly YES
- Target notionals are derived from equity fetched live on every rebalance
- One sleeve per symbol, each capped at `MAX_POSITION_PCT`
- A failed order is logged, alerted, and never assumed to have succeeded — holdings are re-derived from Alpaca on the next run

---

## Paper → Live Switch

1. Set `ALPACA_PAPER=false` in `.env`
2. Replace API keys with live Alpaca credentials
3. Restart the service

Zero code changes by design. All orders are notional, so fractional shares are automatic — on a $1,000 account each sleeve targets $125, and ASML at ~$1,600/share resolves to ~0.08 shares.

---

## License

Personal use only. Not financial advice.
