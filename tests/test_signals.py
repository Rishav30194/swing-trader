"""
Unit tests for src/signals.py.

All tests use synthetic DataFrames with hand-crafted indicator values.
No network calls, no .env dependency.

Default fixture: all three conditions pass. Each test group then isolates
one condition to confirm it can independently block the signal.
"""

import pandas as pd
import pytest

from src.signals import evaluate_buy_signal, SignalResult

_RSI_LOWER = 40.0
_RSI_UPPER = 55.0


# ---------------------------------------------------------------------------
# Fixture helper
# ---------------------------------------------------------------------------

def _make_df(
    *,
    # Condition 1 — trend filter
    close: float = 100.0,
    ema_50: float = 95.0,              # close > ema_50 → passes
    # Condition 2 — RSI pullback range
    rsi: float = 47.0,                 # 40 <= 47 < 55 → passes
    # Condition 3 — MACD crossover
    macd_curr: float = 0.5,
    macd_signal_curr: float = 0.3,     # curr > signal → above
    macd_prev: float = -0.1,
    macd_signal_prev: float = 0.0,     # prev <= signal → was below → crossover
    # Other required columns
    atr: float = 2.0,
) -> pd.DataFrame:
    """
    Build a 2-row DataFrame with explicit indicator values.
    Row 0 = previous bar, Row 1 = current bar.
    By default all three buy-signal conditions are satisfied on the current bar.
    """
    dates = pd.date_range("2024-01-01", periods=2, freq="B", tz="UTC")
    return pd.DataFrame({
        "timestamp":   dates,
        "open":        [close - 0.5,  close - 0.5],
        "high":        [close + 1.0,  close + 1.0],
        "low":         [close - 1.0,  close - 1.0],
        "close":       [close,        close],
        "volume":      [1_000_000,    1_000_000],
        "RSI_14":      [rsi,          rsi],
        "EMA_21":      [ema_50 * 1.02, ema_50 * 1.02],
        "EMA_50":      [ema_50,        ema_50],
        "MACD":        [macd_prev,    macd_curr],
        "MACD_signal": [macd_signal_prev, macd_signal_curr],
        "MACD_hist":   [macd_prev - macd_signal_prev, macd_curr - macd_signal_curr],
        "VOL_SMA_20":  [1_000_000,    1_000_000],
        "ATR_14":      [atr,          atr],
    })


def _call(df: pd.DataFrame) -> SignalResult:
    return evaluate_buy_signal(
        df,
        rsi_lower_bound=_RSI_LOWER,
        rsi_upper_bound=_RSI_UPPER,
    )


# ---------------------------------------------------------------------------
# Full confluence — all conditions pass
# ---------------------------------------------------------------------------

def test_all_conditions_pass_triggers_signal():
    result = _call(_make_df())
    assert result.triggered is True


def test_returns_signal_result_type():
    result = _call(_make_df())
    assert isinstance(result, SignalResult)


# ---------------------------------------------------------------------------
# Condition 1 — Trend filter (close > EMA_50)
# ---------------------------------------------------------------------------

def test_close_below_ema50_blocks_signal():
    result = _call(_make_df(close=90.0, ema_50=95.0))
    assert result.triggered is False
    assert result.context["cond_trend"] is False


def test_close_equal_to_ema50_blocks_signal():
    # Condition is strictly greater-than; equal does not pass
    result = _call(_make_df(close=95.0, ema_50=95.0))
    assert result.triggered is False
    assert result.context["cond_trend"] is False


def test_close_just_above_ema50_passes():
    result = _call(_make_df(close=95.01, ema_50=95.0))
    assert result.context["cond_trend"] is True


# ---------------------------------------------------------------------------
# Condition 2 — RSI pullback range [rsi_lower_bound, rsi_upper_bound)
# ---------------------------------------------------------------------------

def test_rsi_below_lower_bound_blocks_signal():
    # RSI < 40 → too oversold, potential larger problem
    result = _call(_make_df(rsi=35.0))
    assert result.triggered is False
    assert result.context["cond_rsi"] is False


def test_rsi_at_lower_bound_passes():
    # RSI == 40 → exactly at lower bound, >= check passes
    result = _call(_make_df(rsi=40.0))
    assert result.context["cond_rsi"] is True


def test_rsi_in_middle_of_range_passes():
    result = _call(_make_df(rsi=47.0))
    assert result.context["cond_rsi"] is True


def test_rsi_at_upper_bound_blocks_signal():
    # RSI == 55 → strict less-than, does not pass
    result = _call(_make_df(rsi=55.0))
    assert result.triggered is False
    assert result.context["cond_rsi"] is False


def test_rsi_above_upper_bound_blocks_signal():
    # RSI > 55 → no real pullback has occurred
    result = _call(_make_df(rsi=60.0))
    assert result.triggered is False
    assert result.context["cond_rsi"] is False


# ---------------------------------------------------------------------------
# Condition 3 — MACD bullish crossover
# ---------------------------------------------------------------------------

def test_macd_already_above_signal_no_crossover_blocks():
    # MACD was already above signal on the previous bar — not a new crossover
    result = _call(_make_df(
        macd_prev=0.3, macd_signal_prev=0.1,
        macd_curr=0.5, macd_signal_curr=0.3,
    ))
    assert result.triggered is False
    assert result.context["cond_macd"] is False


def test_macd_below_signal_on_both_bars_blocks():
    result = _call(_make_df(
        macd_prev=-0.3, macd_signal_prev=0.1,
        macd_curr=-0.1, macd_signal_curr=0.2,
    ))
    assert result.triggered is False
    assert result.context["cond_macd"] is False


def test_bearish_crossover_blocks_signal():
    # MACD was above, now crossed below — bearish
    result = _call(_make_df(
        macd_prev=0.3, macd_signal_prev=0.1,
        macd_curr=0.1, macd_signal_curr=0.3,
    ))
    assert result.triggered is False
    assert result.context["cond_macd"] is False


def test_macd_crossover_exactly_at_zero_line_passes():
    result = _call(_make_df(
        macd_prev=-0.01, macd_signal_prev=0.0,
        macd_curr=0.01,  macd_signal_curr=0.0,
    ))
    assert result.context["cond_macd"] is True


def test_prev_macd_equal_to_signal_counts_as_crossover():
    # prev: MACD == signal (satisfies <=), curr: MACD > signal → valid crossover
    result = _call(_make_df(
        macd_prev=0.2, macd_signal_prev=0.2,
        macd_curr=0.3, macd_signal_curr=0.2,
    ))
    assert result.context["cond_macd"] is True


# ---------------------------------------------------------------------------
# Context dict completeness
# ---------------------------------------------------------------------------

def test_context_contains_all_expected_keys():
    result = _call(_make_df())
    expected_keys = {
        "close", "ema_50",
        "rsi", "rsi_lower_bound", "rsi_upper_bound",
        "macd", "macd_signal", "macd_prev", "macd_signal_prev",
        "atr",
        "cond_trend", "cond_rsi", "cond_macd",
    }
    assert expected_keys.issubset(set(result.context.keys()))


def test_context_cond_flags_reflect_actual_state():
    # Only RSI fails (too high)
    result = _call(_make_df(rsi=60.0))
    assert result.context["cond_trend"] is True
    assert result.context["cond_rsi"]   is False
    assert result.context["cond_macd"]  is True


def test_context_atr_is_populated():
    result = _call(_make_df(atr=3.5))
    assert result.context["atr"] == 3.5


# ---------------------------------------------------------------------------
# NaN handling
# ---------------------------------------------------------------------------

def test_nan_rsi_returns_not_triggered():
    df = _make_df()
    df.loc[df.index[-1], "RSI_14"] = float("nan")
    result = _call(df)
    assert result.triggered is False


def test_nan_atr_returns_not_triggered():
    df = _make_df()
    df.loc[df.index[-1], "ATR_14"] = float("nan")
    result = _call(df)
    assert result.triggered is False


def test_nan_prev_macd_returns_not_triggered():
    df = _make_df()
    df.loc[df.index[-2], "MACD"] = float("nan")
    result = _call(df)
    assert result.triggered is False


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

def test_raises_on_missing_indicator_column():
    df = _make_df().drop(columns=["RSI_14"])
    with pytest.raises(ValueError, match="missing columns"):
        _call(df)


def test_raises_on_single_row_df():
    df = _make_df().iloc[[-1]]
    with pytest.raises(ValueError, match="at least 2 rows"):
        _call(df)
