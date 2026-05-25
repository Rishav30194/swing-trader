# Swing Trader

A personal, automated swing-trading application that executes a disciplined **3-to-5-day hold strategy** on a focused list of high-quality assets. The system scans for high-probability setups using combined technical indicators and manages risk strictly. A human approval gate sits in front of every trade entry.

> **Current Status: Phase 2 complete — strategy validated. Phase 3 (paper trading automation) next.**

---

## Strategy Overview

### Asset Universe

| Symbol | Type         | Rationale                                          |
|--------|--------------|-----------------------------------------------------|
| NVDA   | Single stock | High-volatility, high-liquidity swing candidate    |
| ASML   | Single stock | Semiconductor equipment, lower NVDA correlation    |
| VOO    | ETF          | S&P 500 tracker, trend-following use case          |
| QQQM   | ETF          | Nasdaq-100, tech-heavy moderate volatility         |
| MSFT   | Single stock | Large-cap tech, strong trend structure             |
| AAPL   | Single stock | Highest US market liquidity                        |
| AMD    | Single stock | High-beta semiconductor, similar profile to NVDA   |
| TSM    | Single stock | Semiconductor equipment, non-US, lower correlation |

### Buy Signal — Three-Condition AND Gate

All three conditions must be true simultaneously on the same daily bar:

1. **Trend Filter** — Close > EMA(50). Asset is in a structural uptrend. Never buy falling knives.
2. **RSI Pullback** — 40 ≤ RSI(14) < 55. A mild, controlled dip within the uptrend. Below 40 suggests a deeper problem; at or above 55 means no real pullback has occurred.
3. **MACD Bullish Crossover** — MACD line crossed above signal line on this specific bar (was at or below on the prior bar). Momentum is turning up.

### Risk Management

- **Stop-loss**: Entry − (1.5 × ATR). Adapts to each asset's actual volatility.
- **Take-profit**: Entry + (2 × ATR). R:R ≥ 1.33:1, achievable within the 5-day hold window.
- **Trailing stop**: Ratchets up as price rises, activates after 0.5× ATR move in favor. Locks in profit early.
- **Force-close**: Day 5 EOD, regardless of P&L. Discipline over hope.

### Exit Priority

1. Hard stop hit → immediate market sell (no human gate)
2. Trailing stop hit → immediate market sell (no human gate)
3. Take-profit hit → limit sell at TP price
4. Day 5 EOD → market close

### Backtest Results (2022–2024)

| Period | Sharpe | Max DD | Trades | Win Rate | Verdict |
|--------|--------|--------|--------|----------|---------|
| 2022–2024 (full) | 1.043 | -1.89% | 15 | 73.3% | Phase 2 gate PASS |
| 2022 only (bear) | 0.228 | -1.48% | 2 | 50.0% | Capital protected |
| 2023–2024 (bull) | 1.291 | -1.89% | 13 | 76.9% | PASS |

The 2022 bear market result is intentional: the EMA_50 trend filter blocked almost all signals, leaving equity nearly flat (+0.55%) while the broader market fell ~20%.

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│              Data Ingestion Layer                   │
│   Alpaca API — historical bars + real-time quotes   │
└────────────────────┬────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────┐
│              Strategy Layer                         │
│  indicators.py → signals.py → risk.py               │
│  RSI · EMA · MACD · Vol SMA · ATR                   │
└────────────────────┬────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────┐
│         Human-in-the-Loop Gate (Telegram)           │
│  Alert sent → waits for YES/NO reply                │
│  Stops bypass this gate and execute immediately     │
└────────────────────┬────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────┐
│              Execution Layer                        │
│  executor.py — Alpaca order placement               │
│  Paper env (Phase 1–3) ↔ Live env (Phase 4)         │
└────────────────────┬────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────┐
│              State & Scheduler Layer                │
│  SQLite — positions, trade log                      │
│  APScheduler — scans every 15 min, 9:45–15:45 EST   │
└─────────────────────────────────────────────────────┘
```

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
│   ├── data.py         # Alpaca data fetching (historical + latest bar)
│   ├── indicators.py   # RSI, EMA, MACD, Vol SMA, ATR (pure functions)
│   ├── signals.py      # Three-condition AND gate evaluation
│   ├── risk.py         # ATR-based SL/TP, position sizing, trailing stop
│   ├── notifier.py     # Telegram alerts + YES/NO reply handler
│   └── executor.py     # Alpaca order placement (paper + live)
├── tests/
│   ├── test_indicators.py
│   ├── test_signals.py
│   └── test_risk.py
├── docs/               # Architecture and implementation phase docs
├── scripts/
│   └── validate_data.py
├── main.py             # Entry point — scheduler + orchestration
├── backtest.py         # Phase 2 standalone backtest runner
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

# Strategy parameters (tune without touching code)
SYMBOLS=NVDA,ASML,VOO,QQQM,MSFT,AAPL,AMD,TSM
RSI_LOWER_BOUND=40
RSI_UPPER_BOUND=55
ATR_STOP_MULTIPLIER=1.5
ATR_TP_MULTIPLIER=2.0
ATR_TRAILING_ACTIVATION=0.5
RISK_PER_TRADE_PCT=0.02
MAX_OPEN_POSITIONS=2
```

Get your Alpaca paper trading keys at [alpaca.markets](https://alpaca.markets). Create a Telegram bot via [@BotFather](https://t.me/BotFather).

### 3. Validate the data pipeline

```bash
python scripts/validate_data.py
```

All four symbols should return 1 year of clean OHLCV bars with no nulls.

### 4. Run the backtester

```bash
python backtest.py                                          # 2022–2024 full
python backtest.py --start 2022-01-01 --end 2022-12-31     # bear market
python backtest.py --start 2023-01-01 --end 2024-12-31     # bull market
```

### 5. Run the live paper trading loop

```bash
python main.py
```

The scheduler will start scanning every 15 minutes during market hours (9:45–15:45 EST). Trade entry alerts are sent to Telegram for human approval.

---

## Implementation Phases

| Phase | Description                        | Status      |
|-------|------------------------------------|-------------|
| 1     | Environment, data pipeline, config | Complete    |
| 2     | Indicators, signals, backtesting   | Complete    |
| 3     | Paper trading automation           | Not started |
| 4     | Live capital deployment            | Not started |

**Phase gate before live trading:** ≥ 20 paper trades, Sharpe > 0.8, max drawdown < 15%.

---

## Risk Guardrails

These are hard constraints that cannot be bypassed:

- Live orders are blocked when `ALPACA_PAPER=true`
- No order is placed without a stop-loss computed first
- Stop-loss and trailing stop exits execute immediately without human approval
- Position size is calculated from live account equity on every trade
- Maximum 2 open positions at any time
- Day 5 force-close executes even if Telegram is unreachable

---

## Paper → Live Switch

The only change needed to go from paper to live trading:

1. Set `ALPACA_PAPER=false` in `.env`
2. Replace API keys with live Alpaca credentials
3. Restart the service

Zero code changes by design.

---

## License

Personal use only. Not financial advice.
