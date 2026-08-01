# Architecture

Technical reference. For what the app does and whether you should use it, see the [README](../README.md).

## Stack

| Component     | Choice                | Notes                                    |
|---------------|-----------------------|------------------------------------------|
| Language      | Python 3.12+          |                                          |
| Broker / data | Alpaca Markets API    | Same SDK for paper and live              |
| SDK           | `alpaca-py`           |                                          |
| Data          | `pandas`              | OHLCV frames                             |
| Indicators    | `pandas-ta`           | 200-day SMA only                         |
| Scheduler     | `APScheduler`         | In-process cron, weekly triggers         |
| Notifications | `python-telegram-bot` | Alerts + reply handling                  |
| State         | `SQLite` (stdlib)     | Regime state, cash ledger, audit log     |
| Config        | `python-dotenv`       | Secrets from `.env`, never hardcoded     |
| Backtesting   | Direct pandas sim     | Calls `portfolio.py`; avoids divergence  |

---

## Layers

```
┌─────────────────────────────────────────────────────┐
│  Data                                               │
│  Alpaca — completed daily bars only                 │
│  (today's forming bar is dropped before use)        │
└────────────────────┬────────────────────────────────┘
┌────────────────────▼────────────────────────────────┐
│  Strategy                                           │
│  indicators.py → portfolio.py                       │
│  SMA_200 → regime state → target weights → orders   │
│  Pure functions; shared by live AND backtest        │
└────────────────────┬────────────────────────────────┘
┌────────────────────▼────────────────────────────────┐
│  Human gate (Telegram)                              │
│  One weekly plan → waits for YES/NO                 │
│  BUYS need YES · SELLS bypass entirely              │
└────────────────────┬────────────────────────────────┘
┌────────────────────▼────────────────────────────────┐
│  Execution                                          │
│  executor.py — notional market orders               │
│  Holdings re-derived from Alpaca every run          │
└────────────────────┬────────────────────────────────┘
┌────────────────────▼────────────────────────────────┐
│  State & scheduling                                 │
│  SQLite — sleeves, strategy_state, rebalance_log    │
│  APScheduler — rebalance Fri 16:15 ET               │
│                heartbeat Sat 09:00 ET               │
└─────────────────────────────────────────────────────┘
```

**Why the strategy layer is shared.** `backtest.py` calls the same `portfolio.py`
functions `main.py` calls. A backtest that passes therefore validates the
production code rather than a parallel reimplementation of it.

---

## Files

```
swing-trader/
├── docs/architecture.md
├── src/
│   ├── config.py       # .env → typed frozen Settings
│   ├── data.py         # Alpaca bars; drops today's forming bar
│   ├── indicators.py   # SMA_200
│   ├── portfolio.py    # THE STRATEGY — regime, weights, orders
│   ├── database.py     # SQLite: sleeves, strategy_state, rebalance_log
│   ├── notifier.py     # Telegram plan/result + reply handling
│   └── executor.py     # Alpaca orders, holdings, account state
├── tests/              # 249 tests
├── scripts/
│   ├── validate_data.py
│   └── test_notional_order.py
├── main.py             # weekly rebalance scheduler
├── backtest.py         # strategy tester
├── validate_oos.py     # matched-exposure control, train/test, bootstrap
└── requirements.txt
```

---

## Modules

### `config.py`
Loads `.env`, exposes one frozen `Settings`. Fails loudly at startup if a
required variable is missing. `TRADING_CAPITAL` is required — the app must
refuse to start rather than guess how much money to deploy.

### `data.py`
Fetches daily OHLCV from Alpaca.

`completed_only=True` drops today's bar **while the session is still open**.
Alpaca includes the current session as a bar from the opening bell, so acting on
it means deciding against a close that has not happened. After 16:00 ET that
same bar is final and is kept — dropping it there would give live a two-bar
execution lag where the backtest has one.

Callers request ≥365 calendar days so `SMA_200` is converged, not merely present.

### `indicators.py`
`SMA_200` is the only column any code reads.

`MIN_BARS_FOR_STRATEGY = 200` — `main.py` refuses to trade a symbol with less,
rather than silently treating a NaN regime as "stay flat".

### `portfolio.py` — the entire strategy
Pure: no I/O, no config import. Every threshold is an explicit argument, so the
same code runs in the backtest and in production.

- `compute_regime_state(df, band, currently_held)` → `RegimeState(on, context)`.
  Hysteresis around `SMA_200`. A NaN SMA holds the current state rather than
  churning the sleeve.
- `compute_target_weights(states, universe_size, max_position_pct)` → weights.
  Equal weight is 1/`universe_size` — the **configured** count, not the number of
  evaluated sleeves. A symbol that could not be priced leaves its weight in cash
  instead of handing it to the survivors.
- `validate_target_weights(...)` — asserts the hard rules before any order.
- `diff_to_orders(current, target, equity, ...)` → `list[RebalanceOrder]`, sells
  first so proceeds fund the buys. `drift_tolerance` gates **drift only** —
  regime entries and exits are decisions, not sizing adjustments, and must never
  be suppressed by a threshold.

### `database.py`
- `get_regime_states` / `set_regime_state` — the only state not derivable from
  Alpaca, since hysteresis needs last week's decision
- `get_strategy_cash` / `set_strategy_cash` — the strategy's own cash ledger, so
  a $100k account can trade the $1k allocated to it
- `log_rebalance_order` — one audit row per order attempt and outcome
- `get_weekly_summary(conn, since)` — read-only aggregator for the heartbeat

### `notifier.py`
- `send_rebalance_plan` — the weekly message: every sleeve's close, `SMA_200`,
  the gap, and the resulting orders. Enough to decide without a laptop
- `send_rebalance_result` — what filled, failed, or was skipped
- `send_weekly_summary` — Saturday heartbeat
- Every send returns `bool` and never raises. Sells must execute even when
  Telegram is unreachable, so no caller may be forced into an exception path

### `executor.py`
- `get_current_holdings()` — Alpaca is the source of truth for what is held,
  never the local database. A partially-applied rebalance, a manual trade, or a
  crash mid-run all self-correct on the next cycle
- `get_account_equity()` / `get_account_cash()` — ceilings on the strategy ledger
- `place_buy_order` / `place_sell_notional` — dollar-amount orders, fractional
  shares supported
- `place_sell_order` — full exits by exact share count, so no fractional dust
- Re-reads `ALPACA_PAPER` on every order; raises if it changed since startup
- Polls until filled so callers see the real fill price

### `main.py`
Two weekly jobs:
- **Rebalance** — Fri 16:15 ET. Orders queue to Monday's open, reproducing the
  one-bar lag the strategy was validated under
- **Heartbeat** — Sat 09:00 ET, unconditional, so a crashed rebalance is visible

Flow: holdings → regimes → weights → validate → plan → [gate] → execute → log →
report.

A symbol that fails to evaluate is omitted entirely: no target weight, no order,
sleeve untouched. Never liquidated on missing data. Both jobs are wrapped so no
unhandled exception can die silently inside the scheduler.

---

## Database schema

```sql
-- Regime flag per symbol. Hysteresis needs last week's state, and this is the
-- only thing not derivable from Alpaca. Share counts live in Alpaca, not here.
CREATE TABLE sleeves (
    symbol       TEXT PRIMARY KEY,
    regime_on    INTEGER NOT NULL DEFAULT 0,
    last_close   REAL,
    last_sma_200 REAL,
    updated_at   TEXT NOT NULL
);

-- The strategy's own cash ledger, so a $100k account can trade $1k.
-- strategy equity = managed sleeve value + this cash.
CREATE TABLE strategy_state (
    key        TEXT PRIMARY KEY,     -- 'strategy_cash'
    value      REAL NOT NULL,
    updated_at TEXT NOT NULL
);

-- One row per order attempt.
CREATE TABLE rebalance_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    symbol    TEXT NOT NULL,
    side      TEXT NOT NULL,   -- buy | sell
    notional  REAL NOT NULL,
    reason    TEXT NOT NULL,   -- regime_entry | regime_exit | drift
    status    TEXT NOT NULL,   -- filled | partial | failed | skipped
    order_id  TEXT,
    detail    TEXT             -- JSON: Alpaca response or failure reason
);

-- Whole-plan events.
CREATE TABLE trade_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    symbol    TEXT NOT NULL,   -- 'PORTFOLIO' for plan-level events
    event     TEXT NOT NULL,   -- plan | error
    detail    TEXT             -- JSON
);
```

---

## Capital model

Sizing uses the **strategy's** capital, never the account balance:

```
strategy equity = market value of managed sleeves + strategy cash ledger
```

seeded from `TRADING_CAPITAL`. Profits compound; money deposited into the
account but never allocated stays invisible. Account equity and cash are
ceilings only — if the ledger ever claims more than the account holds, the
account wins.

The ledger moves only on actual fills, so skipped and failed orders cannot drift
it away from reality.

**Account dedication is assumed.** Positions outside `SYMBOLS` are never sold and
never counted as strategy capital, but each run warns about them. A short
position in a managed symbol aborts the run — this strategy is long-only, and a
negative holding would invert the order arithmetic.

---

## Operational notes

**Order timing.** The rebalance runs after Friday's close, so market orders queue
to Monday's open. That is deliberate: it reproduces the execution lag the
strategy was tested under.

**Cash vs margin accounts.** When every sleeve is on, the plan deploys 100% of
allocated capital, and a rebalance that sells one sleeve to fund another does
both in the same run. On a margin account (including Alpaca paper) proceeds are
available immediately. On a **cash** account, sale proceeds settle T+1, so a
same-run buy funded by that morning's sale can be rejected for unsettled funds.
The failure is safe — logged, alerted, and retried on the next weekly run.

**Reviewing the database.** `sqlite3` may not be installed on a server; query via
`python3.12` instead.

```sql
-- which sleeves are currently invested
SELECT symbol, regime_on, last_close, last_sma_200 FROM sleeves ORDER BY symbol;

-- how much capital the strategy thinks it has uninvested
SELECT value FROM strategy_state WHERE key = 'strategy_cash';

-- anything that failed
SELECT * FROM rebalance_log WHERE status IN ('failed','partial')
ORDER BY timestamp DESC;
```

---

## Paper → live

1. `ALPACA_PAPER=false` in `.env`
2. Replace the API keys with live credentials
3. Restart

Zero code changes, enforced by design in `config.py` and `executor.py`.

All orders are notional, so fractional shares are automatic. On $1,000 each
sleeve targets $125, and ASML at ~$1,600/share resolves to ~0.08 shares.
