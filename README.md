# Swing Trader

A personal, automated swing-trading application that executes a disciplined **3-to-5-day hold strategy** on a focused list of high-quality assets. The system scans for high-probability setups using combined technical indicators and manages risk strictly. A human approval gate sits in front of every trade entry.

> **Current Status: Phase 1 complete — data pipeline validated. Phase 2 (indicators + backtesting) in progress.**

---

## Strategy Overview

### Asset Universe

| Symbol | Type         | Rationale                                         |
|--------|--------------|---------------------------------------------------|
| NVDA   | Single stock | High-volatility, high-liquidity swing candidate   |
| ASML   | Single stock | Semiconductor equipment, lower NVDA correlation   |
| VOO    | ETF          | S&P 500 tracker, trend-following use case         |
| QQQM   | ETF          | Nasdaq-100, tech-heavy moderate volatility        |

### Buy Signal — All 4 conditions must be true simultaneously

1. **RSI Cooldown** — RSI(14) < 45. Asset has pulled back; not overbought.
2. **EMA Proximity** — Price within 2% of the 21-day EMA. Confirms trend alignment.
3. **MACD Bullish Crossover** — MACD line crossed above signal line on this specific bar.
4. **Volume Spike** — Current volume ≥ 1.5× the 20-day average. Confirms institutional participation.

### Risk Management

- **Stop-loss**: Entry − (2 × ATR). Adapts to each asset's actual volatility.
- **Take-profit**: Entry + (3 × ATR). Ensures R:R > 1.5:1.
- **Trailing stop**: Ratchets up as price rises, activates after 1× ATR in favor.
- **Force-close**: Day 5 EOD, regardless of P&L. Discipline over hope.

### Exit Priority

1. Hard stop hit → immediate market sell (no human gate)
2. Trailing stop hit → immediate market sell (no human gate)
3. Take-profit hit → limit sell at TP price
4. Day 5 EOD → market close

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

| Component     | Choice                  |
|---------------|-------------------------|
| Language      | Python 3.12+            |
| Broker / Data | Alpaca Markets API      |
| Indicators    | `pandas-ta`             |
| Scheduler     | `APScheduler`           |
| Notifications | `python-telegram-bot`   |
| State store   | SQLite                  |
| Backtesting   | `backtrader` (Phase 2)  |
| Deployment    | Ubuntu VPS + systemd    |

---

## Project Structure

```
swing-trader/
├── src/
│   ├── config.py       # Loads .env, exposes typed Settings dataclass
│   ├── data.py         # Alpaca data fetching (historical + latest bar)
│   ├── indicators.py   # RSI, EMA, MACD, Vol SMA, ATR (pure functions)
│   ├── signals.py      # Four-condition AND gate evaluation
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
SYMBOLS=NVDA,ASML,VOO,QQQM
RSI_THRESHOLD=45
EMA_PROXIMITY_PCT=0.02
VOLUME_SPIKE_MULTIPLIER=1.5
ATR_STOP_MULTIPLIER=2.0
ATR_TP_MULTIPLIER=3.0
RISK_PER_TRADE_PCT=0.02
MAX_OPEN_POSITIONS=2
```

Get your Alpaca paper trading keys at [alpaca.markets](https://alpaca.markets). Create a Telegram bot via [@BotFather](https://t.me/BotFather).

### 3. Validate the data pipeline

```bash
python scripts/validate_data.py
```

All four symbols should return 1 year of clean OHLCV bars with no nulls.

### 4. Run the backtester (Phase 2)

```bash
python backtest.py
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
| 2     | Indicators, signals, backtesting   | In progress |
| 3     | Paper trading automation           | Not started |
| 4     | Live capital deployment            | Not started |

**Phase gate before live trading:** ≥ 30 paper trades, Sharpe > 0.8, max drawdown < 15%.

---

## Risk Guardrails

These are hard-coded constraints that cannot be bypassed:

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
