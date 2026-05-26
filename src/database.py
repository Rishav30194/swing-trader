"""
database.py — SQLite state store for open positions and the trade log.

All functions accept an explicit sqlite3.Connection so they are testable
with an in-memory database (':memory:') and have no global mutable state.

Typical usage:
    from src.database import init_db, save_position, get_open_positions
    conn = init_db("trades.db")
    ...
    conn.close()

Schema (matches docs/architecture.md exactly):
  positions  — one row per trade, status open|closed
  trade_log  — append-only event log (signal, approved, rejected, bought, sold, …)
"""

import json
import logging
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from src.risk import Position

logger = logging.getLogger(__name__)

_CREATE_POSITIONS = """
CREATE TABLE IF NOT EXISTS positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT    NOT NULL,
    entry_date      TEXT    NOT NULL,
    entry_price     REAL    NOT NULL,
    shares          REAL    NOT NULL,
    stop_loss       REAL    NOT NULL,
    trailing_stop   REAL    NOT NULL,
    take_profit     REAL    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'open',
    exit_date       TEXT,
    exit_price      REAL,
    exit_reason     TEXT,
    pnl_dollars     REAL,
    pnl_pct         REAL
);
"""

_CREATE_TRADE_LOG = """
CREATE TABLE IF NOT EXISTS trade_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT    NOT NULL,
    symbol    TEXT    NOT NULL,
    event     TEXT    NOT NULL,
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
    conn.executescript(_CREATE_POSITIONS + _CREATE_TRADE_LOG)
    conn.commit()
    logger.info("Database initialised at %s", path)
    return conn


# ---------------------------------------------------------------------------
# Position operations
# ---------------------------------------------------------------------------

def save_position(conn: sqlite3.Connection, position: Position) -> int:
    """
    Insert a new open position into the `positions` table.

    Returns:
        The auto-assigned row id (set as `position.db_id` by the caller).

    Raises:
        sqlite3.Error on any DB failure.
    """
    cursor = conn.execute(
        """
        INSERT INTO positions
            (symbol, entry_date, entry_price, shares,
             stop_loss, trailing_stop, take_profit, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'open')
        """,
        (
            position.symbol,
            position.entry_date.isoformat(),
            position.entry_price,
            position.shares,
            position.stop_loss,
            position.trailing_stop,
            position.take_profit,
        ),
    )
    conn.commit()
    db_id = cursor.lastrowid
    logger.info("Saved position id=%d  %s  %.4f shares @ %.4f", db_id, position.symbol, position.shares, position.entry_price)
    return db_id  # type: ignore[return-value]


def update_position(conn: sqlite3.Connection, db_id: int, **fields) -> None:
    """
    Update one or more columns on a positions row by its id.

    Allowed field names (any subset):
        stop_loss, trailing_stop, status, exit_date, exit_price,
        exit_reason, pnl_dollars, pnl_pct

    Raises:
        ValueError  if `fields` is empty or contains unknown column names.
        sqlite3.Error on any DB failure.
    """
    _ALLOWED = {
        "stop_loss", "trailing_stop", "status",
        "exit_date", "exit_price", "exit_reason",
        "pnl_dollars", "pnl_pct",
    }
    unknown = set(fields) - _ALLOWED
    if unknown:
        raise ValueError(f"update_position: unknown column(s): {unknown}")
    if not fields:
        raise ValueError("update_position: at least one field required")

    set_clause = ", ".join(f"{col} = ?" for col in fields)
    values     = list(fields.values()) + [db_id]
    conn.execute(f"UPDATE positions SET {set_clause} WHERE id = ?", values)
    conn.commit()
    logger.debug("Updated position id=%d  fields=%s", db_id, list(fields))


def get_open_positions(conn: sqlite3.Connection) -> list[Position]:
    """
    Return all rows in `positions` where status = 'open' as Position objects.

    The `db_id` field on each returned Position is populated from the row id
    so callers can pass it back to `update_position`.

    Returns:
        List of Position objects (may be empty if no open positions exist).
    """
    rows = conn.execute(
        """
        SELECT id, symbol, entry_date, entry_price, shares,
               stop_loss, trailing_stop, take_profit
        FROM positions
        WHERE status = 'open'
        ORDER BY entry_date ASC
        """
    ).fetchall()

    positions = []
    for row in rows:
        pos = Position(
            symbol=row["symbol"],
            entry_price=row["entry_price"],
            shares=row["shares"],
            stop_loss=row["stop_loss"],
            trailing_stop=row["trailing_stop"],
            take_profit=row["take_profit"],
            entry_date=date.fromisoformat(row["entry_date"]),
            db_id=row["id"],
        )
        positions.append(pos)

    return positions


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
