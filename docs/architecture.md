# Architecture — Swing Trader

## Technology Stack

| Component        | Choice              | Notes                                          |
|------------------|---------------------|------------------------------------------------|
| Language         | Python 3.12+        | Ecosystem fit for quant work                   |
| Broker / Data    | Alpaca Markets API  | Free paper trading, same SDK for live          |
| SDK              | `alpaca-py`         | Official Alpaca Python SDK                     |
| Data wrangling   | `pandas`            | OHLCV frame manipulation                       |
| Indicators       | `pandas-ta`         | RSI, EMA, MACD, ATR — no C compilation needed |
| Scheduler        | `APScheduler`       | In-process cron, market-hours gating           |
| Notifications    | `python-telegram-bot` | Push alerts + reply handling               |
| State store      | `SQLite` (stdlib)   | Open positions, trade log, PnL history         |
| Config           | `python-dotenv`     | Secrets from `.env`, never hardcoded           |
| Backtesting      | Direct pandas sim   | Calls production modules; avoids divergence    |
| Deployment       | Ubuntu VPS + systemd | Hetzner CX11 or DigitalOcean Droplet (~$5/mo) |

---

## System Layers

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
│  Paper env (Phase 1) ↔ Live env (Phase 4)           │
│  Switched via ALPACA_BASE_URL env var only          │
└────────────────────┬────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────┐
│              State & Scheduler Layer                │
│  SQLite — positions, trade log                      │
│  APScheduler — scans every 15 min, 9:45–15:45 EST   │
└─────────────────────────────────────────────────────┘
```

---

## Project File Structure

```
swing-trader/
├── docs/                        # ← You are here
│   ├── project_overview.md
│   ├── architecture.md
│   └── implementation_phases.md
│
├── src/
│   ├── __init__.py
│   ├── config.py           # Loads .env, exposes typed settings
│   ├── data.py             # Alpaca data fetching (historical + live)
│   ├── indicators.py       # RSI, EMA21/50, MACD, Vol SMA, ATR, ADX, Stochastic, OBV
│   ├── signals.py          # Buy signal AND-gate evaluation
│   ├── risk.py             # ATR-based SL/TP, trailing stop logic
│   ├── database.py         # SQLite state store — positions + trade log
│   ├── notifier.py         # Telegram push alerts + reply handler
│   └── executor.py         # Alpaca order placement (paper + live)
│
├── tests/
│   ├── test_indicators.py  # Unit tests for indicator math
│   ├── test_signals.py     # Unit tests for signal logic
│   ├── test_risk.py        # Unit tests for SL/TP calculations
│   └── test_database.py    # Unit tests for SQLite state store (in-memory)
│
├── logs/                   # Runtime logs (gitignored)
├── main.py                 # Entry point — wires scheduler + modules
├── backtest.py             # Phase 2 standalone backtest runner
├── validate_oos.py         # OOS statistical validation (walk-forward, permutation, bootstrap)
├── .env                    # Secrets — NEVER commit (gitignored)
├── .gitignore
└── requirements.txt
```

---

## Module Responsibilities

### `config.py`
- Loads all environment variables via `python-dotenv`
- Exposes a single `Settings` dataclass or object used everywhere
- Fails loudly at startup if any required env var is missing
- Single source of truth for: API keys, symbols list, strategy parameters

### `data.py`
- Fetches historical daily OHLCV bars from Alpaca (up to 1 year)
- Fetches latest bar for real-time scanning during market hours
- Returns clean `pd.DataFrame` with standardized column names
- Handles Alpaca pagination and rate limits transparently

### `indicators.py`
- Pure functions: input is a DataFrame, output is the same DataFrame
  with indicator columns appended
- Computes: RSI(14), EMA(21), EMA(50), MACD(12,26,9), Vol SMA(20), ATR(14),
  ADX(14), Stochastic(14,3,3), OBV
- No side effects, no API calls — purely mathematical

### `signals.py`
- Imports from `indicators.py`
- Implements the three-condition AND gate:
  1. Close > EMA_50 (trend filter)
  2. 40 ≤ RSI(14) < 55 (pullback in uptrend)
  3. MACD bullish crossover on this bar
- Returns a typed result: `SignalResult(triggered: bool, context: dict)`
- The `context` dict carries all values for the Telegram alert message

### `risk.py`
- Computes ATR-based stop-loss (1.5× ATR) and take-profit (2× ATR) levels
- Computes position size based on account equity and risk-per-trade %
- Updates trailing stop: only moves up, never down; activates at 0.5× ATR gain
- Checks exit conditions: hard stop, trailing stop, TP, day-5 rule

### `database.py`
- Initialises SQLite schema on first run via `init_db(path)`
- `save_position` / `update_position` / `get_open_positions` — position lifecycle
- `log_event` — appends structured events to `trade_log` for audit and review
- In-memory SQLite used in tests; file-backed in production

### `notifier.py`
- Sends formatted Telegram messages for all events
- Handles the YES/NO reply flow for trade entry approval
- Separate notification paths for entries (gated) vs exits (immediate)
- Telegram's async API wrapped with `asyncio.run()` — rest of codebase stays synchronous

### `executor.py`
- Places market and limit orders via Alpaca SDK
- Reads `ALPACA_PAPER` env var to target paper vs live environment
- Logs every order attempt and response to SQLite and log file
- Never called directly — always invoked through the human gate flow

### `main.py`
- Initializes APScheduler
- Market hours check: skips scan outside 9:45–15:45 EST on trading days
- Orchestrates: fetch → indicators → signals → [gate] → execute → log

---

## Database Schema (SQLite)

### `positions` table
```sql
CREATE TABLE positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    entry_date      TEXT NOT NULL,          -- ISO 8601
    entry_price     REAL NOT NULL,
    shares          INTEGER NOT NULL,
    stop_loss       REAL NOT NULL,
    trailing_stop   REAL NOT NULL,
    take_profit     REAL NOT NULL,
    status          TEXT DEFAULT 'open',    -- open | closed
    exit_date       TEXT,
    exit_price      REAL,
    exit_reason     TEXT,                   -- sl | trailing | tp | day5 | manual
    pnl_dollars     REAL,
    pnl_pct         REAL
);
```

### `trade_log` table
```sql
CREATE TABLE trade_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    event           TEXT NOT NULL,          -- signal | approved | rejected | bought | sold | stop_updated
    detail          TEXT                    -- JSON blob with full context
);
```

---

## Environment Variables (.env)

```
# Alpaca — Paper Trading
ALPACA_API_KEY=your_paper_key_here
ALPACA_API_SECRET=your_paper_secret_here
ALPACA_PAPER=true                          # flip to false for Phase 4

# Telegram
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here

# Strategy parameters (override defaults without touching code)
SYMBOLS=NVDA,ASML,VOO,QQQM,MSFT,AAPL,AMD,TSM
RSI_LOWER_BOUND=40                         # RSI must be at or above this (no panic-sell entries)
RSI_UPPER_BOUND=55                         # RSI must be below this (only buy actual dips)
ATR_STOP_MULTIPLIER=1.5                    # hard stop = entry − (1.5 × ATR)
ATR_TP_MULTIPLIER=2.0                      # take-profit = entry + (2 × ATR)
ATR_TRAILING_ACTIVATION=0.5               # trailing stop activates after 0.5 × ATR gain
RISK_PER_TRADE_PCT=0.02                   # 2% of account equity per trade
MAX_OPEN_POSITIONS=2                       # never hold more than 2 at once
```

---

## Paper → Live Switch Protocol

The only change required to go from paper to live:

1. Set `ALPACA_PAPER=false` in `.env`
2. Replace `ALPACA_API_KEY` and `ALPACA_API_SECRET` with live credentials
3. Restart the service

Zero code changes. This is enforced by design in `config.py` and `executor.py`.
