"""
test_portfolio.py — Unit tests for src/portfolio.py.

The whole strategy lives in this module, so these tests are the ones that
actually protect the validated behaviour. No API calls; every function is pure.

Coverage:
  - Hysteresis band: entry, exit, and the hold region between them.
  - NaN SMA_200 holds the current state rather than churning the sleeve.
  - Equal weighting across ALL symbols, with off sleeves at exactly 0.
  - Hard rules 2 and 5 enforced by validate_target_weights.
  - Order generation: sizing, dust threshold, classification, sell-first order.
"""

import numpy as np
import pandas as pd
import pytest

from src.portfolio import (
    RebalanceOrder,
    RegimeState,
    compute_regime_state,
    compute_target_weights,
    diff_to_orders,
    validate_target_weights,
)

BAND = 0.02


def _df(close: float, sma: float | float = 100.0) -> pd.DataFrame:
    """Minimal indicator-enriched frame — only the last row is read."""
    return pd.DataFrame({
        "close":   [close - 1, close],
        "SMA_200": [sma, sma],
    })


# ---------------------------------------------------------------------------
# compute_regime_state — hysteresis
# ---------------------------------------------------------------------------

class TestRegimeEntry:
    def test_enters_above_upper_band(self):
        # 103 > 100 * 1.02
        assert compute_regime_state(_df(103.0), band=BAND, currently_held=False).on is True

    def test_does_not_enter_inside_band(self):
        # 101 is above the SMA but has not cleared 102 — stays flat
        assert compute_regime_state(_df(101.0), band=BAND, currently_held=False).on is False

    def test_does_not_enter_exactly_at_upper_band(self):
        assert compute_regime_state(_df(102.0), band=BAND, currently_held=False).on is False

    def test_does_not_enter_below_sma(self):
        assert compute_regime_state(_df(90.0), band=BAND, currently_held=False).on is False

    def test_entry_reason_recorded(self):
        state = compute_regime_state(_df(103.0), band=BAND, currently_held=False)
        assert state.context["reason"] == "entry_band_cleared"


class TestRegimeExit:
    def test_exits_below_lower_band(self):
        # 97 < 100 * 0.98
        assert compute_regime_state(_df(97.0), band=BAND, currently_held=True).on is False

    def test_holds_inside_band(self):
        # 99 is below the SMA but above 98 — hysteresis keeps the sleeve on
        assert compute_regime_state(_df(99.0), band=BAND, currently_held=True).on is True

    def test_holds_exactly_at_lower_band(self):
        assert compute_regime_state(_df(98.0), band=BAND, currently_held=True).on is True

    def test_holds_well_above_sma(self):
        assert compute_regime_state(_df(150.0), band=BAND, currently_held=True).on is True

    def test_exit_reason_recorded(self):
        state = compute_regime_state(_df(97.0), band=BAND, currently_held=True)
        assert state.context["reason"] == "exit_band_broken"


class TestHysteresisIsAsymmetric:
    def test_same_price_gives_different_state_by_history(self):
        """The point of the band: 99 holds if held, stays flat if flat."""
        held = compute_regime_state(_df(99.0), band=BAND, currently_held=True)
        flat = compute_regime_state(_df(99.0), band=BAND, currently_held=False)
        assert held.on is True
        assert flat.on is False

    def test_zero_band_collapses_to_plain_crossover(self):
        above = compute_regime_state(_df(100.5), band=0.0, currently_held=False)
        below = compute_regime_state(_df(99.5), band=0.0, currently_held=True)
        assert above.on is True
        assert below.on is False


class TestRegimeContext:
    def test_context_carries_decision_inputs(self):
        ctx = compute_regime_state(_df(103.0, 100.0), band=BAND, currently_held=False).context
        assert ctx["close"] == 103.0
        assert ctx["sma_200"] == 100.0
        assert ctx["lower_band"] == pytest.approx(98.0)
        assert ctx["upper_band"] == pytest.approx(102.0)
        assert ctx["was_held"] is False


class TestRegimeNaNHandling:
    def test_nan_sma_holds_existing_position(self):
        df = pd.DataFrame({"close": [100.0, 100.0], "SMA_200": [np.nan, np.nan]})
        assert compute_regime_state(df, band=BAND, currently_held=True).on is True

    def test_nan_sma_keeps_flat_sleeve_flat(self):
        df = pd.DataFrame({"close": [100.0, 100.0], "SMA_200": [np.nan, np.nan]})
        assert compute_regime_state(df, band=BAND, currently_held=False).on is False

    def test_nan_sma_records_skip_reason(self):
        df = pd.DataFrame({"close": [100.0, 100.0], "SMA_200": [np.nan, np.nan]})
        state = compute_regime_state(df, band=BAND, currently_held=False)
        assert state.context["skip_reason"] == "sma_200_nan"


class TestRegimeValidation:
    def test_missing_sma_column_raises(self):
        with pytest.raises(ValueError, match="missing columns"):
            compute_regime_state(pd.DataFrame({"close": [1.0]}), band=BAND,
                                 currently_held=False)

    def test_empty_frame_raises(self):
        with pytest.raises(ValueError, match="empty"):
            compute_regime_state(pd.DataFrame({"close": [], "SMA_200": []}),
                                 band=BAND, currently_held=False)


# ---------------------------------------------------------------------------
# compute_target_weights
# ---------------------------------------------------------------------------

class TestTargetWeights:
    def _states(self, **flags) -> dict[str, RegimeState]:
        return {sym: RegimeState(on=on) for sym, on in flags.items()}

    def _weights(self, states, *, universe_size=None, cap=1.0):
        return compute_target_weights(
            states,
            universe_size=universe_size if universe_size is not None else len(states),
            max_position_pct=cap,
        )

    def test_equal_weight_when_all_on(self):
        w = self._weights(self._states(A=True, B=True, C=True, D=True), cap=0.25)
        assert all(v == pytest.approx(0.25) for v in w.values())

    def test_off_sleeve_gets_exactly_zero(self):
        assert self._weights(self._states(A=True, B=False))["B"] == 0.0

    def test_weights_do_not_concentrate_when_sleeves_switch_off(self):
        """The remaining sleeve must stay at 1/N, not absorb the freed capital."""
        w = self._weights(self._states(A=True, B=False, C=False, D=False))
        assert w["A"] == pytest.approx(0.25)
        assert sum(w.values()) == pytest.approx(0.25)

    def test_weights_do_not_concentrate_when_a_symbol_cannot_be_evaluated(self):
        """
        A symbol missing from `states` (data outage, or not yet listed) must
        leave its weight in cash. Dividing by len(states) here would hand its
        capital to the survivors — concentrating exactly when data is missing.
        """
        w = self._weights(self._states(A=True, B=True, C=True), universe_size=4)
        assert all(v == pytest.approx(0.25) for v in w.values())
        assert sum(w.values()) == pytest.approx(0.75)

    def test_cap_applies_when_below_equal_weight(self):
        w = self._weights(self._states(A=True, B=True), cap=0.10)
        assert all(v == pytest.approx(0.10) for v in w.values())

    def test_weights_sum_to_one_when_all_on_and_uncapped(self):
        w = self._weights(self._states(A=True, B=True, C=True, D=True))
        assert sum(w.values()) == pytest.approx(1.0)

    def test_empty_states_raises(self):
        with pytest.raises(ValueError, match="must not be empty"):
            compute_target_weights({}, universe_size=4, max_position_pct=0.25)

    def test_universe_smaller_than_states_raises(self):
        with pytest.raises(ValueError, match="smaller than"):
            compute_target_weights(self._states(A=True, B=True),
                                   universe_size=1, max_position_pct=0.25)

    @pytest.mark.parametrize("bad", [0.0, -0.1, 1.5])
    def test_invalid_cap_raises(self, bad):
        with pytest.raises(ValueError, match="max_position_pct"):
            self._weights(self._states(A=True), cap=bad)


# ---------------------------------------------------------------------------
# validate_target_weights — hard rules 2 and 5
# ---------------------------------------------------------------------------

class TestValidateTargetWeights:
    def test_accepts_valid_weights(self):
        states = {"A": RegimeState(on=True), "B": RegimeState(on=False)}
        validate_target_weights({"A": 0.25, "B": 0.0}, states, max_position_pct=0.25)

    def test_rejects_weight_above_cap(self):
        states = {"A": RegimeState(on=True)}
        with pytest.raises(ValueError, match="hard rule 5"):
            validate_target_weights({"A": 0.5}, states, max_position_pct=0.25)

    def test_rejects_nonzero_weight_on_off_sleeve(self):
        states = {"A": RegimeState(on=False)}
        with pytest.raises(ValueError, match="hard rule 2"):
            validate_target_weights({"A": 0.25}, states, max_position_pct=0.25)

    def test_rejects_negative_weight(self):
        states = {"A": RegimeState(on=True)}
        with pytest.raises(ValueError, match="negative"):
            validate_target_weights({"A": -0.1}, states, max_position_pct=0.25)

    def test_rejects_total_above_one(self):
        states = {"A": RegimeState(on=True), "B": RegimeState(on=True)}
        with pytest.raises(ValueError, match="exceeding 1.0"):
            validate_target_weights({"A": 0.8, "B": 0.8}, states, max_position_pct=0.9)


# ---------------------------------------------------------------------------
# diff_to_orders
# ---------------------------------------------------------------------------

class TestDiffToOrders:
    def test_buys_when_flat_and_target_positive(self):
        orders = diff_to_orders({}, {"A": 0.25}, 1000.0)
        assert len(orders) == 1
        assert orders[0].side == "buy"
        assert orders[0].notional == pytest.approx(250.0)

    def test_sells_entire_sleeve_when_target_zero(self):
        orders = diff_to_orders({"A": 250.0}, {"A": 0.0}, 1000.0)
        assert orders[0].side == "sell"
        assert orders[0].notional == pytest.approx(250.0)
        assert orders[0].reason == "regime_exit"

    def test_entry_is_classified_as_regime_entry(self):
        assert diff_to_orders({}, {"A": 0.25}, 1000.0)[0].reason == "regime_entry"

    def test_partial_adjustment_is_classified_as_drift(self):
        orders = diff_to_orders({"A": 200.0}, {"A": 0.25}, 1000.0)
        assert orders[0].reason == "drift"
        assert orders[0].notional == pytest.approx(50.0)

    def test_increases_exposure_flag_matches_side(self):
        buy = diff_to_orders({}, {"A": 0.25}, 1000.0)[0]
        sell = diff_to_orders({"A": 250.0}, {"A": 0.0}, 1000.0)[0]
        assert buy.increases_exposure is True
        assert sell.increases_exposure is False

    def test_skips_dust_below_drift_tolerance(self):
        # 0.1% of 1000 = $1 threshold; a $0.50 gap must not generate an order
        assert diff_to_orders({"A": 249.5}, {"A": 0.25}, 1000.0,
                              min_order_notional=0.01, drift_tolerance=0.001) == []

    def test_skips_orders_below_alpaca_minimum(self):
        assert diff_to_orders({"A": 249.5}, {"A": 0.25}, 1000.0,
                              min_order_notional=1.0, drift_tolerance=0.0) == []

    def test_regime_transition_always_clears_threshold(self):
        orders = diff_to_orders({}, {"A": 0.25}, 1000.0, drift_tolerance=0.1)
        assert len(orders) == 1

    def test_sells_are_ordered_before_buys(self):
        orders = diff_to_orders(
            {"SELLME": 250.0}, {"SELLME": 0.0, "BUYME": 0.25}, 1000.0)
        assert [o.side for o in orders] == ["sell", "buy"]

    def test_symbol_absent_from_holdings_treated_as_flat(self):
        orders = diff_to_orders({"OTHER": 100.0}, {"A": 0.25}, 1000.0)
        assert orders[0].symbol == "A"
        assert orders[0].notional == pytest.approx(250.0)

    def test_no_orders_when_already_at_target(self):
        assert diff_to_orders({"A": 250.0}, {"A": 0.25}, 1000.0) == []

    def test_zero_target_and_zero_holding_produces_nothing(self):
        assert diff_to_orders({}, {"A": 0.0}, 1000.0) == []

    @pytest.mark.parametrize("bad_equity", [0.0, -100.0])
    def test_non_positive_equity_raises(self, bad_equity):
        with pytest.raises(ValueError, match="equity must be positive"):
            diff_to_orders({}, {"A": 0.25}, bad_equity)

    def test_returns_rebalance_order_instances(self):
        assert all(isinstance(o, RebalanceOrder)
                   for o in diff_to_orders({}, {"A": 0.25}, 1000.0))

    def test_notional_is_always_positive(self):
        orders = diff_to_orders({"A": 500.0}, {"A": 0.25}, 1000.0)
        assert orders[0].notional > 0
        assert orders[0].side == "sell"


# ---------------------------------------------------------------------------
# drift_tolerance must gate ONLY drift — regime decisions always execute
# ---------------------------------------------------------------------------

class TestDriftToleranceScope:
    def test_regime_entry_fires_despite_a_huge_drift_tolerance(self):
        """
        Gating regime transitions on drift_tolerance silently stopped all
        trading once the tolerance exceeded 1/N.
        """
        orders = diff_to_orders({}, {"A": 0.125}, 100_000.0, drift_tolerance=0.25)
        assert len(orders) == 1
        assert orders[0].reason == "regime_entry"

    def test_regime_exit_fires_despite_a_huge_drift_tolerance(self):
        orders = diff_to_orders({"A": 12_500.0}, {"A": 0.0}, 100_000.0,
                                drift_tolerance=0.25)
        assert len(orders) == 1
        assert orders[0].reason == "regime_exit"

    def test_drift_is_suppressed_by_the_tolerance(self):
        orders = diff_to_orders({"A": 10_000.0}, {"A": 0.125}, 100_000.0,
                                drift_tolerance=0.25)
        assert orders == []

    def test_drift_fires_when_below_the_tolerance(self):
        orders = diff_to_orders({"A": 10_000.0}, {"A": 0.125}, 100_000.0,
                                drift_tolerance=0.001)
        assert len(orders) == 1
        assert orders[0].reason == "drift"

    def test_regime_transitions_still_respect_the_broker_minimum(self):
        """A sub-$1 exit costs more in fees than it recovers."""
        orders = diff_to_orders({"A": 0.50}, {"A": 0.0}, 1_000.0,
                                min_order_notional=1.0, drift_tolerance=0.0)
        assert orders == []
