"""
Unit tests for src/indicators.py.

All tests use synthetic DataFrames — no network calls, no Alpaca dependency.
The fixtures produce deterministic price series so assertions can be exact
(within floating-point tolerance).
"""

import numpy as np
import pandas as pd
import pytest

from src.indicators import compute_indicators, MIN_BARS


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_df(n: int = 100, *, seed: int = 42) -> pd.DataFrame:
    """
    Build a synthetic OHLCV DataFrame with `n` rows.

    Uses a seeded random walk on close prices so results are reproducible.
    Open, high, low are derived from close so the OHLCV relationships hold.
    """
    rng = np.random.default_rng(seed)
    close = 100.0 + np.cumsum(rng.normal(0, 1, n))
    close = np.maximum(close, 1.0)  # no negative prices

    noise = rng.uniform(0.5, 2.0, n)
    high   = close + noise
    low    = close - noise
    open_  = close + rng.normal(0, 0.5, n)
    volume = rng.integers(500_000, 5_000_000, n).astype(float)

    dates = pd.date_range("2023-01-01", periods=n, freq="B", tz="UTC")

    return pd.DataFrame({
        "timestamp": dates,
        "open":   open_,
        "high":   high,
        "low":    low,
        "close":  close,
        "volume": volume,
    })


# ---------------------------------------------------------------------------
# Output shape and columns
# ---------------------------------------------------------------------------

def test_returns_all_indicator_columns():
    df = _make_df(100)
    out = compute_indicators(df)
    expected_cols = {
        "RSI_14", "EMA_21", "EMA_50",
        "MACD", "MACD_signal", "MACD_hist",
        "VOL_SMA_20", "ATR_14",
        "ADX_14", "DMP_14", "DMN_14",
        "STOCHk_14_3_3", "STOCHd_14_3_3",
        "OBV",
    }
    assert expected_cols.issubset(set(out.columns))


def test_original_columns_preserved():
    df = _make_df(100)
    out = compute_indicators(df)
    for col in ["timestamp", "open", "high", "low", "close", "volume"]:
        assert col in out.columns


def test_row_count_unchanged():
    df = _make_df(100)
    out = compute_indicators(df)
    assert len(out) == len(df)


def test_does_not_mutate_input():
    df = _make_df(100)
    original_cols = list(df.columns)
    compute_indicators(df)
    assert list(df.columns) == original_cols


# ---------------------------------------------------------------------------
# RSI
# ---------------------------------------------------------------------------

def test_rsi_range():
    """RSI must always be in [0, 100] where defined."""
    out = compute_indicators(_make_df(100))
    valid = out["RSI_14"].dropna()
    assert (valid >= 0).all() and (valid <= 100).all()


def test_rsi_falls_on_consecutive_down_days():
    """RSI should decrease on a long streak of falling prices."""
    n = 100
    close = np.linspace(200, 100, n)  # steady decline
    df = pd.DataFrame({
        "timestamp": pd.date_range("2023-01-01", periods=n, freq="B", tz="UTC"),
        "open":   close,
        "high":   close + 1,
        "low":    close - 1,
        "close":  close,
        "volume": np.full(n, 1_000_000.0),
    })
    out = compute_indicators(df)
    rsi = out["RSI_14"].dropna()
    # On a pure downtrend RSI should be well below 50
    assert rsi.iloc[-1] < 40


def test_rsi_rises_on_consecutive_up_days():
    """RSI should be elevated on a long streak of rising prices."""
    n = 100
    close = np.linspace(100, 200, n)  # steady rise
    df = pd.DataFrame({
        "timestamp": pd.date_range("2023-01-01", periods=n, freq="B", tz="UTC"),
        "open":   close,
        "high":   close + 1,
        "low":    close - 1,
        "close":  close,
        "volume": np.full(n, 1_000_000.0),
    })
    out = compute_indicators(df)
    rsi = out["RSI_14"].dropna()
    assert rsi.iloc[-1] > 60


# ---------------------------------------------------------------------------
# EMA
# ---------------------------------------------------------------------------

def test_ema21_tracks_price_in_uptrend():
    """In a steady uptrend, EMA_21 should be below close (lagging)."""
    n = 100
    close = np.linspace(100, 200, n)
    df = pd.DataFrame({
        "timestamp": pd.date_range("2023-01-01", periods=n, freq="B", tz="UTC"),
        "open":   close, "high": close + 1, "low": close - 1,
        "close":  close, "volume": np.full(n, 1_000_000.0),
    })
    out = compute_indicators(df)
    assert out["EMA_21"].iloc[-1] < out["close"].iloc[-1]


def test_ema50_slower_than_ema21():
    """EMA_50 should lag further behind price than EMA_21 in an uptrend."""
    n = 100
    close = np.linspace(100, 200, n)
    df = pd.DataFrame({
        "timestamp": pd.date_range("2023-01-01", periods=n, freq="B", tz="UTC"),
        "open":   close, "high": close + 1, "low": close - 1,
        "close":  close, "volume": np.full(n, 1_000_000.0),
    })
    out = compute_indicators(df)
    # In uptrend: close > EMA_21 > EMA_50
    assert out["EMA_21"].iloc[-1] > out["EMA_50"].iloc[-1]


# ---------------------------------------------------------------------------
# MACD
# ---------------------------------------------------------------------------

def test_macd_positive_in_uptrend():
    """MACD line (fast EMA − slow EMA) should be positive in a steady uptrend."""
    n = 100
    close = np.linspace(100, 200, n)
    df = pd.DataFrame({
        "timestamp": pd.date_range("2023-01-01", periods=n, freq="B", tz="UTC"),
        "open":   close, "high": close + 1, "low": close - 1,
        "close":  close, "volume": np.full(n, 1_000_000.0),
    })
    out = compute_indicators(df)
    assert out["MACD"].iloc[-1] > 0


def test_macd_histogram_equals_macd_minus_signal():
    """MACD_hist must equal MACD − MACD_signal on every non-NaN row."""
    out = compute_indicators(_make_df(100))
    valid = out.dropna(subset=["MACD", "MACD_signal", "MACD_hist"])
    diff = (valid["MACD"] - valid["MACD_signal"] - valid["MACD_hist"]).abs()
    assert (diff < 1e-8).all()


# ---------------------------------------------------------------------------
# Volume SMA
# ---------------------------------------------------------------------------

def test_vol_sma20_is_mean_of_last_20_volumes():
    """VOL_SMA_20 on the last row should equal the mean of the last 20 volumes."""
    df = _make_df(100)
    out = compute_indicators(df)
    expected = df["volume"].iloc[-20:].mean()
    assert abs(out["VOL_SMA_20"].iloc[-1] - expected) < 1e-6


# ---------------------------------------------------------------------------
# ATR
# ---------------------------------------------------------------------------

def test_atr_positive():
    """ATR must always be positive where defined."""
    out = compute_indicators(_make_df(100))
    valid = out["ATR_14"].dropna()
    assert (valid > 0).all()


def test_atr_higher_with_wider_candles():
    """ATR should be larger when candle ranges are wider."""
    n = 100
    close = np.full(n, 100.0)
    base = pd.date_range("2023-01-01", periods=n, freq="B", tz="UTC")

    narrow = pd.DataFrame({
        "timestamp": base, "open": close, "close": close,
        "high": close + 0.5, "low": close - 0.5,
        "volume": np.full(n, 1_000_000.0),
    })
    wide = pd.DataFrame({
        "timestamp": base, "open": close, "close": close,
        "high": close + 5.0, "low": close - 5.0,
        "volume": np.full(n, 1_000_000.0),
    })

    atr_narrow = compute_indicators(narrow)["ATR_14"].iloc[-1]
    atr_wide   = compute_indicators(wide)["ATR_14"].iloc[-1]
    assert atr_wide > atr_narrow


# ---------------------------------------------------------------------------
# ADX
# ---------------------------------------------------------------------------

def test_adx_range():
    """ADX must always be in [0, 100] where defined."""
    out = compute_indicators(_make_df(100))
    valid = out["ADX_14"].dropna()
    assert (valid >= 0).all() and (valid <= 100).all()


def test_dmp_greater_than_dmn_in_uptrend():
    """In a sustained uptrend, +DI (DMP) should exceed -DI (DMN)."""
    n = 100
    close = np.linspace(100, 200, n)
    df = pd.DataFrame({
        "timestamp": pd.date_range("2023-01-01", periods=n, freq="B", tz="UTC"),
        "open": close, "high": close + 1, "low": close - 1,
        "close": close, "volume": np.full(n, 1_000_000.0),
    })
    out = compute_indicators(df)
    assert out["DMP_14"].iloc[-1] > out["DMN_14"].iloc[-1]


def test_dmn_greater_than_dmp_in_downtrend():
    """In a sustained downtrend, -DI (DMN) should exceed +DI (DMP)."""
    n = 100
    close = np.linspace(200, 100, n)
    df = pd.DataFrame({
        "timestamp": pd.date_range("2023-01-01", periods=n, freq="B", tz="UTC"),
        "open": close, "high": close + 1, "low": close - 1,
        "close": close, "volume": np.full(n, 1_000_000.0),
    })
    out = compute_indicators(df)
    assert out["DMN_14"].iloc[-1] > out["DMP_14"].iloc[-1]


def test_adx_higher_in_strong_trend_than_choppy():
    """ADX should be higher after a clean trend than after sideways price action."""
    n = 100
    dates = pd.date_range("2023-01-01", periods=n, freq="B", tz="UTC")

    trending = np.linspace(100, 200, n)
    choppy   = 100 + np.sin(np.linspace(0, 20 * np.pi, n)) * 5  # oscillates ±5

    def _df(close):
        return pd.DataFrame({
            "timestamp": dates,
            "open": close, "high": close + 1, "low": close - 1,
            "close": close, "volume": np.full(n, 1_000_000.0),
        })

    adx_trend  = compute_indicators(_df(trending))["ADX_14"].iloc[-1]
    adx_choppy = compute_indicators(_df(choppy))["ADX_14"].iloc[-1]
    assert adx_trend > adx_choppy


# ---------------------------------------------------------------------------
# Stochastic
# ---------------------------------------------------------------------------

def test_stoch_range():
    """Stochastic %K and %D must always be in [0, 100] where defined."""
    out = compute_indicators(_make_df(100))
    for col in ("STOCHk_14_3_3", "STOCHd_14_3_3"):
        valid = out[col].dropna()
        assert (valid >= 0).all() and (valid <= 100).all()


def test_stoch_low_at_end_of_downtrend():
    """After a sustained decline price is near the period low, so %K should be low."""
    n = 100
    close = np.linspace(200, 100, n)
    df = pd.DataFrame({
        "timestamp": pd.date_range("2023-01-01", periods=n, freq="B", tz="UTC"),
        "open": close, "high": close + 0.5, "low": close - 0.5,
        "close": close, "volume": np.full(n, 1_000_000.0),
    })
    out = compute_indicators(df)
    assert out["STOCHk_14_3_3"].iloc[-1] < 20


def test_stoch_high_at_end_of_uptrend():
    """After a sustained rise price is near the period high, so %K should be high."""
    n = 100
    close = np.linspace(100, 200, n)
    df = pd.DataFrame({
        "timestamp": pd.date_range("2023-01-01", periods=n, freq="B", tz="UTC"),
        "open": close, "high": close + 0.5, "low": close - 0.5,
        "close": close, "volume": np.full(n, 1_000_000.0),
    })
    out = compute_indicators(df)
    assert out["STOCHk_14_3_3"].iloc[-1] > 80


def test_stochd_is_smoother_than_stochk():
    """STOCHd (signal) should have lower variance than STOCHk (raw)."""
    out = compute_indicators(_make_df(100))
    k_std = out["STOCHk_14_3_3"].dropna().std()
    d_std = out["STOCHd_14_3_3"].dropna().std()
    assert d_std <= k_std


# ---------------------------------------------------------------------------
# OBV
# ---------------------------------------------------------------------------

def test_obv_increases_on_consecutive_up_days():
    """Every day closes higher → volume is added → OBV is monotonically increasing."""
    n = 100
    close = np.linspace(100, 200, n)
    df = pd.DataFrame({
        "timestamp": pd.date_range("2023-01-01", periods=n, freq="B", tz="UTC"),
        "open": close, "high": close + 1, "low": close - 1,
        "close": close, "volume": np.full(n, 1_000_000.0),
    })
    out = compute_indicators(df)
    obv_diff = out["OBV"].diff().dropna()
    assert (obv_diff > 0).all()


def test_obv_decreases_on_consecutive_down_days():
    """Every day closes lower → volume is subtracted → OBV is monotonically decreasing."""
    n = 100
    close = np.linspace(200, 100, n)
    df = pd.DataFrame({
        "timestamp": pd.date_range("2023-01-01", periods=n, freq="B", tz="UTC"),
        "open": close, "high": close + 1, "low": close - 1,
        "close": close, "volume": np.full(n, 1_000_000.0),
    })
    out = compute_indicators(df)
    obv_diff = out["OBV"].diff().dropna()
    assert (obv_diff < 0).all()


def test_obv_magnitude_reflects_volume():
    """OBV change per bar should equal the bar's volume on a pure up day."""
    n = 100
    close = np.linspace(100, 200, n)
    volume = np.full(n, 500_000.0)
    df = pd.DataFrame({
        "timestamp": pd.date_range("2023-01-01", periods=n, freq="B", tz="UTC"),
        "open": close, "high": close + 1, "low": close - 1,
        "close": close, "volume": volume,
    })
    out = compute_indicators(df)
    # Every diff should equal exactly the per-bar volume (500_000)
    obv_diff = out["OBV"].diff().dropna()
    assert (obv_diff == 500_000.0).all()


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------

def test_raises_on_missing_column():
    df = _make_df(100).drop(columns=["volume"])
    with pytest.raises(ValueError, match="missing required columns"):
        compute_indicators(df)


def test_raises_on_too_few_rows():
    df = _make_df(MIN_BARS - 1)
    with pytest.raises(ValueError, match="at least"):
        compute_indicators(df)


def test_exactly_min_bars_is_accepted():
    df = _make_df(MIN_BARS)
    out = compute_indicators(df)
    assert len(out) == MIN_BARS
