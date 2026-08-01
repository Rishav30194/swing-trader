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
    get_regime_states,
    get_strategy_cash,
    get_weekly_summary,
    log_event,
    log_rebalance_order,
    set_regime_state,
    set_strategy_cash,
)


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def conn():
    """Fresh in-memory database for each test."""
    c = init_db(":memory:")
    yield c
    c.close()


# ---------------------------------------------------------------------------
# init_db
# ---------------------------------------------------------------------------

def test_init_db_creates_tables(conn):
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "sleeves" in tables
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

def test_weekly_summary_empty_db(conn):
    s = get_weekly_summary(conn, date.today() - timedelta(days=7))
    assert s.sleeves_on == [] and s.sleeves_off == []
    assert s.orders_filled == 0 and s.orders_failed == 0
    assert s.notional_bought == 0.0 and s.notional_sold == 0.0
    assert s.period_end == date.today()


def test_weekly_summary_splits_sleeves_by_regime(conn):
    set_regime_state(conn, "NVDA", True)
    set_regime_state(conn, "MSFT", True)
    set_regime_state(conn, "ASML", False)
    s = get_weekly_summary(conn, date.today() - timedelta(days=7))
    assert s.sleeves_on == ["MSFT", "NVDA"]
    assert s.sleeves_off == ["ASML"]


def test_weekly_summary_counts_filled_and_failed(conn):
    log_rebalance_order(conn, "NVDA", "buy", 125.0, "regime_entry", "filled")
    log_rebalance_order(conn, "AMD", "sell", 60.0, "regime_exit", "filled")
    log_rebalance_order(conn, "TSM", "buy", 90.0, "regime_entry", "failed")
    log_rebalance_order(conn, "VOO", "buy", 40.0, "drift", "skipped")
    s = get_weekly_summary(conn, date.today() - timedelta(days=7))
    assert s.orders_filled == 2
    assert s.orders_failed == 1


def test_weekly_summary_sums_notional_by_side_for_fills_only(conn):
    log_rebalance_order(conn, "NVDA", "buy", 125.0, "regime_entry", "filled")
    log_rebalance_order(conn, "MSFT", "buy", 75.0, "drift", "filled")
    log_rebalance_order(conn, "AMD", "sell", 60.0, "regime_exit", "filled")
    log_rebalance_order(conn, "TSM", "buy", 999.0, "regime_entry", "failed")
    s = get_weekly_summary(conn, date.today() - timedelta(days=7))
    assert s.notional_bought == pytest.approx(200.0)
    assert s.notional_sold == pytest.approx(60.0)


def test_weekly_summary_excludes_orders_before_window(conn):
    log_rebalance_order(conn, "NVDA", "buy", 125.0, "regime_entry", "filled")
    conn.execute(
        "UPDATE rebalance_log SET timestamp = ? WHERE symbol = 'NVDA'",
        ((date.today() - timedelta(days=30)).isoformat(),),
    )
    conn.commit()
    log_rebalance_order(conn, "MSFT", "buy", 75.0, "regime_entry", "filled")
    s = get_weekly_summary(conn, date.today() - timedelta(days=7))
    assert s.orders_filled == 1
    assert s.notional_bought == pytest.approx(75.0)


# ---------------------------------------------------------------------------
# Sleeve regime state
# ---------------------------------------------------------------------------

def test_get_regime_states_empty_by_default(conn):
    assert get_regime_states(conn) == {}


def test_set_and_get_regime_state(conn):
    set_regime_state(conn, "NVDA", True, last_close=204.12, last_sma_200=203.5)
    assert get_regime_states(conn) == {"NVDA": True}


def test_set_regime_state_upserts_rather_than_duplicating(conn):
    set_regime_state(conn, "NVDA", True)
    set_regime_state(conn, "NVDA", False)
    states = get_regime_states(conn)
    assert states == {"NVDA": False}
    assert conn.execute("SELECT COUNT(*) FROM sleeves").fetchone()[0] == 1


def test_regime_state_persists_decision_inputs(conn):
    set_regime_state(conn, "NVDA", True, last_close=204.12, last_sma_200=203.5)
    row = conn.execute("SELECT * FROM sleeves WHERE symbol = 'NVDA'").fetchone()
    assert row["last_close"] == pytest.approx(204.12)
    assert row["last_sma_200"] == pytest.approx(203.5)


# ---------------------------------------------------------------------------
# rebalance_log
# ---------------------------------------------------------------------------

def test_log_rebalance_order_writes_row(conn):
    log_rebalance_order(conn, "NVDA", "buy", 125.0, "regime_entry", "filled",
                        order_id="abc-123", detail={"filled_avg_price": 204.0})
    row = conn.execute("SELECT * FROM rebalance_log").fetchone()
    assert row["symbol"] == "NVDA"
    assert row["side"] == "buy"
    assert row["notional"] == pytest.approx(125.0)
    assert row["status"] == "filled"
    assert row["order_id"] == "abc-123"


def test_log_rebalance_order_serialises_detail_as_json(conn):
    log_rebalance_order(conn, "NVDA", "buy", 1.0, "drift", "failed",
                        detail={"error": "rejected"})
    row = conn.execute("SELECT detail FROM rebalance_log").fetchone()
    assert json.loads(row["detail"]) == {"error": "rejected"}


def test_log_rebalance_order_allows_null_detail(conn):
    log_rebalance_order(conn, "NVDA", "sell", 1.0, "regime_exit", "skipped")
    assert conn.execute("SELECT detail FROM rebalance_log").fetchone()["detail"] is None


# ---------------------------------------------------------------------------
# Strategy cash ledger
# ---------------------------------------------------------------------------

def test_strategy_cash_seeds_from_the_allocation(conn):
    assert get_strategy_cash(conn, 1_000.0) == pytest.approx(1_000.0)


def test_strategy_cash_persists_the_seed(conn):
    get_strategy_cash(conn, 1_000.0)
    assert get_strategy_cash(conn, 999_999.0) == pytest.approx(1_000.0)


def test_set_strategy_cash_round_trips(conn):
    set_strategy_cash(conn, 875.0)
    assert get_strategy_cash(conn, 1_000.0) == pytest.approx(875.0)


def test_set_strategy_cash_upserts(conn):
    set_strategy_cash(conn, 100.0)
    set_strategy_cash(conn, 200.0)
    assert conn.execute("SELECT COUNT(*) FROM strategy_state").fetchone()[0] == 1


def test_strategy_cash_never_stored_negative(conn):
    set_strategy_cash(conn, -50.0)
    assert get_strategy_cash(conn, 1_000.0) == pytest.approx(0.0)
