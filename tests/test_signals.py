"""
Unit tests for src/signals.py.

All tests use synthetic DataFrames with hand-crafted indicator values.
No network calls, no .env dependency.

Default fixture: all four conditions pass. Each test group then isolates
one condition to confirm it can independently block the signal.
"""

import pandas as pd
import pytest

from src.signals import evaluate_buy_signal, SignalResult

# Default thresholds matching the documented strategy
_RSI_THRESH  = 45.0
_EMA_PROX    = 0.02   # 2%
_VOL_MULT    = 1.5


# ---------------------------------------------------------------------------
# Fixture helper
# ---------------------------------------------------------------------------

def _make_df(
    *,
    # Condition 1 — RSI
    rsi: float = 40.0,           # < 45 → passes
    # Condition 2 — EMA proximity
    close: float = 100.0,
    ema_21: float = 101.0,       # 1% away → passes
    # Condition 3 — MACD crossover
    macd_curr: float = 0.5,
    macd_signal_curr: float = 0.3,   # curr > signal → above
    macd_prev: float = -0.1,
    macd_signal_prev: float = 0.0,   # prev <= signal → was below → crossover
    # Condition 4 — volume spike
    volume: float = 2_000_000.0,
    vol_sma: float = 1_000_000.0,    # ratio = 2.0 → passes at 1.5x
    # Other required columns
    atr: float = 2.0,
) -> pd.DataFrame:
    """
    Build a 2-row DataFrame with explicit indicator values.
    Row 0 = previous bar, Row 1 = current bar.
    By default all four buy-signal conditions are satisfied on the current bar.
    """
    dates = pd.date_range("2024-01-01", periods=2, freq="B", tz="UTC")
    return pd.DataFrame({
        "timestamp":   dates,
        "open":        [close - 0.5, close - 0.5],
        "high":        [close + 1.0, close + 1.0],
        "low":         [close - 1.0, close - 1.0],
        "close":       [close,       close],
        "volume":      [volume,      volume],
        "RSI_14":      [rsi,         rsi],
        "EMA_21":      [ema_21,      ema_21],
        "EMA_50":      [ema_21 * 0.98, ema_21 * 0.98],
        "MACD":        [macd_prev,   macd_curr],
        "MACD_signal": [macd_signal_prev, macd_signal_curr],
        "MACD_hist":   [macd_prev - macd_signal_prev, macd_curr - macd_signal_curr],
        "VOL_SMA_20":  [vol_sma,     vol_sma],
        "ATR_14":      [atr,         atr],
    })


def _call(df: pd.DataFrame) -> SignalResult:
    return evaluate_buy_signal(
        df,
        rsi_threshold=_RSI_THRESH,
        ema_proximity_pct=_EMA_PROX,
        volume_spike_multiplier=_VOL_MULT,
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
# Condition 1 — RSI
# ---------------------------------------------------------------------------

def test_rsi_above_threshold_blocks_signal():
    result = _call(_make_df(rsi=50.0))   # 50 >= 45
    assert result.triggered is False
    assert result.context["cond_rsi"] is False


def test_rsi_exactly_at_threshold_blocks_signal():
    # Condition is strict less-than: RSI must be < threshold, not <=
    result = _call(_make_df(rsi=45.0))
    assert result.triggered is False
    assert result.context["cond_rsi"] is False


def test_rsi_just_below_threshold_passes():
    result = _call(_make_df(rsi=44.9))
    assert result.context["cond_rsi"] is True


# ---------------------------------------------------------------------------
# Condition 2 — EMA proximity
# ---------------------------------------------------------------------------

def test_price_too_far_above_ema_blocks_signal():
    # close=100, ema_21=96 → distance = 4/96 ≈ 4.2% > 2%
    result = _call(_make_df(close=100.0, ema_21=96.0))
    assert result.triggered is False
    assert result.context["cond_ema"] is False


def test_price_too_far_below_ema_blocks_signal():
    # close=100, ema_21=104 → distance = 4/104 ≈ 3.8% > 2%
    result = _call(_make_df(close=100.0, ema_21=104.0))
    assert result.triggered is False
    assert result.context["cond_ema"] is False


def test_price_exactly_at_ema_passes():
    result = _call(_make_df(close=100.0, ema_21=100.0))
    assert result.context["cond_ema"] is True


def test_price_at_boundary_passes():
    # close=100, ema_21=98.04 → distance ≈ 2.0% (just within)
    result = _call(_make_df(close=100.0, ema_21=98.04))
    assert result.context["cond_ema"] is True


# ---------------------------------------------------------------------------
# Condition 3 — MACD bullish crossover
# ---------------------------------------------------------------------------

def test_macd_already_above_signal_no_crossover_blocks():
    # MACD was already above signal on the previous bar — not a new crossover
    result = _call(_make_df(
        macd_prev=0.3, macd_signal_prev=0.1,   # prev: above
        macd_curr=0.5, macd_signal_curr=0.3,   # curr: still above
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
    # Crossover from negative to positive exactly
    result = _call(_make_df(
        macd_prev=-0.01, macd_signal_prev=0.0,
        macd_curr=0.01,  macd_signal_curr=0.0,
    ))
    assert result.context["cond_macd"] is True


def test_prev_macd_equal_to_signal_counts_as_crossover():
    # prev: MACD == signal (<=), curr: MACD > signal → valid crossover
    result = _call(_make_df(
        macd_prev=0.2, macd_signal_prev=0.2,
        macd_curr=0.3, macd_signal_curr=0.2,
    ))
    assert result.context["cond_macd"] is True


# ---------------------------------------------------------------------------
# Condition 4 — Volume spike
# ---------------------------------------------------------------------------

def test_volume_below_multiplier_blocks_signal():
    # volume = 1.4x SMA → below 1.5x threshold
    result = _call(_make_df(volume=1_400_000.0, vol_sma=1_000_000.0))
    assert result.triggered is False
    assert result.context["cond_volume"] is False


def test_volume_exactly_at_multiplier_passes():
    # volume = exactly 1.5x SMA
    result = _call(_make_df(volume=1_500_000.0, vol_sma=1_000_000.0))
    assert result.context["cond_volume"] is True


def test_volume_above_multiplier_passes():
    result = _call(_make_df(volume=3_000_000.0, vol_sma=1_000_000.0))
    assert result.context["cond_volume"] is True


# ---------------------------------------------------------------------------
# Context dict completeness
# ---------------------------------------------------------------------------

def test_context_contains_all_expected_keys():
    result = _call(_make_df())
    expected_keys = {
        "close", "ema_21", "ema_dist_pct", "ema_proximity_max_pct",
        "rsi", "rsi_threshold",
        "macd", "macd_signal", "macd_prev", "macd_signal_prev",
        "volume", "vol_sma_20", "volume_ratio", "volume_spike_min",
        "atr",
        "cond_rsi", "cond_ema", "cond_macd", "cond_volume",
    }
    assert expected_keys.issubset(set(result.context.keys()))


def test_context_cond_flags_reflect_actual_state():
    # Only RSI fails
    result = _call(_make_df(rsi=50.0))
    assert result.context["cond_rsi"]    is False
    assert result.context["cond_ema"]    is True
    assert result.context["cond_macd"]   is True
    assert result.context["cond_volume"] is True


def test_context_volume_ratio_is_correct():
    result = _call(_make_df(volume=2_000_000.0, vol_sma=1_000_000.0))
    assert result.context["volume_ratio"] == 2.0


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
