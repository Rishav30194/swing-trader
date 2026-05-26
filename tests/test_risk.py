"""
Unit tests for src/risk.py.

No network calls, no .env dependency. All inputs are hand-crafted so
assertions can be exact or within a known tolerance.
"""

from datetime import date

import pandas as pd
import pytest

from src.risk import (
    ExitLevels,
    Position,
    compute_exit_levels,
    compute_position_size,
    update_trailing_stop,
    check_exit_conditions,
)

# Default multipliers matching the documented strategy
_STOP_MULT = 2.0
_TP_MULT   = 3.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_position(
    *,
    symbol:        str   = "NVDA",
    entry_price:   float = 100.0,
    shares:        float = 10.0,
    stop_loss:     float = 96.0,   # entry − 2×ATR (ATR=2)
    trailing_stop: float = 96.0,
    take_profit:   float = 106.0,  # entry + 3×ATR
    entry_date:    date  = date(2024, 1, 2),
) -> Position:
    return Position(
        symbol=symbol,
        entry_price=entry_price,
        shares=shares,
        stop_loss=stop_loss,
        trailing_stop=trailing_stop,
        take_profit=take_profit,
        entry_date=entry_date,
    )


def _make_bar(
    *,
    low:       float = 98.0,
    high:      float = 102.0,
    close:     float = 101.0,
    timestamp: str   = "2024-01-04",   # 2 days after entry by default
) -> pd.Series:
    return pd.Series({
        "low":       low,
        "high":      high,
        "close":     close,
        "timestamp": pd.Timestamp(timestamp, tz="UTC"),
    })


# ---------------------------------------------------------------------------
# compute_exit_levels
# ---------------------------------------------------------------------------

def test_exit_levels_basic():
    levels = compute_exit_levels(100.0, 2.0, atr_stop_multiplier=2.0, atr_tp_multiplier=3.0)
    assert levels.stop_loss   == pytest.approx(96.0)
    assert levels.take_profit == pytest.approx(106.0)


def test_stop_loss_below_entry():
    levels = compute_exit_levels(100.0, 2.0, atr_stop_multiplier=2.0, atr_tp_multiplier=3.0)
    assert levels.stop_loss < 100.0


def test_take_profit_above_entry():
    levels = compute_exit_levels(100.0, 2.0, atr_stop_multiplier=2.0, atr_tp_multiplier=3.0)
    assert levels.take_profit > 100.0


def test_rr_ratio_greater_than_1():
    levels = compute_exit_levels(100.0, 2.0, atr_stop_multiplier=2.0, atr_tp_multiplier=3.0)
    risk   = 100.0 - levels.stop_loss
    reward = levels.take_profit - 100.0
    assert reward / risk == pytest.approx(1.5)


def test_larger_atr_widens_both_levels():
    small = compute_exit_levels(100.0, 1.0, atr_stop_multiplier=2.0, atr_tp_multiplier=3.0)
    large = compute_exit_levels(100.0, 4.0, atr_stop_multiplier=2.0, atr_tp_multiplier=3.0)
    assert large.stop_loss   < small.stop_loss
    assert large.take_profit > small.take_profit


def test_exit_levels_raises_on_zero_entry():
    with pytest.raises(ValueError, match="entry_price"):
        compute_exit_levels(0.0, 2.0, atr_stop_multiplier=2.0, atr_tp_multiplier=3.0)


def test_exit_levels_raises_on_zero_atr():
    with pytest.raises(ValueError, match="atr"):
        compute_exit_levels(100.0, 0.0, atr_stop_multiplier=2.0, atr_tp_multiplier=3.0)


def test_exit_levels_raises_on_negative_atr():
    with pytest.raises(ValueError, match="atr"):
        compute_exit_levels(100.0, -1.0, atr_stop_multiplier=2.0, atr_tp_multiplier=3.0)


# ---------------------------------------------------------------------------
# compute_position_size
# ---------------------------------------------------------------------------

def test_position_size_basic():
    # equity=10_000, risk=2% → $200 risk
    # entry=100, stop=96 → $4 risk per share → 50 shares
    shares = compute_position_size(10_000.0, 0.02, 100.0, 96.0)
    assert shares == pytest.approx(50.0)


def test_position_size_allows_fractional_shares():
    # risk=$200, risk_per_share=$3 → 66.666… — no flooring with fractional orders
    shares = compute_position_size(10_000.0, 0.02, 100.0, 97.0)
    assert shares == pytest.approx(200.0 / 3.0)


def test_position_size_scales_with_equity():
    small  = compute_position_size(5_000.0,  0.02, 100.0, 96.0)
    large  = compute_position_size(20_000.0, 0.02, 100.0, 96.0)
    assert large == pytest.approx(small * 4)   # 20k is 4× 5k


def test_position_size_returns_fractional_for_small_account():
    # equity=10, risk=2% → $0.20 risk; entry=100, stop=96 → $4/share → 0.05 shares
    # fractional orders: no longer returns 0; main.py guards on notional < $1
    shares = compute_position_size(10.0, 0.02, 100.0, 96.0)
    assert shares == pytest.approx(0.05)
    assert 0.0 < shares < 1.0


def test_position_size_raises_if_stop_above_entry():
    with pytest.raises(ValueError, match="stop_loss.*must be below"):
        compute_position_size(10_000.0, 0.02, 100.0, 105.0)


def test_position_size_raises_if_stop_equals_entry():
    with pytest.raises(ValueError, match="stop_loss.*must be below"):
        compute_position_size(10_000.0, 0.02, 100.0, 100.0)


def test_position_size_raises_on_zero_equity():
    with pytest.raises(ValueError, match="equity"):
        compute_position_size(0.0, 0.02, 100.0, 96.0)


def test_position_size_raises_on_zero_risk_pct():
    with pytest.raises(ValueError, match="risk_pct"):
        compute_position_size(10_000.0, 0.0, 100.0, 96.0)


# ---------------------------------------------------------------------------
# update_trailing_stop
# ---------------------------------------------------------------------------

def test_trailing_stop_does_not_activate_before_threshold():
    # ATR=2, entry=100, activation=0.5 → threshold = 100 + 0.5×2 = 101
    # current_price=100.5 < 101 → stop unchanged
    pos = _make_position(entry_price=100.0, trailing_stop=96.0)
    new_stop = update_trailing_stop(pos, 100.5, 2.0, atr_stop_multiplier=_STOP_MULT, atr_trailing_activation=0.5)
    assert new_stop == 96.0


def test_trailing_stop_does_not_activate_at_exact_threshold():
    # current_price == entry + 0.5×ATR = 101 → condition is <=, so no activation
    pos = _make_position(entry_price=100.0, trailing_stop=96.0)
    new_stop = update_trailing_stop(pos, 101.0, 2.0, atr_stop_multiplier=_STOP_MULT, atr_trailing_activation=0.5)
    assert new_stop == 96.0


def test_trailing_stop_activates_above_threshold():
    # current_price=103 > threshold(101) → candidate = 103 − 4 = 99 > 96 → new stop = 99
    pos = _make_position(entry_price=100.0, trailing_stop=96.0)
    new_stop = update_trailing_stop(pos, 103.0, 2.0, atr_stop_multiplier=_STOP_MULT, atr_trailing_activation=0.5)
    assert new_stop == pytest.approx(99.0)


def test_trailing_stop_never_moves_down():
    # price is above threshold(101) but candidate 98.5 < current trailing_stop=99 → stays at 99
    pos = _make_position(entry_price=100.0, trailing_stop=99.0)
    new_stop = update_trailing_stop(pos, 102.5, 2.0, atr_stop_multiplier=_STOP_MULT, atr_trailing_activation=0.5)
    assert new_stop == pytest.approx(99.0)


def test_trailing_stop_ratchets_up_with_rising_price():
    pos = _make_position(entry_price=100.0, trailing_stop=96.0)
    # First tick up: 105 > threshold(101), candidate = 105 − 4 = 101
    stop1 = update_trailing_stop(pos, 105.0, 2.0, atr_stop_multiplier=_STOP_MULT, atr_trailing_activation=0.5)
    assert stop1 == pytest.approx(101.0)
    # Second tick up: 108 > threshold, candidate = 108 − 4 = 104
    pos2 = _make_position(entry_price=100.0, trailing_stop=stop1)
    stop2 = update_trailing_stop(pos2, 108.0, 2.0, atr_stop_multiplier=_STOP_MULT, atr_trailing_activation=0.5)
    assert stop2 == pytest.approx(104.0)
    assert stop2 > stop1


# ---------------------------------------------------------------------------
# check_exit_conditions
# ---------------------------------------------------------------------------

def test_no_exit_when_price_in_range():
    pos = _make_position()
    bar = _make_bar(low=97.0, high=103.0, timestamp="2024-01-04")  # day 2
    assert check_exit_conditions(pos, bar) is None


def test_hard_stop_triggered_when_low_hits_stop():
    pos = _make_position(stop_loss=96.0)
    bar = _make_bar(low=95.5, high=100.0)
    assert check_exit_conditions(pos, bar) == "sl"


def test_hard_stop_triggered_at_exactly_stop_price():
    pos = _make_position(stop_loss=96.0)
    bar = _make_bar(low=96.0, high=100.0)
    assert check_exit_conditions(pos, bar) == "sl"


def test_trailing_stop_triggered_when_low_hits_trailing():
    # trailing_stop raised above hard stop_loss
    pos = _make_position(stop_loss=96.0, trailing_stop=99.0)
    bar = _make_bar(low=98.5, high=103.0)
    assert check_exit_conditions(pos, bar) == "trailing"


def test_take_profit_triggered_when_high_reaches_tp():
    pos = _make_position(take_profit=106.0)
    bar = _make_bar(low=99.0, high=107.0)
    assert check_exit_conditions(pos, bar) == "tp"


def test_take_profit_triggered_at_exactly_tp_price():
    pos = _make_position(take_profit=106.0)
    bar = _make_bar(low=100.0, high=106.0)
    assert check_exit_conditions(pos, bar) == "tp"


def test_day5_triggered_after_5_trading_days():
    # Entry Tue Jan 2 → 5 trading days = Wed Jan 9 (Tue+Wed+Thu+Fri+Mon = 5)
    pos = _make_position(entry_date=date(2024, 1, 2))
    bar = _make_bar(low=97.0, high=103.0, timestamp="2024-01-09")
    assert check_exit_conditions(pos, bar) == "day5"


def test_day5_not_triggered_on_trading_day_4():
    # Entry Tue Jan 2 → 4 trading days = Mon Jan 8 (Tue+Wed+Thu+Fri = 4)
    pos = _make_position(entry_date=date(2024, 1, 2))
    bar = _make_bar(low=97.0, high=103.0, timestamp="2024-01-08")
    assert check_exit_conditions(pos, bar) is None


def test_day5_triggered_beyond_day_5():
    # Entry Tue Jan 2 → 6 trading days = Thu Jan 10
    pos = _make_position(entry_date=date(2024, 1, 2))
    bar = _make_bar(low=97.0, high=103.0, timestamp="2024-01-10")
    assert check_exit_conditions(pos, bar) == "day5"


def test_day5_not_triggered_thursday_entry_tuesday_exit():
    # The bug this fix addresses: Thu Jan 11 entry + 5 calendar days = Tue Jan 16
    # but that is only 3 trading days (Thu+Fri+Mon) — should NOT fire
    pos = _make_position(entry_date=date(2024, 1, 11))
    bar = _make_bar(low=97.0, high=103.0, timestamp="2024-01-16")
    assert check_exit_conditions(pos, bar) is None


def test_hard_stop_takes_priority_over_trailing():
    # Both hard stop and trailing stop breached — hard stop wins
    pos = _make_position(stop_loss=96.0, trailing_stop=99.0)
    bar = _make_bar(low=94.0, high=100.0)   # below both
    assert check_exit_conditions(pos, bar) == "sl"


def test_stop_takes_priority_over_tp():
    # Extreme bar: low hits stop AND high hits TP (gap or volatile day)
    pos = _make_position(stop_loss=96.0, take_profit=106.0)
    bar = _make_bar(low=95.0, high=107.0)
    assert check_exit_conditions(pos, bar) == "sl"


def test_trailing_takes_priority_over_tp():
    pos = _make_position(stop_loss=96.0, trailing_stop=99.0, take_profit=106.0)
    bar = _make_bar(low=98.0, high=107.0)
    assert check_exit_conditions(pos, bar) == "trailing"


def test_tp_takes_priority_over_day5():
    pos = _make_position(entry_date=date(2024, 1, 2), take_profit=106.0)
    # Day 7 bar also hits TP — tp should win
    bar = _make_bar(low=100.0, high=107.0, timestamp="2024-01-09")
    assert check_exit_conditions(pos, bar) == "tp"
