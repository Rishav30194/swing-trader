"""
Unit tests for src/indicators.py.

Only SMA_200 remains — the retired signal strategy's other columns were removed
once nothing read them. These tests cover the value itself, the warm-up
boundary, and the input guards, since a wrong or silently-NaN SMA_200 decides
whether real money is in the market.

All tests use synthetic DataFrames — no network calls, no Alpaca dependency.
"""

import numpy as np
import pandas as pd
import pytest

from src.indicators import MIN_BARS, MIN_BARS_FOR_STRATEGY, compute_indicators


def _ohlcv(closes: list[float]) -> pd.DataFrame:
    n = len(closes)
    return pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01", periods=n, freq="B", tz="UTC"),
        "open": closes,
        "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes],
        "close": closes,
        "volume": [1_000_000.0] * n,
    })


# ---------------------------------------------------------------------------
# Output shape and columns
# ---------------------------------------------------------------------------

def test_appends_sma_200():
    assert "SMA_200" in compute_indicators(_ohlcv([100.0] * 250)).columns


def test_original_columns_preserved():
    out = compute_indicators(_ohlcv([100.0] * 250))
    for col in ("timestamp", "open", "high", "low", "close", "volume"):
        assert col in out.columns


def test_row_count_unchanged():
    df = _ohlcv([100.0] * 250)
    assert len(compute_indicators(df)) == len(df)


def test_does_not_mutate_input():
    df = _ohlcv([100.0] * 250)
    compute_indicators(df)
    assert "SMA_200" not in df.columns


def test_retired_columns_are_gone():
    """Nothing read them; recomputing them weekly for every symbol was waste."""
    out = compute_indicators(_ohlcv([100.0] * 250))
    for col in ("RSI_14", "EMA_21", "EMA_50", "MACD", "ATR_14", "OBV", "ADX_14"):
        assert col not in out.columns


# ---------------------------------------------------------------------------
# Value correctness
# ---------------------------------------------------------------------------

def test_sma_200_equals_mean_of_last_200_closes():
    closes = list(np.linspace(100.0, 300.0, 250))
    out = compute_indicators(_ohlcv(closes))
    assert out["SMA_200"].iloc[-1] == pytest.approx(float(np.mean(closes[-200:])))


def test_sma_200_is_flat_for_constant_prices():
    out = compute_indicators(_ohlcv([150.0] * 250))
    assert out["SMA_200"].iloc[-1] == pytest.approx(150.0)


def test_sma_200_lags_a_rising_price():
    """The regime filter only works because the average trails the price."""
    last = compute_indicators(_ohlcv(list(np.linspace(100.0, 300.0, 250)))).iloc[-1]
    assert last["close"] > last["SMA_200"]


def test_sma_200_sits_above_a_falling_price():
    last = compute_indicators(_ohlcv(list(np.linspace(300.0, 100.0, 250)))).iloc[-1]
    assert last["close"] < last["SMA_200"]


# ---------------------------------------------------------------------------
# Warm-up boundary
# ---------------------------------------------------------------------------

def test_sma_200_first_value_lands_on_bar_200():
    out = compute_indicators(_ohlcv([100.0] * 250))
    assert pd.isna(out["SMA_200"].iloc[198])
    assert not pd.isna(out["SMA_200"].iloc[199])


def test_frame_shorter_than_strategy_minimum_yields_nan_last_row():
    """main.py must reject these rather than treat NaN as 'stay flat'."""
    out = compute_indicators(_ohlcv([100.0] * (MIN_BARS_FOR_STRATEGY - 1)))
    assert pd.isna(out["SMA_200"].iloc[-1])


def test_exactly_the_strategy_minimum_produces_a_value():
    out = compute_indicators(_ohlcv([100.0] * MIN_BARS_FOR_STRATEGY))
    assert not pd.isna(out["SMA_200"].iloc[-1])


# ---------------------------------------------------------------------------
# Input guards
# ---------------------------------------------------------------------------

def test_rejects_missing_columns():
    df = _ohlcv([100.0] * 250).drop(columns=["volume"])
    with pytest.raises(ValueError, match="missing required columns"):
        compute_indicators(df)


def test_rejects_frames_below_min_bars():
    with pytest.raises(ValueError, match=f"at least {MIN_BARS} rows"):
        compute_indicators(_ohlcv([100.0] * (MIN_BARS - 1)))


def test_accepts_exactly_min_bars():
    compute_indicators(_ohlcv([100.0] * MIN_BARS))
