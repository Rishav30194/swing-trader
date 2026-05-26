"""
test_executor.py — Unit tests for src/executor.py.

All Alpaca TradingClient calls are mocked — no real API calls are made.

Coverage:
  - get_account_equity returns the float from account.equity.
  - place_buy_order submits the correct side/qty and returns a filled dict.
  - place_sell_order includes the reason in the returned dict.
  - Paper guard raises when ALPACA_PAPER env var changed since startup.
  - _wait_for_fill returns immediately on first filled poll.
  - _wait_for_fill returns last known state on timeout.
  - _wait_for_fill handles multiple polls before fill.
  - Submission failure propagates the exception to the caller.
"""

import os
from unittest.mock import MagicMock, call, patch

import pytest

import src.executor as executor
from src.executor import (
    _order_to_dict,
    _wait_for_fill,
    get_account_equity,
    place_buy_order,
    place_sell_order,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _mock_order(
    order_id:          str   = "order-abc-123",
    symbol:            str   = "NVDA",
    qty:               str   = "10",
    filled_qty:        str   = "10",
    filled_avg_price:  str   = "134.50",
    status:            str   = "filled",
    side:              str   = "buy",
) -> MagicMock:
    order                    = MagicMock()
    order.id                 = order_id
    order.symbol             = symbol
    order.qty                = qty
    order.filled_qty         = filled_qty
    order.filled_avg_price   = filled_avg_price
    order.status             = MagicMock(value=status)
    order.side               = MagicMock(value=side)
    order.created_at         = "2025-01-15T10:30:00Z"
    return order


def _mock_account(equity: str = "100000.00") -> MagicMock:
    account        = MagicMock()
    account.equity = equity
    return account


# ---------------------------------------------------------------------------
# _order_to_dict
# ---------------------------------------------------------------------------

class TestOrderToDict:
    def test_converts_numeric_fields(self):
        result = _order_to_dict(_mock_order(qty="10", filled_qty="10", filled_avg_price="134.50"))
        assert result["qty"]              == pytest.approx(10.0)
        assert result["filled_qty"]       == pytest.approx(10.0)
        assert result["filled_avg_price"] == 134.50

    def test_converts_string_fields(self):
        result = _order_to_dict(_mock_order(order_id="abc", symbol="NVDA"))
        assert result["id"]     == "abc"
        assert result["symbol"] == "NVDA"

    def test_status_and_side_extracted_from_enum_value(self):
        result = _order_to_dict(_mock_order(status="filled", side="buy"))
        assert result["status"] == "filled"
        assert result["side"]   == "buy"

    def test_handles_none_filled_avg_price(self):
        result = _order_to_dict(_mock_order(filled_avg_price=None))
        assert result["filled_avg_price"] == 0.0

    def test_handles_status_without_value_attr(self):
        order        = _mock_order()
        order.status = "filled"   # plain string, not enum
        result       = _order_to_dict(order)
        assert result["status"] == "filled"


# ---------------------------------------------------------------------------
# get_account_equity
# ---------------------------------------------------------------------------

class TestGetAccountEquity:
    def test_returns_float_equity(self):
        with patch.object(executor, "_client") as mock_client:
            mock_client.get_account.return_value = _mock_account("98765.43")
            assert get_account_equity() == pytest.approx(98765.43)

    def test_calls_get_account_once(self):
        with patch.object(executor, "_client") as mock_client:
            mock_client.get_account.return_value = _mock_account()
            get_account_equity()
        mock_client.get_account.assert_called_once()

    def test_raises_on_api_failure(self):
        with patch.object(executor, "_client") as mock_client:
            mock_client.get_account.side_effect = ConnectionError("API down")
            with pytest.raises(ConnectionError):
                get_account_equity()


# ---------------------------------------------------------------------------
# _wait_for_fill
# ---------------------------------------------------------------------------

class TestWaitForFill:
    def test_returns_immediately_when_already_filled(self):
        filled = _mock_order(status="filled", filled_avg_price="134.50")
        with patch.object(executor, "_client") as mock_client:
            mock_client.get_order_by_id.return_value = filled
            with patch("time.sleep") as mock_sleep:
                result = _wait_for_fill("order-1")
        assert result["filled_avg_price"] == 134.50
        mock_sleep.assert_not_called()

    def test_polls_until_filled(self):
        pending = _mock_order(status="new",    filled_qty="0",  filled_avg_price=None)
        filled  = _mock_order(status="filled", filled_qty="10", filled_avg_price="134.50")
        with patch.object(executor, "_client") as mock_client:
            mock_client.get_order_by_id.side_effect = [pending, pending, filled]
            with patch("time.sleep"):
                result = _wait_for_fill("order-1")
        assert result["status"] == "filled"
        assert mock_client.get_order_by_id.call_count == 3

    def test_returns_last_known_state_on_timeout(self):
        pending = _mock_order(status="new", filled_qty="0", filled_avg_price=None)
        with patch.object(executor, "_client") as mock_client:
            mock_client.get_order_by_id.return_value = pending
            with patch("time.sleep"):
                # Force immediate timeout: first monotonic sets deadline in past
                with patch("time.monotonic", side_effect=[0.0, 999.0, 999.0]):
                    result = _wait_for_fill("order-1")
        assert result["status"] == "new"

    def test_accepts_partially_filled_status(self):
        partial = _mock_order(status="partially_filled", filled_qty="5")
        with patch.object(executor, "_client") as mock_client:
            mock_client.get_order_by_id.return_value = partial
            with patch("time.sleep"):
                result = _wait_for_fill("order-1")
        assert result["status"] == "partially_filled"


# ---------------------------------------------------------------------------
# place_buy_order
# ---------------------------------------------------------------------------

class TestPlaceBuyOrder:
    def _setup(self, mock_client, status: str = "filled") -> MagicMock:
        submitted = _mock_order(order_id="buy-1", status="new")
        filled    = _mock_order(order_id="buy-1", status=status, filled_avg_price="134.50")
        mock_client.submit_order.return_value       = submitted
        mock_client.get_order_by_id.return_value    = filled
        return filled

    def test_returns_filled_dict(self):
        with patch.object(executor, "_client") as mock_client:
            self._setup(mock_client)
            with patch("time.sleep"):
                result = place_buy_order("NVDA", 1340.0)  # $1340 notional
        assert result["filled_avg_price"] == 134.50
        assert result["status"]           == "filled"

    def test_submits_notional_market_buy_order(self):
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest
        with patch.object(executor, "_client") as mock_client:
            self._setup(mock_client)
            with patch("time.sleep"):
                place_buy_order("NVDA", 1340.0)
        submitted_request = mock_client.submit_order.call_args.args[0]
        assert isinstance(submitted_request, MarketOrderRequest)
        assert submitted_request.symbol           == "NVDA"
        assert submitted_request.notional         == pytest.approx(1340.0)
        assert submitted_request.side             == OrderSide.BUY
        assert submitted_request.time_in_force    == TimeInForce.DAY

    def test_raises_on_submission_failure(self):
        with patch.object(executor, "_client") as mock_client:
            mock_client.submit_order.side_effect = Exception("Rejected")
            with pytest.raises(Exception, match="Rejected"):
                place_buy_order("NVDA", 1340.0)

    def test_does_not_include_reason_key(self):
        with patch.object(executor, "_client") as mock_client:
            self._setup(mock_client)
            with patch("time.sleep"):
                result = place_buy_order("NVDA", 1340.0)
        assert "reason" not in result


# ---------------------------------------------------------------------------
# place_sell_order
# ---------------------------------------------------------------------------

class TestPlaceSellOrder:
    def _setup(self, mock_client) -> None:
        submitted = _mock_order(order_id="sell-1", status="new",    side="sell")
        filled    = _mock_order(order_id="sell-1", status="filled", side="sell",
                                filled_avg_price="130.00")
        mock_client.submit_order.return_value    = submitted
        mock_client.get_order_by_id.return_value = filled

    def test_returns_filled_dict_with_reason(self):
        with patch.object(executor, "_client") as mock_client:
            self._setup(mock_client)
            with patch("time.sleep"):
                result = place_sell_order("NVDA", 10, "sl")
        assert result["filled_avg_price"] == 130.00
        assert result["reason"]           == "sl"

    def test_submits_market_sell_order(self):
        from alpaca.trading.enums import OrderSide
        with patch.object(executor, "_client") as mock_client:
            self._setup(mock_client)
            with patch("time.sleep"):
                place_sell_order("NVDA", 10, "tp")
        submitted_request = mock_client.submit_order.call_args.args[0]
        assert submitted_request.side == OrderSide.SELL

    def test_reason_preserved_for_all_exit_types(self):
        for reason in ("sl", "trailing", "tp", "day5", "manual"):
            with patch.object(executor, "_client") as mock_client:
                self._setup(mock_client)
                with patch("time.sleep"):
                    result = place_sell_order("NVDA", 10, reason)
            assert result["reason"] == reason

    def test_raises_on_submission_failure(self):
        with patch.object(executor, "_client") as mock_client:
            mock_client.submit_order.side_effect = Exception("Insufficient qty")
            with pytest.raises(Exception, match="Insufficient qty"):
                place_sell_order("NVDA", 10, "sl")


# ---------------------------------------------------------------------------
# Paper guard
# ---------------------------------------------------------------------------

class TestPaperGuard:
    def test_raises_when_env_var_changed_since_startup(self):
        # executor.settings.alpaca_paper is True (from .env); patch env to say false
        flipped = "false" if executor.settings.alpaca_paper else "true"
        with patch.dict(os.environ, {"ALPACA_PAPER": flipped}):
            with patch.object(executor, "_client"):
                with pytest.raises(RuntimeError, match="changed since startup"):
                    place_buy_order("NVDA", 1340.0)

    def test_does_not_raise_when_env_matches_startup(self):
        current = "true" if executor.settings.alpaca_paper else "false"
        filled  = _mock_order(status="filled")
        with patch.dict(os.environ, {"ALPACA_PAPER": current}):
            with patch.object(executor, "_client") as mock_client:
                mock_client.submit_order.return_value    = _mock_order(status="new")
                mock_client.get_order_by_id.return_value = filled
                with patch("time.sleep"):
                    result = place_buy_order("NVDA", 1340.0)
        assert result["status"] == "filled"
