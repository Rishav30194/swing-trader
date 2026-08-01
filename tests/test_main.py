"""
test_main.py — Unit tests for the rebalance orchestration in main.py.

This module moves money, so the tests focus on the ways that can go wrong
quietly rather than on the happy path:

  - an order that submits without raising but never fills
  - a partial fill
  - Telegram being unreachable (reductions must still execute — hard rule 3)
  - a symbol whose data cannot be fetched (sleeve must be left alone, never sold)
  - the approval gate defaulting to "skip increases" on NO or timeout

No network calls: Alpaca, Telegram, and the database are all mocked.
"""

from dataclasses import replace
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

import main
from src.portfolio import RebalanceOrder, RegimeState


def _settings(**overrides):
    """Patch main.settings — Settings is frozen, so swap the whole instance."""
    return patch.object(main, "settings", replace(main.settings, **overrides))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _order(**overrides) -> RebalanceOrder:
    defaults = dict(symbol="NVDA", side="buy", notional=125.0,
                    reason="regime_entry", increases_exposure=True)
    return RebalanceOrder(**{**defaults, **overrides})


def _sell(**overrides) -> RebalanceOrder:
    return _order(**{"side": "sell", "reason": "regime_exit",
                     "increases_exposure": False, **overrides})


def _resp(status="filled", qty=1.0, price=125.0, oid="o1") -> dict:
    return {"id": oid, "status": status, "filled_qty": qty,
            "filled_avg_price": price, "symbol": "NVDA"}


def _bars(n=250, close=204.0) -> pd.DataFrame:
    return pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=n, freq="D", tz="UTC"),
        "open": np.full(n, close), "high": np.full(n, close),
        "low": np.full(n, close), "close": np.full(n, close),
        "volume": np.full(n, 1_000.0),
    })


# ---------------------------------------------------------------------------
# _classify_fill — the bug that recorded unfilled orders as filled
# ---------------------------------------------------------------------------

class TestClassifyFill:
    def test_filled(self):
        assert main._classify_fill({"status": "filled"}) == "filled"

    def test_partially_filled(self):
        assert main._classify_fill({"status": "partially_filled"}) == "partial"

    @pytest.mark.parametrize("status", [
        "new", "accepted", "pending_new", "rejected", "canceled", "expired", ""
    ])
    def test_anything_else_is_failed(self, status):
        assert main._classify_fill({"status": status}) == "failed"

    def test_missing_status_is_failed(self):
        assert main._classify_fill({}) == "failed"

    def test_status_is_case_insensitive(self):
        assert main._classify_fill({"status": "FILLED"}) == "filled"


class TestActualNotional:
    def test_uses_filled_quantity_and_price(self):
        assert main._actual_notional(_resp(qty=2.0, price=100.0), 999.0) == pytest.approx(200.0)

    def test_falls_back_when_nothing_filled(self):
        assert main._actual_notional(_resp(qty=0.0, price=0.0), 125.0) == pytest.approx(125.0)

    def test_falls_back_on_missing_fields(self):
        assert main._actual_notional({}, 125.0) == pytest.approx(125.0)

    def test_handles_none_values(self):
        resp = {"filled_qty": None, "filled_avg_price": None}
        assert main._actual_notional(resp, 125.0) == pytest.approx(125.0)


# ---------------------------------------------------------------------------
# _execute_order
# ---------------------------------------------------------------------------

class TestExecuteOrder:
    def test_buy_uses_notional(self):
        with patch.object(main, "place_buy_order", return_value=_resp()) as buy, \
             patch.object(main, "log_rebalance_order"):
            result = main._execute_order(MagicMock(), _order(notional=125.0), {})
        buy.assert_called_once_with("NVDA", 125.0)
        assert result["status"] == "filled"

    def test_regime_exit_sells_exact_share_count(self):
        """A full exit must sell shares, not dollars, or dust is left behind."""
        holdings = {"NVDA": {"shares": 1.2345, "notional": 250.0}}
        with patch.object(main, "place_sell_order", return_value=_resp()) as sell, \
             patch.object(main, "log_rebalance_order"):
            main._execute_order(MagicMock(), _sell(), holdings)
        sell.assert_called_once_with("NVDA", 1.2345, "regime_exit")

    def test_drift_trim_sells_notional(self):
        order = _order(side="sell", reason="drift", increases_exposure=False, notional=40.0)
        with patch.object(main, "place_sell_notional", return_value=_resp()) as sell, \
             patch.object(main, "log_rebalance_order"):
            main._execute_order(MagicMock(), order, {})
        sell.assert_called_once_with("NVDA", 40.0, "drift")

    def test_unfilled_order_is_recorded_as_failed(self):
        """Submitting without raising is not the same as filling."""
        with patch.object(main, "place_buy_order", return_value=_resp("new", qty=0.0)), \
             patch.object(main, "log_rebalance_order") as log:
            result = main._execute_order(MagicMock(), _order(), {})
        assert result["status"] == "failed"
        assert log.call_args[0][5] == "failed"

    def test_rejected_order_is_recorded_as_failed(self):
        with patch.object(main, "place_buy_order", return_value=_resp("rejected", qty=0.0)), \
             patch.object(main, "log_rebalance_order"):
            assert main._execute_order(MagicMock(), _order(), {})["status"] == "failed"

    def test_partial_fill_is_recorded_as_partial(self):
        with patch.object(main, "place_buy_order",
                          return_value=_resp("partially_filled", qty=0.5, price=100.0)), \
             patch.object(main, "log_rebalance_order") as log:
            result = main._execute_order(MagicMock(), _order(notional=125.0), {})
        assert result["status"] == "partial"
        assert result["notional"] == pytest.approx(50.0)   # actual, not requested
        assert log.call_args[0][5] == "partial"

    def test_exception_is_caught_and_logged_as_failed(self):
        with patch.object(main, "place_buy_order", side_effect=RuntimeError("api down")), \
             patch.object(main, "log_rebalance_order") as log:
            result = main._execute_order(MagicMock(), _order(), {})
        assert result["status"] == "failed"
        assert log.call_args[0][5] == "failed"

    def test_exception_does_not_propagate(self):
        """One bad symbol must not abort the whole rebalance (hard rule 7)."""
        with patch.object(main, "place_buy_order", side_effect=RuntimeError("boom")), \
             patch.object(main, "log_rebalance_order"):
            main._execute_order(MagicMock(), _order(), {})   # must not raise

    def test_records_actual_filled_notional_not_requested(self):
        with patch.object(main, "place_buy_order", return_value=_resp(qty=1.0, price=130.0)), \
             patch.object(main, "log_rebalance_order") as log:
            result = main._execute_order(MagicMock(), _order(notional=125.0), {})
        assert result["notional"] == pytest.approx(130.0)
        assert log.call_args[0][3] == pytest.approx(130.0)


# ---------------------------------------------------------------------------
# _execute_plan — hard rule 3
# ---------------------------------------------------------------------------

class TestExecutePlan:
    def test_reductions_execute_without_approval(self):
        with patch.object(main, "_execute_order",
                          return_value={"symbol": "NVDA", "side": "sell",
                                        "notional": 1.0, "status": "filled"}) as ex:
            results = main._execute_plan(MagicMock(), [_sell()], {}, approved=False)
        ex.assert_called_once()
        assert results[0]["status"] == "filled"

    def test_increases_are_skipped_without_approval(self):
        with patch.object(main, "_execute_order") as ex, \
             patch.object(main, "log_rebalance_order") as log:
            results = main._execute_plan(MagicMock(), [_order()], {}, approved=False)
        ex.assert_not_called()
        assert results[0]["status"] == "skipped"
        assert log.call_args[0][5] == "skipped"

    def test_increases_execute_when_approved(self):
        with patch.object(main, "_execute_order",
                          return_value={"symbol": "NVDA", "side": "buy",
                                        "notional": 1.0, "status": "filled"}) as ex:
            main._execute_plan(MagicMock(), [_order()], {}, approved=True)
        ex.assert_called_once()

    def test_mixed_plan_runs_sells_and_skips_buys(self):
        orders = [_sell(symbol="AMD"), _order(symbol="NVDA")]
        with patch.object(main, "_execute_order",
                          return_value={"symbol": "AMD", "side": "sell",
                                        "notional": 1.0, "status": "filled"}) as ex, \
             patch.object(main, "log_rebalance_order"):
            results = main._execute_plan(MagicMock(), orders, {}, approved=False)
        assert ex.call_count == 1
        assert [r["status"] for r in results] == ["filled", "skipped"]


# ---------------------------------------------------------------------------
# _await_approval
# ---------------------------------------------------------------------------

class TestAwaitApproval:
    def test_no_increases_does_not_wait_at_all(self):
        with patch.object(main, "listen_for_reply") as listen:
            assert main._await_approval([_sell()]) is False
        listen.assert_not_called()

    def test_yes_approves(self):
        with patch.object(main, "listen_for_reply", return_value=True):
            assert main._await_approval([_order()]) is True

    def test_no_rejects(self):
        with patch.object(main, "listen_for_reply", return_value=False):
            assert main._await_approval([_order()]) is False

    def test_timeout_rejects(self):
        with patch.object(main, "listen_for_reply", return_value=None):
            assert main._await_approval([_order()]) is False


# ---------------------------------------------------------------------------
# _evaluate_regimes
# ---------------------------------------------------------------------------

class TestEvaluateRegimes:
    def _patches(self, bars_side_effect, symbols=("NVDA", "AMD")):
        return (
            _settings(symbols=symbols),
            patch.object(main, "get_regime_states", return_value={}),
            patch.object(main, "get_historical_bars", side_effect=bars_side_effect),
            patch.object(main, "set_regime_state"),
        )

    def test_symbol_with_failed_fetch_is_omitted(self):
        """An unpriceable sleeve must be left alone, never assigned a weight."""
        def fetch(symbol, **kwargs):
            if symbol == "AMD":
                raise RuntimeError("data outage")
            return _bars()

        p1, p2, p3, p4 = self._patches(fetch)
        with p1, p2, p3, p4:
            states = main._evaluate_regimes(MagicMock())
        assert "AMD" not in states
        assert "NVDA" in states

    def test_symbol_with_insufficient_history_is_omitted(self):
        p1, p2, p3, p4 = self._patches(lambda symbol, **kw: _bars(n=50))
        with p1, p2, p3, p4:
            states = main._evaluate_regimes(MagicMock())
        assert states == {}

    def test_state_is_persisted_for_each_evaluated_symbol(self):
        p1, p2, p3, p4 = self._patches(lambda symbol, **kw: _bars())
        with p1, p2, p3, p4 as set_state:
            main._evaluate_regimes(MagicMock())
        assert set_state.call_count == 2

    def test_requests_completed_bars_only(self):
        """Deciding on a still-forming bar is the defect that broke the old strategy."""
        p1, p2, p3, p4 = self._patches(lambda symbol, **kw: _bars())
        with p1, p2, p3 as fetch, p4:
            main._evaluate_regimes(MagicMock())
        assert fetch.call_args.kwargs["completed_only"] is True


# ---------------------------------------------------------------------------
# _run_rebalance — end to end with everything mocked
# ---------------------------------------------------------------------------

class TestRunRebalance:
    def _base(self, states=None, orders=None):
        states = states if states is not None else {"NVDA": RegimeState(on=True)}
        return {
            "get_account_equity": patch.object(main, "get_account_equity", return_value=1000.0),
            "get_current_holdings": patch.object(main, "get_current_holdings", return_value={}),
            "_evaluate_regimes": patch.object(main, "_evaluate_regimes", return_value=states),
            "log_event": patch.object(main, "log_event"),
            "send_rebalance_plan": patch.object(main, "send_rebalance_plan", return_value=True),
            "send_rebalance_result": patch.object(main, "send_rebalance_result"),
            "send_error_alert": patch.object(main, "send_error_alert"),
        }

    def test_aborts_when_equity_fetch_fails(self):
        with patch.object(main, "get_account_equity", side_effect=RuntimeError("down")), \
             patch.object(main, "get_current_holdings"), \
             patch.object(main, "_evaluate_regimes") as regimes, \
             patch.object(main, "send_error_alert") as alert:
            main._run_rebalance(MagicMock())
        regimes.assert_not_called()
        alert.assert_called_once()

    def test_aborts_when_holdings_fetch_fails(self):
        """Acting on an unknown portfolio state is worse than doing nothing."""
        with patch.object(main, "get_account_equity", return_value=1000.0), \
             patch.object(main, "get_current_holdings", side_effect=RuntimeError("down")), \
             patch.object(main, "_evaluate_regimes") as regimes, \
             patch.object(main, "send_error_alert") as alert:
            main._run_rebalance(MagicMock())
        regimes.assert_not_called()
        alert.assert_called_once()

    def test_aborts_when_no_symbol_could_be_evaluated(self):
        p = self._base(states={})
        with p["get_account_equity"], p["get_current_holdings"], p["_evaluate_regimes"], \
             p["send_error_alert"] as alert, \
             patch.object(main, "send_rebalance_plan") as plan:
            main._run_rebalance(MagicMock())
        plan.assert_not_called()
        alert.assert_called_once()

    def test_sends_plan_and_stops_when_no_orders_needed(self):
        # already at target: 1/8 of 1000 = 125 held
        p = self._base()
        with p["get_account_equity"], \
             patch.object(main, "get_current_holdings",
                          return_value={"NVDA": {"shares": 1.0, "notional": 125.0}}), \
             p["_evaluate_regimes"], p["log_event"], \
             p["send_rebalance_plan"] as plan, \
             patch.object(main, "send_rebalance_result") as result, \
             _settings(symbols=("NVDA",) * 8):
            main._run_rebalance(MagicMock())
        plan.assert_called_once()
        result.assert_not_called()

    def test_reductions_execute_even_when_telegram_send_fails(self):
        """Hard rule 3 — de-risking must never depend on Telegram."""
        states = {"NVDA": RegimeState(on=False)}
        p = self._base(states=states)
        with p["get_account_equity"], \
             patch.object(main, "get_current_holdings",
                          return_value={"NVDA": {"shares": 1.0, "notional": 250.0}}), \
             p["_evaluate_regimes"], p["log_event"], \
             patch.object(main, "send_rebalance_plan", return_value=False), \
             patch.object(main, "send_rebalance_result"), \
             patch.object(main, "send_error_alert"), \
             patch.object(main, "_execute_order",
                          return_value={"symbol": "NVDA", "side": "sell",
                                        "notional": 250.0, "status": "filled"}) as ex, \
             _settings(symbols=("NVDA",)):
            main._run_rebalance(MagicMock())
        ex.assert_called_once()
        assert ex.call_args[0][1].side == "sell"

    def test_alerts_when_an_order_fails(self):
        states = {"NVDA": RegimeState(on=False)}
        p = self._base(states=states)
        with p["get_account_equity"], \
             patch.object(main, "get_current_holdings",
                          return_value={"NVDA": {"shares": 1.0, "notional": 250.0}}), \
             p["_evaluate_regimes"], p["log_event"], p["send_rebalance_plan"], \
             patch.object(main, "send_rebalance_result"), \
             patch.object(main, "send_error_alert") as alert, \
             patch.object(main, "_execute_order",
                          return_value={"symbol": "NVDA", "side": "sell",
                                        "notional": 250.0, "status": "failed"}), \
             _settings(symbols=("NVDA",)):
            main._run_rebalance(MagicMock())
        alert.assert_called_once()

    def test_alerts_on_partial_fill(self):
        states = {"NVDA": RegimeState(on=False)}
        p = self._base(states=states)
        with p["get_account_equity"], \
             patch.object(main, "get_current_holdings",
                          return_value={"NVDA": {"shares": 1.0, "notional": 250.0}}), \
             p["_evaluate_regimes"], p["log_event"], p["send_rebalance_plan"], \
             patch.object(main, "send_rebalance_result"), \
             patch.object(main, "send_error_alert") as alert, \
             patch.object(main, "_execute_order",
                          return_value={"symbol": "NVDA", "side": "sell",
                                        "notional": 100.0, "status": "partial"}), \
             _settings(symbols=("NVDA",)):
            main._run_rebalance(MagicMock())
        alert.assert_called_once()

    def test_validation_failure_alerts_instead_of_escaping(self):
        """
        A hard-rule violation must reach the user. If it escapes into APScheduler
        it is logged and swallowed, and the rebalance silently stops happening.
        """
        p = self._base()
        with p["get_account_equity"], p["get_current_holdings"], p["_evaluate_regimes"], \
             patch.object(main, "validate_target_weights",
                          side_effect=ValueError("hard rule 5 violated")), \
             patch.object(main, "send_error_alert") as alert, \
             patch.object(main, "_execute_plan") as execute:
            main._run_rebalance(MagicMock())     # must not raise
        alert.assert_called_once()
        execute.assert_not_called()

    def test_no_orders_are_placed_when_sizing_fails(self):
        p = self._base()
        with p["get_account_equity"], p["get_current_holdings"], p["_evaluate_regimes"], \
             patch.object(main, "diff_to_orders", side_effect=ValueError("bad equity")), \
             patch.object(main, "send_error_alert"), \
             patch.object(main, "_execute_order") as ex:
            main._run_rebalance(MagicMock())
        ex.assert_not_called()

    def test_plan_is_json_serialisable_for_the_event_log(self):
        """log_event JSON-encodes the plan; a non-serialisable order would crash it."""
        import json
        states = {"NVDA": RegimeState(on=True)}
        p = self._base(states=states)
        captured = {}
        with p["get_account_equity"], p["get_current_holdings"], p["_evaluate_regimes"], \
             patch.object(main, "log_event",
                          side_effect=lambda c, s, e, d: captured.update(d)), \
             p["send_rebalance_plan"], patch.object(main, "send_rebalance_result"), \
             patch.object(main, "_await_approval", return_value=False), \
             patch.object(main, "_execute_plan", return_value=[]), \
             _settings(symbols=("NVDA",)):
            main._run_rebalance(MagicMock())
        json.dumps(captured)   # must not raise


# ---------------------------------------------------------------------------
# _warn_on_unmanaged_holdings
# ---------------------------------------------------------------------------

class TestUnmanagedHoldings:
    def test_silent_when_every_holding_is_managed(self):
        holdings = {"NVDA": {"shares": 1.0, "notional": 125.0}}
        with _settings(symbols=("NVDA", "AMD")), \
             patch.object(main, "send_error_alert") as alert:
            main._warn_on_unmanaged_holdings(holdings)
        alert.assert_not_called()

    def test_alerts_on_a_position_outside_the_universe(self):
        """Sizing uses total equity, so a foreign holding inflates every sleeve."""
        holdings = {"NVDA": {"shares": 1.0, "notional": 125.0},
                    "TSLA": {"shares": 10.0, "notional": 4000.0}}
        with _settings(symbols=("NVDA",)), \
             patch.object(main, "send_error_alert") as alert:
            main._warn_on_unmanaged_holdings(holdings)
        alert.assert_called_once()
        assert "TSLA" in alert.call_args[0][0]

    def test_silent_on_an_empty_account(self):
        with _settings(symbols=("NVDA",)), \
             patch.object(main, "send_error_alert") as alert:
            main._warn_on_unmanaged_holdings({})
        alert.assert_not_called()

    def test_unmanaged_positions_are_never_sold(self):
        """The rebalancer only ever acts on symbols in the configured universe."""
        from src.portfolio import compute_target_weights, diff_to_orders
        weights = compute_target_weights(
            {"NVDA": RegimeState(on=True)}, universe_size=1, max_position_pct=1.0)
        orders = diff_to_orders(
            {"NVDA": 0.0, "TSLA": 4000.0}, weights, 5000.0)
        assert all(o.symbol != "TSLA" for o in orders)


# ---------------------------------------------------------------------------
# _guarded — scheduler jobs must never die silently
# ---------------------------------------------------------------------------

class TestGuarded:
    def test_passes_through_on_success(self):
        calls = []
        main._guarded(lambda: calls.append(1), "job")()
        assert calls == [1]

    def test_swallows_and_alerts_on_failure(self):
        def boom():
            raise RuntimeError("scheduler job exploded")
        with patch.object(main, "send_error_alert") as alert:
            main._guarded(boom, "Weekly rebalance")()   # must not raise
        alert.assert_called_once()
        assert "Weekly rebalance" in alert.call_args[0][0]

    def test_alert_failure_does_not_re_raise(self):
        """If Telegram is also down, the scheduler still must not crash."""
        def boom():
            raise RuntimeError("boom")
        with patch.object(main, "send_error_alert", side_effect=Exception("telegram down")):
            main._guarded(boom, "job")()      # must not raise


# ---------------------------------------------------------------------------
# Long-only guard
# ---------------------------------------------------------------------------

class TestShortPositionGuard:
    def test_aborts_on_a_short_position_in_a_managed_symbol(self):
        """A negative position inverts the order maths and must never be traded through."""
        holdings = {"NVDA": {"shares": -2.0, "notional": -250.0}}
        with patch.object(main, "get_account_equity", return_value=1000.0), \
             patch.object(main, "get_current_holdings", return_value=holdings), \
             patch.object(main, "_evaluate_regimes") as regimes, \
             patch.object(main, "send_error_alert") as alert, \
             _settings(symbols=("NVDA",)):
            main._run_rebalance(MagicMock())
        regimes.assert_not_called()
        alert.assert_called_once()
        assert "long-only" in alert.call_args[0][0]

    def test_normal_long_holdings_pass_the_guard(self):
        holdings = {"NVDA": {"shares": 1.0, "notional": 125.0}}
        with patch.object(main, "get_account_equity", return_value=1000.0), \
             patch.object(main, "get_current_holdings", return_value=holdings), \
             patch.object(main, "_evaluate_regimes", return_value={}) as regimes, \
             patch.object(main, "send_error_alert"), \
             _settings(symbols=("NVDA",)):
            main._run_rebalance(MagicMock())
        regimes.assert_called_once()
