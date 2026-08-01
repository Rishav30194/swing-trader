"""
database.py — SQLite state store for the regime overlay.

All functions accept an explicit sqlite3.Connection so they are testable
with an in-memory database (':memory:') and have no global mutable state.

Typical usage:
    from src.database import init_db, get_regime_states, get_strategy_cash
    conn = init_db("trades.db")
    ...
    conn.close()

Schema (see docs/architecture.md):
  sleeves        — regime flag per symbol; the hysteresis band needs last week's
                   state, and it is the only thing not derivable from Alpaca
  strategy_state — the strategy's own cash ledger, so a $100k account can trade
                   the $1k allocated to it
  rebalance_log  — one row per order attempt, with its outcome
  trade_log      — append-only event log

Share counts deliberately live in Alpaca, not here (hard rule 7). The retired
signal strategy's `positions` table is no longer created; existing databases
keep theirs untouched.
"""

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WeeklySummary:
    """Aggregated activity over a reporting window, for the weekly heartbeat."""
    period_start:     date
    period_end:       date
    sleeves_on:       list[str]
    sleeves_off:      list[str]
    orders_filled:    int
    orders_failed:    int
    notional_bought:  float
    notional_sold:    float

_CREATE_TRADE_LOG = """
CREATE TABLE IF NOT EXISTS trade_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT    NOT NULL,
    symbol    TEXT    NOT NULL,
    event     TEXT    NOT NULL,
    detail    TEXT
);
"""

# Regime state is the only thing the strategy cannot re-derive from Alpaca:
# the hysteresis band needs to know whether a sleeve was held last week.
# Share counts deliberately live in Alpaca, not here (hard rule 7).
_CREATE_SLEEVES = """
CREATE TABLE IF NOT EXISTS sleeves (
    symbol       TEXT    PRIMARY KEY,
    regime_on    INTEGER NOT NULL DEFAULT 0,
    last_close   REAL,
    last_sma_200 REAL,
    updated_at   TEXT    NOT NULL
);
"""

# The strategy's own cash ledger. The account balance is NOT the strategy's
# capital: a $100,000 paper account must still trade the $1,000 allocated to it.
# Strategy equity = market value of managed sleeves + this cash, so profits
# compound while unallocated money in the account is never touched.
_CREATE_STRATEGY_STATE = """
CREATE TABLE IF NOT EXISTS strategy_state (
    key        TEXT PRIMARY KEY,
    value      REAL NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_CREATE_REBALANCE_LOG = """
CREATE TABLE IF NOT EXISTS rebalance_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT    NOT NULL,
    symbol    TEXT    NOT NULL,
    side      TEXT    NOT NULL,
    notional  REAL    NOT NULL,
    reason    TEXT    NOT NULL,
    status    TEXT    NOT NULL,
    order_id  TEXT,
    detail    TEXT
);
"""


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------

def init_db(path: str | Path = "trades.db") -> sqlite3.Connection:
    """
    Open (or create) the SQLite database at `path` and ensure the schema exists.

    Pass ':memory:' in tests to get an isolated in-memory database.

    Returns:
        An open sqlite3.Connection with WAL mode and foreign keys enabled.
    """
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row  # lets callers access columns by name
    conn.execute("PRAGMA journal_mode=WAL")  # safe for concurrent readers
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        _CREATE_TRADE_LOG + _CREATE_SLEEVES
        + _CREATE_REBALANCE_LOG + _CREATE_STRATEGY_STATE
    )
    conn.commit()
    logger.info("Database initialised at %s", path)
    return conn


# ---------------------------------------------------------------------------
# Trade log
# ---------------------------------------------------------------------------

def log_event(
    conn:   sqlite3.Connection,
    symbol: str,
    event:  str,
    detail: dict | None = None,
) -> None:
    """
    Append one row to `trade_log`.

    Args:
        conn:   Open database connection.
        symbol: Ticker symbol (e.g. "NVDA").
        event:  Short event label. Defined values:
                  signal | approved | rejected | bought | sold | stop_updated | error
        detail: Optional dict serialised as JSON. Carry the full signal context,
                order response, or error traceback here.
    """
    ts     = datetime.now(timezone.utc).isoformat()
    detail_json = json.dumps(detail) if detail is not None else None
    conn.execute(
        "INSERT INTO trade_log (timestamp, symbol, event, detail) VALUES (?, ?, ?, ?)",
        (ts, symbol, event, detail_json),
    )
    conn.commit()
    logger.debug("trade_log: %s  %s  %s", ts, symbol, event)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Sleeve regime state
# ---------------------------------------------------------------------------

def get_regime_states(conn: sqlite3.Connection) -> dict[str, bool]:
    """
    Return the persisted regime flag for every known sleeve.

    Symbols absent from the result have never been recorded and must be treated
    as flat (not held) by the caller — which is the safe default, since entering
    requires clearing the upper band explicitly.
    """
    rows = conn.execute("SELECT symbol, regime_on FROM sleeves").fetchall()
    return {row["symbol"]: bool(row["regime_on"]) for row in rows}


def set_regime_state(
    conn: sqlite3.Connection,
    symbol: str,
    on: bool,
    *,
    last_close: float | None = None,
    last_sma_200: float | None = None,
) -> None:
    """Upsert one sleeve's regime flag and the values behind the decision."""
    conn.execute(
        """
        INSERT INTO sleeves (symbol, regime_on, last_close, last_sma_200, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(symbol) DO UPDATE SET
            regime_on    = excluded.regime_on,
            last_close   = excluded.last_close,
            last_sma_200 = excluded.last_sma_200,
            updated_at   = excluded.updated_at
        """,
        (symbol, int(on), last_close, last_sma_200,
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    logger.debug("sleeve %s regime_on=%s", symbol, on)


# ---------------------------------------------------------------------------
# Strategy cash ledger
# ---------------------------------------------------------------------------

_STRATEGY_CASH = "strategy_cash"


def get_strategy_cash(conn: sqlite3.Connection, default: float) -> float:
    """
    Return the strategy's uninvested cash, seeding it with `default` on first run.

    `default` is TRADING_CAPITAL — the initial allocation. After that the ledger
    moves only when the strategy itself buys or sells, so money deposited into
    the account later is never picked up automatically.
    """
    row = conn.execute(
        "SELECT value FROM strategy_state WHERE key = ?", (_STRATEGY_CASH,)
    ).fetchone()
    if row is not None:
        return float(row["value"])

    set_strategy_cash(conn, default)
    logger.info("Strategy cash ledger initialised at $%.2f", default)
    return default


def set_strategy_cash(conn: sqlite3.Connection, value: float) -> None:
    """Persist the strategy's uninvested cash. Never stores a negative balance."""
    value = max(0.0, value)
    conn.execute(
        """
        INSERT INTO strategy_state (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value, updated_at = excluded.updated_at
        """,
        (_STRATEGY_CASH, value, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    logger.debug("strategy_cash = %.2f", value)


# ---------------------------------------------------------------------------
# Rebalance log
# ---------------------------------------------------------------------------

def log_rebalance_order(
    conn:     sqlite3.Connection,
    symbol:   str,
    side:     str,
    notional: float,
    reason:   str,
    status:   str,
    order_id: str | None = None,
    detail:   dict | None = None,
) -> None:
    """
    Append one row to `rebalance_log`.

    Args:
        status: planned | filled | failed | skipped
        detail: Optional dict serialised as JSON — carry the Alpaca response
                or the failure reason here.
    """
    conn.execute(
        """
        INSERT INTO rebalance_log
            (timestamp, symbol, side, notional, reason, status, order_id, detail)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (datetime.now(timezone.utc).isoformat(), symbol, side, notional, reason,
         status, order_id, json.dumps(detail) if detail is not None else None),
    )
    conn.commit()
    logger.debug("rebalance_log: %s %s $%.2f %s", symbol, side, notional, status)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def get_weekly_summary(conn: sqlite3.Connection, since: date) -> WeeklySummary:
    """
    Aggregate rebalance activity from `since` (inclusive) up to now.

    Read-only. ISO timestamp strings compare lexicographically, so a plain date
    bound matches "YYYY-MM-DDT…" correctly.

    Returns:
        A WeeklySummary covering [since, today].
    """
    since_iso = since.isoformat()

    states = get_regime_states(conn)
    sleeves_on = sorted(s for s, on in states.items() if on)
    sleeves_off = sorted(s for s, on in states.items() if not on)

    rows = conn.execute(
        """
        SELECT side, notional, status FROM rebalance_log
        WHERE timestamp >= ?
        """,
        (since_iso,),
    ).fetchall()

    # A partial fill moved real money, so it counts toward the traded notional.
    filled = [r for r in rows if r["status"] in ("filled", "partial")]
    return WeeklySummary(
        period_start=since,
        period_end=date.today(),
        sleeves_on=sleeves_on,
        sleeves_off=sleeves_off,
        orders_filled=len(filled),
        orders_failed=sum(1 for r in rows if r["status"] == "failed"),
        notional_bought=sum(r["notional"] for r in filled if r["side"] == "buy"),
        notional_sold=sum(r["notional"] for r in filled if r["side"] == "sell"),
    )
