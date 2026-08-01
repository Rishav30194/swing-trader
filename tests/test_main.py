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

from contextlib import ExitStack
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
    """
    Drives the whole cycle with every boundary mocked. `_run` applies a working
    set of defaults so each test only states the one thing it is varying.
    """

    DEFAULTS = {
        "get_account_equity": 100_000.0,      # deliberately NOT the strategy's capital
        "get_account_cash": 100_000.0,
        "get_strategy_cash": 1_000.0,
        "get_current_holdings": {},
        "_evaluate_regimes": {"NVDA": RegimeState(on=True)},
        "send_rebalance_plan": True,
        "_await_approval": False,     # never let a real Telegram poll start
    }
    VOID = ("log_event", "set_strategy_cash", "send_rebalance_result",
            "send_error_alert", "_execute_plan")

    def _run(self, symbols=("NVDA",), **overrides):
        """Run one rebalance; returns the dict of patched mocks."""
        def _patch(name, value):
            if isinstance(value, dict) and "__raises__" in value:
                return patch.object(main, name, side_effect=value["__raises__"])
            return patch.object(main, name, return_value=value)

        with ExitStack() as stack:
            mocks = {}
            values = {**self.DEFAULTS, **overrides}
            for name, value in values.items():
                mocks[name] = stack.enter_context(_patch(name, value))
            for name in self.VOID:
                if name not in mocks:
                    mocks[name] = stack.enter_context(patch.object(main, name))
            stack.enter_context(_settings(symbols=symbols))
            main._run_rebalance(MagicMock())
            return mocks

    def _raises(self, exc):
        return {"__raises__": exc}

    def test_sizes_off_strategy_capital_not_account_equity(self):
        """A $100k paper account must still trade the $1k allocated to it."""
        mocks = self._run(symbols=("NVDA",) * 8, _execute_plan=[])
        orders = mocks["send_rebalance_plan"].call_args[0][0]
        assert len(orders) == 1
        assert orders[0].notional == pytest.approx(125.0)      # 1/8 of $1,000
        assert orders[0].notional != pytest.approx(12_500.0)   # 1/8 of $100,000

    def test_plan_message_shows_both_capital_and_account(self):
        mocks = self._run(_execute_plan=[])
        args = mocks["send_rebalance_plan"].call_args[0]
        assert args[2] == pytest.approx(1_000.0)     # strategy capital
        assert args[3] == pytest.approx(100_000.0)   # account equity

    def test_profits_compound_into_deployable_capital(self):
        """Once the sleeves are worth more, the strategy sizes off the larger figure."""
        holdings = {"NVDA": {"shares": 1.0, "notional": 1_100.0}}
        mocks = self._run(symbols=("NVDA",) * 8,
                          get_current_holdings=holdings, get_strategy_cash=0.0,
                          _execute_plan=[])
        assert mocks["send_rebalance_plan"].call_args[0][2] == pytest.approx(1_100.0)

    def test_aborts_when_equity_fetch_fails(self):
        mocks = self._run(get_account_equity=self._raises(RuntimeError("down")))
        mocks["_evaluate_regimes"].assert_not_called()
        mocks["send_error_alert"].assert_called_once()

    def test_aborts_when_holdings_fetch_fails(self):
        """Acting on an unknown portfolio state is worse than doing nothing."""
        mocks = self._run(get_current_holdings=self._raises(RuntimeError("down")))
        mocks["_evaluate_regimes"].assert_not_called()
        mocks["send_error_alert"].assert_called_once()

    def test_aborts_when_no_symbol_could_be_evaluated(self):
        mocks = self._run(_evaluate_regimes={})
        mocks["send_rebalance_plan"].assert_not_called()
        mocks["send_error_alert"].assert_called_once()

    def test_sends_plan_and_stops_when_no_orders_needed(self):
        # already at target: 1/8 of $1,000 equity, held exactly
        holdings = {"NVDA": {"shares": 1.0, "notional": 125.0}}
        mocks = self._run(symbols=("NVDA",) * 8,
                          get_current_holdings=holdings, get_strategy_cash=875.0)
        mocks["send_rebalance_plan"].assert_called_once()
        mocks["send_rebalance_result"].assert_not_called()

    def test_validation_failure_alerts_instead_of_escaping(self):
        """A hard-rule violation must reach the user, not die in APScheduler."""
        mocks = self._run(validate_target_weights=self._raises(
            ValueError("hard rule 5 violated")))
        mocks["send_error_alert"].assert_called_once()
        mocks["_execute_plan"].assert_not_called()

    def test_no_orders_are_placed_when_sizing_fails(self):
        mocks = self._run(diff_to_orders=self._raises(ValueError("bad equity")))
        mocks["_execute_plan"].assert_not_called()

    def test_alerts_when_an_order_fails(self):
        mocks = self._run(_execute_plan=[
            {"symbol": "NVDA", "side": "buy", "notional": 125.0, "status": "failed"}])
        mocks["send_error_alert"].assert_called_once()

    def test_alerts_on_partial_fill(self):
        mocks = self._run(_execute_plan=[
            {"symbol": "NVDA", "side": "buy", "notional": 60.0, "status": "partial"}])
        mocks["send_error_alert"].assert_called_once()

    def test_cash_ledger_is_updated_after_execution(self):
        mocks = self._run(_execute_plan=[
            {"symbol": "NVDA", "side": "buy", "notional": 125.0, "status": "filled"}])
        mocks["set_strategy_cash"].assert_called_once()
        assert mocks["set_strategy_cash"].call_args[0][1] == pytest.approx(875.0)

    def test_plan_is_json_serialisable_for_the_event_log(self):
        """log_event JSON-encodes the plan; a non-serialisable order would crash it."""
        import json
        mocks = self._run(_execute_plan=[])
        json.dumps(mocks["log_event"].call_args[0][3])


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


# ---------------------------------------------------------------------------
# compute_strategy_equity — the $1,000-in-a-$100,000-account problem
# ---------------------------------------------------------------------------

class TestComputeStrategyEquity:
    def test_uses_allocation_not_account_balance(self):
        with _settings(symbols=("NVDA",)):
            eq = main.compute_strategy_equity({}, 1_000.0, 100_000.0, 100_000.0)
        assert eq == pytest.approx(1_000.0)

    def test_managed_value_plus_cash(self):
        holdings = {"NVDA": {"shares": 1.0, "notional": 600.0}}
        with _settings(symbols=("NVDA",)):
            eq = main.compute_strategy_equity(holdings, 400.0, 100_000.0, 100_000.0)
        assert eq == pytest.approx(1_000.0)

    def test_profits_compound(self):
        """$1,000 that grew to $1,100 becomes $1,100 of deployable capital."""
        holdings = {"NVDA": {"shares": 1.0, "notional": 1_100.0}}
        with _settings(symbols=("NVDA",)):
            eq = main.compute_strategy_equity(holdings, 0.0, 100_000.0, 100_000.0)
        assert eq == pytest.approx(1_100.0)

    def test_unmanaged_positions_are_excluded(self):
        """A manually-held stock must not inflate the strategy's capital."""
        holdings = {"NVDA": {"shares": 1.0, "notional": 500.0},
                    "TSLA": {"shares": 10.0, "notional": 50_000.0}}
        with _settings(symbols=("NVDA",)):
            eq = main.compute_strategy_equity(holdings, 500.0, 100_000.0, 100_000.0)
        assert eq == pytest.approx(1_000.0)

    def test_capped_by_account_equity(self):
        """The ledger can never authorise more than the account actually holds."""
        with _settings(symbols=("NVDA",)):
            eq = main.compute_strategy_equity({}, 5_000.0, 800.0, 800.0)
        assert eq == pytest.approx(800.0)

    def test_cash_capped_by_account_cash(self):
        with _settings(symbols=("NVDA",)):
            eq = main.compute_strategy_equity({}, 1_000.0, 100_000.0, 250.0)
        assert eq == pytest.approx(250.0)

    def test_empty_account_gives_zero(self):
        with _settings(symbols=("NVDA",)):
            assert main.compute_strategy_equity({}, 0.0, 0.0, 0.0) == pytest.approx(0.0)


class TestUpdateStrategyCash:
    def _r(self, side, notional, status):
        return {"symbol": "NVDA", "side": side, "notional": notional, "status": status}

    def test_buy_reduces_cash(self):
        conn = MagicMock()
        with patch.object(main, "set_strategy_cash") as setter:
            closing = main._update_strategy_cash(conn, 1_000.0, [self._r("buy", 125.0, "filled")])
        assert closing == pytest.approx(875.0)
        setter.assert_called_once()

    def test_sell_increases_cash(self):
        with patch.object(main, "set_strategy_cash"):
            closing = main._update_strategy_cash(
                MagicMock(), 100.0, [self._r("sell", 250.0, "filled")])
        assert closing == pytest.approx(350.0)

    def test_skipped_orders_do_not_move_the_ledger(self):
        with patch.object(main, "set_strategy_cash"):
            closing = main._update_strategy_cash(
                MagicMock(), 1_000.0, [self._r("buy", 125.0, "skipped")])
        assert closing == pytest.approx(1_000.0)

    def test_failed_orders_do_not_move_the_ledger(self):
        with patch.object(main, "set_strategy_cash"):
            closing = main._update_strategy_cash(
                MagicMock(), 1_000.0, [self._r("buy", 125.0, "failed")])
        assert closing == pytest.approx(1_000.0)

    def test_partial_fills_do_move_the_ledger(self):
        with patch.object(main, "set_strategy_cash"):
            closing = main._update_strategy_cash(
                MagicMock(), 1_000.0, [self._r("buy", 60.0, "partial")])
        assert closing == pytest.approx(940.0)

    def test_never_goes_negative(self):
        with patch.object(main, "set_strategy_cash"):
            closing = main._update_strategy_cash(
                MagicMock(), 100.0, [self._r("buy", 500.0, "filled")])
        assert closing == pytest.approx(0.0)
