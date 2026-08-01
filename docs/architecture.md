# Architecture — Swing Trader

## Technology Stack

| Component        | Choice              | Notes                                          |
|------------------|---------------------|------------------------------------------------|
| Language         | Python 3.12+        | Ecosystem fit for quant work                   |
| Broker / Data    | Alpaca Markets API  | Free paper trading, same SDK for live          |
| SDK              | `alpaca-py`         | Official Alpaca Python SDK                     |
| Data wrangling   | `pandas`            | OHLCV frame manipulation                       |
| Indicators       | `pandas-ta`         | SMA, RSI, EMA, MACD, ATR — no C compilation    |
| Scheduler        | `APScheduler`       | In-process cron, weekly triggers               |
| Notifications    | `python-telegram-bot` | Push alerts + reply handling                 |
| State store      | `SQLite` (stdlib)   | Regime state, rebalance log, event log         |
| Config           | `python-dotenv`     | Secrets from `.env`, never hardcoded           |
| Backtesting      | Direct pandas sim   | Calls `portfolio.py`; avoids divergence        |
| Deployment       | Ubuntu VPS + systemd | Hetzner CPX11 (~$7.59/mo)                     |

---

## System Layers

```
┌─────────────────────────────────────────────────────┐
│              Data Ingestion Layer                   │
│   Alpaca API — completed daily bars only            │
│   (today's forming bar is dropped before use)       │
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
│  Paper ↔ Live switched via ALPACA_PAPER only        │
└────────────────────┬────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────┐
│              State & Scheduler Layer                │
│  SQLite — sleeves (regime), rebalance_log, trade_log│
│  APScheduler — rebalance Fri 16:15 ET               │
│              + heartbeat Sat 09:00 ET               │
└─────────────────────────────────────────────────────┘
```

**Why the strategy layer is shared.** The retired strategy failed partly because
the live path evaluated a different bar than the backtest did, and nothing
caught it. `backtest.py` now calls the same `portfolio.py` functions `main.py`
calls, so that class of divergence cannot recur.

---

## Project File Structure

```
swing-trader/
├── docs/
│   ├── project_overview.md
│   ├── architecture.md
│   ├── implementation_phases.md
│   └── strategy_validation.md   # Evidence behind the strategy + its limits
│
├── src/
│   ├── __init__.py
│   ├── config.py           # Loads .env, exposes typed settings
│   ├── data.py             # Alpaca data fetching; drops the forming bar
│   ├── indicators.py       # SMA_200 + legacy indicator columns
│   ├── portfolio.py        # THE STRATEGY — regime, weights, orders
│   ├── risk.py             # Legacy position dataclass (retired strategy)
│   ├── database.py         # SQLite — sleeves, rebalance_log, trade_log
│   ├── notifier.py         # Telegram plan/result alerts + reply handler
│   └── executor.py         # Alpaca order placement + holdings fetch
│
├── tests/
│   ├── test_indicators.py  # Indicator math
│   ├── test_portfolio.py   # Strategy: regime band, weights, order diffing
│   ├── test_risk.py        # Legacy risk helpers
│   ├── test_database.py    # SQLite state store (in-memory)
│   ├── test_notifier.py    # Telegram formatting + send/listen (mocked)
│   └── test_executor.py    # Alpaca order placement (mocked TradingClient)
│
├── scripts/
│   ├── validate_data.py        # Data validation script
│   ├── pull_db.sh              # Pull trades.db snapshot from VPS
│   ├── migrate_db.py           # shares INTEGER → REAL (historical)
│   └── test_notional_order.py  # Integration smoke-test for notional orders
│
├── logs/                   # Runtime logs (gitignored)
├── trades.db               # Local DB snapshot pulled from VPS (gitignored)
├── main.py                 # Entry point — weekly rebalance scheduler
├── backtest.py             # Regime overlay backtester
├── validate_oos.py         # Matched-exposure control, train/test, bootstrap
├── .env                    # Secrets — NEVER commit (gitignored)
├── .gitignore
└── requirements.txt
```

---

## Module Responsibilities

### `config.py`
- Loads all environment variables via `python-dotenv`
- Exposes a single frozen `Settings` dataclass used everywhere
- Fails loudly at startup if any required env var is missing
- Single source of truth for API keys, symbols, and strategy parameters

### `data.py`
- Fetches historical daily OHLCV bars from Alpaca
- `completed_only=True` drops today's still-forming bar. Alpaca includes the
  current session as a bar from the opening bell; evaluating it means deciding
  against a close that has not happened yet
- Returns a clean `pd.DataFrame` with standardised column names
- Callers request ≥365 calendar days so SMA_200 is converged, not merely present

### `indicators.py`
- Pure functions: DataFrame in, same DataFrame with indicator columns appended
- `SMA_200` drives the strategy; the remaining columns (RSI, EMA, MACD, ATR,
  ADX, Stochastic, OBV) are computed but unused by the current strategy
- `MIN_BARS_FOR_STRATEGY = 200` — `main.py` refuses to trade a symbol with less,
  rather than silently treating a NaN regime as "stay flat"

### `portfolio.py` — the entire strategy
Pure, no I/O, no config import. Every threshold is an explicit argument so the
same code runs in the backtest and in production.

- `compute_regime_state(df, band, currently_held)` → `RegimeState(on, context)`.
  Hysteresis around SMA_200. A NaN SMA holds the current state rather than
  churning the sleeve
- `compute_target_weights(states, universe_size, max_position_pct)` → weights.
  Equal weight is 1/`universe_size` — the **configured** count, not the number of
  evaluated sleeves. A symbol that could not be priced leaves its weight in cash
- `validate_target_weights(...)` — asserts hard rules 2 and 5 before any order
- `diff_to_orders(current, target, equity, ...)` → `list[RebalanceOrder]`,
  sells first so proceeds fund the buys

### `database.py`
- Initialises SQLite schema on first run via `init_db(path)`
- `get_regime_states` / `set_regime_state` — the only state the strategy cannot
  re-derive from Alpaca, since hysteresis needs last week's decision
- `log_rebalance_order` — audit row per order (planned/filled/failed/skipped)
- `log_event` — structured events for audit
- `get_weekly_summary(conn, since)` — read-only aggregator for the heartbeat
- Legacy `positions` helpers remain for the retired strategy's historical rows

### `notifier.py`
- `send_rebalance_plan` — the weekly message: every sleeve's close, SMA_200 and
  gap, plus the resulting orders. Enough to decide without opening a laptop
- `send_rebalance_result` — what actually filled, failed, or was skipped
- `send_weekly_summary` — Saturday heartbeat
- Every send returns `bool` and never raises. Hard rule 3 requires reductions to
  execute even when Telegram is unreachable, so no caller may be forced into an
  exception path by a failed send
- Telegram's async API wrapped with `asyncio.run()`

### `executor.py`
- `get_current_holdings()` — Alpaca is the source of truth for what is held, never
  the local database (hard rule 7). A partially-applied rebalance, a manual
  trade, or a crash mid-run all self-correct on the next cycle
- `place_buy_order(symbol, notional)` / `place_sell_notional(...)` — dollar-amount
  market orders, fractional shares supported
- `place_sell_order(symbol, shares, reason)` — full sleeve exits, exact share
  count so no fractional dust is left behind
- Reads `ALPACA_PAPER` on every order call; raises if it changed since startup
- Polls until filled so callers receive the actual fill price

### `main.py`
- APScheduler with two weekly jobs:
  - **Rebalance** — Fri 16:15 ET. Orders queue to Monday's open, reproducing the
    one-bar execution lag the strategy was validated under
  - **Heartbeat** — Sat 09:00 ET, unconditional, so a crashed rebalance is still
    visible
- Orchestrates: holdings → regimes → weights → validate → plan → [gate] →
  execute → log → report
- A symbol that fails to evaluate is omitted entirely: no target weight, no
  order, sleeve untouched. Never liquidated on missing data

---

## Database Schema (SQLite)

### `sleeves` table
```sql
CREATE TABLE sleeves (
    symbol       TEXT PRIMARY KEY,
    regime_on    INTEGER NOT NULL DEFAULT 0,   -- hysteresis needs last week's state
    last_close   REAL,
    last_sma_200 REAL,
    updated_at   TEXT NOT NULL
);
```
Share counts deliberately live in Alpaca, not here (hard rule 7).

### `rebalance_log` table
```sql
CREATE TABLE rebalance_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    symbol    TEXT NOT NULL,
    side      TEXT NOT NULL,          -- buy | sell
    notional  REAL NOT NULL,
    reason    TEXT NOT NULL,          -- regime_entry | regime_exit | drift
    status    TEXT NOT NULL,          -- planned | filled | failed | skipped
    order_id  TEXT,
    detail    TEXT                    -- JSON: Alpaca response or failure reason
);
```

### `trade_log` table
```sql
CREATE TABLE trade_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    symbol    TEXT NOT NULL,          -- 'PORTFOLIO' for whole-plan events
    event     TEXT NOT NULL,          -- plan | error
    detail    TEXT                    -- JSON blob with full context
);
```

### `positions` table (legacy)
Retained so the retired strategy's historical rows survive. Not written by the
current code. Not dropped by any migration.

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

# Universe
SYMBOLS=NVDA,ASML,VOO,QQQM,MSFT,AAPL,AMD,TSM

# Strategy
SMA_BAND=0.02              # hysteresis: exit <0.98×SMA200, enter >1.02×
BARS_LOOKBACK_DAYS=365     # must comfortably exceed 200 trading days

# Rebalancing
DRIFT_TOLERANCE=0.001      # skip trades below 0.1% of equity (dust)
MIN_ORDER_NOTIONAL=1.0     # Alpaca rejects notional orders below $1
REBALANCE_DAY=fri
REBALANCE_HOUR=16
REBALANCE_MINUTE=15
REPLY_TIMEOUT_SECS=14400   # 4h to reply; orders queue to Monday's open anyway
MAX_POSITION_PCT=0.25      # cap per sleeve
```

> Variables from the retired strategy (`RSI_LOWER_BOUND`, `ATR_STOP_MULTIPLIER`,
> `RISK_PER_TRADE_PCT`, `MAX_OPEN_POSITIONS`, …) are no longer read. Leaving them
> in `.env` is harmless.

---

## VPS Operations

**Server:** Hetzner CPX11 — Hillsboro, OR — `5.78.207.143` — Ubuntu 24.04
**User:** `trader` | **Service:** `swing-trader.service` (systemd, auto-starts on reboot)

### Common commands (run from your Mac)

```bash
# Stream live logs
ssh trader@5.78.207.143 "journalctl -u swing-trader -f"

# Check service status
ssh trader@5.78.207.143 "systemctl status swing-trader"

# Restart the service (e.g. after a config change)
ssh trader@5.78.207.143 "sudo systemctl restart swing-trader"

# Pull a fresh DB snapshot for review in DB Browser
bash scripts/pull_db.sh
```

### Updating the code on the VPS

Changes are NOT automatically deployed — push to GitHub, then manually update:

```bash
ssh trader@5.78.207.143 "cd ~/swing-trader && git pull && sudo systemctl restart swing-trader"
```

> `sqlite3` is **not** installed on the VPS — query via `python3.12` instead.

### Reviewing the database locally

```bash
bash scripts/pull_db.sh
```

Then open `trades.db` in DB Browser for SQLite and hit **File → Revert**.
Useful queries:

```sql
-- Which sleeves are currently invested
SELECT symbol, regime_on, last_close, last_sma_200 FROM sleeves ORDER BY symbol;

-- Rebalance history, most recent first
SELECT * FROM rebalance_log ORDER BY timestamp DESC LIMIT 100;

-- Anything that failed
SELECT * FROM rebalance_log WHERE status = 'failed' ORDER BY timestamp DESC;
```

---

## Paper → Live Switch Protocol

The only change required to go from paper to live:

1. Set `ALPACA_PAPER=false` in `.env`
2. Replace `ALPACA_API_KEY` and `ALPACA_API_SECRET` with live credentials
3. Restart the service

Zero code changes. Enforced by design in `config.py` and `executor.py`.

**Note on small accounts.** All orders are notional (dollar-amount), so
fractional shares are automatic. With 8 equal sleeves on a $1,000 account each
target is $125 — ASML at ~$1,600/share resolves to ~0.08 shares, which is fine.
The earlier plan to drop ASML for Phase 4 is no longer needed: it was a
consequence of whole-share sizing under the retired strategy.
