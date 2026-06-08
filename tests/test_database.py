"""
Unit tests for src/database.py.

All tests use an in-memory SQLite database (':memory:') — no files created,
no state shared between tests.
"""

import json
from datetime import date

import pytest

from datetime import timedelta

from src.database import (
    init_db,
    save_position,
    update_position,
    get_open_positions,
    get_weekly_summary,
    log_event,
)
from src.risk import Position


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def conn():
    """Fresh in-memory database for each test."""
    c = init_db(":memory:")
    yield c
    c.close()


def _make_position(**overrides) -> Position:
    defaults = dict(
        symbol="NVDA",
        entry_price=500.0,
        shares=10,
        stop_loss=492.5,
        trailing_stop=492.5,
        take_profit=510.0,
        entry_date=date(2024, 3, 1),
    )
    defaults.update(overrides)
    return Position(**defaults)


# ---------------------------------------------------------------------------
# init_db
# ---------------------------------------------------------------------------

def test_init_db_creates_tables(conn):
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "positions" in tables
    assert "trade_log" in tables


def test_init_db_idempotent():
    """Calling init_db twice on the same path must not fail or duplicate tables."""
    import tempfile, os
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        c1 = init_db(path)
        c1.close()
        c2 = init_db(path)
        c2.close()
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# save_position
# ---------------------------------------------------------------------------

def test_save_position_returns_integer_id(conn):
    pos = _make_position()
    db_id = save_position(conn, pos)
    assert isinstance(db_id, int)
    assert db_id >= 1


def test_save_position_persists_all_fields(conn):
    pos = _make_position(symbol="AAPL", entry_price=175.5, shares=5)
    db_id = save_position(conn, pos)

    row = conn.execute("SELECT * FROM positions WHERE id = ?", (db_id,)).fetchone()
    assert row["symbol"]       == "AAPL"
    assert row["entry_price"]  == pytest.approx(175.5)
    assert row["shares"]       == 5
    assert row["stop_loss"]    == pytest.approx(pos.stop_loss)
    assert row["trailing_stop"] == pytest.approx(pos.trailing_stop)
    assert row["take_profit"]  == pytest.approx(pos.take_profit)
    assert row["entry_date"]   == pos.entry_date.isoformat()
    assert row["status"]       == "open"


def test_save_position_initial_status_is_open(conn):
    db_id = save_position(conn, _make_position())
    row = conn.execute("SELECT status FROM positions WHERE id = ?", (db_id,)).fetchone()
    assert row["status"] == "open"


def test_save_multiple_positions(conn):
    id1 = save_position(conn, _make_position(symbol="NVDA"))
    id2 = save_position(conn, _make_position(symbol="AAPL"))
    assert id1 != id2
    count = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
    assert count == 2


# ---------------------------------------------------------------------------
# update_position
# ---------------------------------------------------------------------------

def test_update_trailing_stop(conn):
    db_id = save_position(conn, _make_position(trailing_stop=492.5))
    update_position(conn, db_id, trailing_stop=497.0)
    row = conn.execute("SELECT trailing_stop FROM positions WHERE id = ?", (db_id,)).fetchone()
    assert row["trailing_stop"] == pytest.approx(497.0)


def test_update_status_to_closed(conn):
    db_id = save_position(conn, _make_position())
    update_position(conn, db_id, status="closed", exit_price=510.0, exit_reason="tp",
                    pnl_dollars=100.0, pnl_pct=2.0, exit_date="2024-03-06")
    row = conn.execute("SELECT * FROM positions WHERE id = ?", (db_id,)).fetchone()
    assert row["status"]       == "closed"
    assert row["exit_price"]   == pytest.approx(510.0)
    assert row["exit_reason"]  == "tp"
    assert row["pnl_dollars"]  == pytest.approx(100.0)
    assert row["exit_date"]    == "2024-03-06"


def test_update_raises_on_unknown_column(conn):
    db_id = save_position(conn, _make_position())
    with pytest.raises(ValueError, match="unknown column"):
        update_position(conn, db_id, nonexistent_column=99)


def test_update_raises_on_empty_fields(conn):
    db_id = save_position(conn, _make_position())
    with pytest.raises(ValueError, match="at least one field"):
        update_position(conn, db_id)


def test_update_only_affects_target_row(conn):
    id1 = save_position(conn, _make_position(symbol="NVDA", trailing_stop=492.5))
    id2 = save_position(conn, _make_position(symbol="AAPL", trailing_stop=170.0))
    update_position(conn, id1, trailing_stop=498.0)

    row2 = conn.execute("SELECT trailing_stop FROM positions WHERE id = ?", (id2,)).fetchone()
    assert row2["trailing_stop"] == pytest.approx(170.0)  # unchanged


# ---------------------------------------------------------------------------
# get_open_positions
# ---------------------------------------------------------------------------

def test_get_open_positions_returns_empty_list_when_none(conn):
    assert get_open_positions(conn) == []


def test_get_open_positions_returns_open_rows(conn):
    save_position(conn, _make_position(symbol="NVDA"))
    save_position(conn, _make_position(symbol="AAPL"))
    positions = get_open_positions(conn)
    assert len(positions) == 2
    symbols = {p.symbol for p in positions}
    assert symbols == {"NVDA", "AAPL"}


def test_get_open_positions_excludes_closed(conn):
    db_id = save_position(conn, _make_position(symbol="NVDA"))
    save_position(conn, _make_position(symbol="AAPL"))
    update_position(conn, db_id, status="closed", exit_price=510.0, exit_reason="tp",
                    pnl_dollars=100.0, pnl_pct=2.0, exit_date="2024-03-06")

    positions = get_open_positions(conn)
    assert len(positions) == 1
    assert positions[0].symbol == "AAPL"


def test_get_open_positions_populates_db_id(conn):
    db_id = save_position(conn, _make_position())
    positions = get_open_positions(conn)
    assert positions[0].db_id == db_id


def test_get_open_positions_restores_all_fields(conn):
    original = _make_position(
        symbol="MSFT", entry_price=420.0, shares=3,
        stop_loss=411.0, trailing_stop=411.0, take_profit=436.0,
        entry_date=date(2024, 4, 15),
    )
    save_position(conn, original)
    restored = get_open_positions(conn)[0]

    assert restored.symbol        == original.symbol
    assert restored.entry_price   == pytest.approx(original.entry_price)
    assert restored.shares        == original.shares
    assert restored.stop_loss     == pytest.approx(original.stop_loss)
    assert restored.trailing_stop == pytest.approx(original.trailing_stop)
    assert restored.take_profit   == pytest.approx(original.take_profit)
    assert restored.entry_date    == original.entry_date


# ---------------------------------------------------------------------------
# log_event
# ---------------------------------------------------------------------------

def test_log_event_inserts_row(conn):
    log_event(conn, "NVDA", "signal", {"rsi": 47.2, "macd": 0.5})
    count = conn.execute("SELECT COUNT(*) FROM trade_log").fetchone()[0]
    assert count == 1


def test_log_event_stores_correct_fields(conn):
    detail = {"rsi": 47.2, "triggered": True}
    log_event(conn, "NVDA", "signal", detail)
    row = conn.execute("SELECT * FROM trade_log").fetchone()
    assert row["symbol"] == "NVDA"
    assert row["event"]  == "signal"
    assert json.loads(row["detail"]) == detail


def test_log_event_without_detail(conn):
    log_event(conn, "AAPL", "error")
    row = conn.execute("SELECT detail FROM trade_log").fetchone()
    assert row["detail"] is None


def test_log_event_timestamp_is_set(conn):
    log_event(conn, "NVDA", "approved")
    row = conn.execute("SELECT timestamp FROM trade_log").fetchone()
    assert row["timestamp"] is not None
    assert len(row["timestamp"]) > 10  # ISO 8601 string


def test_log_multiple_events(conn):
    log_event(conn, "NVDA", "signal")
    log_event(conn, "NVDA", "approved")
    log_event(conn, "NVDA", "bought", {"shares": 10, "price": 500.0})
    count = conn.execute("SELECT COUNT(*) FROM trade_log").fetchone()[0]
    assert count == 3


# ---------------------------------------------------------------------------
# get_weekly_summary
# ---------------------------------------------------------------------------

def _close_position(conn, *, symbol: str, pnl: float, exit_date: date) -> None:
    """Insert a position and immediately mark it closed with the given P&L."""
    db_id = save_position(conn, _make_position(symbol=symbol))
    update_position(
        conn, db_id,
        status="closed",
        exit_date=exit_date.isoformat(),
        exit_price=510.0,
        exit_reason="tp",
        pnl_dollars=pnl,
        pnl_pct=pnl / 10,
    )


def test_weekly_summary_empty_db(conn):
    s = get_weekly_summary(conn, date.today() - timedelta(days=7))
    assert s.trades_closed == 0
    assert s.wins == 0 and s.losses == 0
    assert s.net_pnl_dollars == 0.0
    assert s.open_symbols == []
    assert s.signals == 0
    assert s.period_end == date.today()


def test_weekly_summary_counts_wins_losses_and_net(conn):
    today = date.today()
    _close_position(conn, symbol="NVDA", pnl=120.0, exit_date=today)
    _close_position(conn, symbol="AMD",  pnl=-40.0, exit_date=today - timedelta(days=2))
    s = get_weekly_summary(conn, today - timedelta(days=7))
    assert s.trades_closed == 2
    assert s.wins == 1
    assert s.losses == 1
    assert s.net_pnl_dollars == pytest.approx(80.0)


def test_weekly_summary_excludes_trades_before_window(conn):
    today = date.today()
    _close_position(conn, symbol="NVDA", pnl=120.0, exit_date=today)
    _close_position(conn, symbol="AMD",  pnl=999.0, exit_date=today - timedelta(days=30))
    s = get_weekly_summary(conn, today - timedelta(days=7))
    assert s.trades_closed == 1
    assert s.net_pnl_dollars == pytest.approx(120.0)


def test_weekly_summary_reports_open_symbols(conn):
    save_position(conn, _make_position(symbol="NVDA"))
    save_position(conn, _make_position(symbol="TSM"))
    s = get_weekly_summary(conn, date.today() - timedelta(days=7))
    assert set(s.open_symbols) == {"NVDA", "TSM"}
    assert s.trades_closed == 0


def test_weekly_summary_counts_signals_in_window_only(conn):
    log_event(conn, "NVDA", "signal")
    log_event(conn, "AMD", "signal")
    log_event(conn, "AMD", "approved")  # not a signal — must not count
    s = get_weekly_summary(conn, date.today() - timedelta(days=7))
    assert s.signals == 2
