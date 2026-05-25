"""
indicators.py — Technical indicator computation.

Single public function: compute_indicators(df) takes a raw OHLCV DataFrame
(as returned by data.py) and returns the same DataFrame with indicator columns
appended. All computation is done by pandas-ta.

Column contract added by this module:
  RSI_14         : float  — Relative Strength Index, 14-period
  EMA_21         : float  — Exponential Moving Average, 21-period
  EMA_50         : float  — Exponential Moving Average, 50-period
  MACD           : float  — MACD line (12-period EMA − 26-period EMA)
  MACD_signal    : float  — Signal line (9-period EMA of MACD)
  MACD_hist      : float  — Histogram (MACD − signal)
  VOL_SMA_20     : float  — Simple Moving Average of volume, 20-period
  ATR_14         : float  — Average True Range, 14-period
  ADX_14         : float  — Average Directional Index, 14-period (trend strength 0–100)
  DMP_14         : float  — Plus Directional Indicator (+DI, bullish pressure)
  DMN_14         : float  — Minus Directional Indicator (-DI, bearish pressure)
  STOCHk_14_3_3  : float  — Stochastic %K, 14-period smoothed by 3
  STOCHd_14_3_3  : float  — Stochastic %D, 3-period SMA of %K (signal line)
  OBV            : float  — On-Balance Volume (cumulative volume trend)

The first N rows of each indicator will be NaN (warm-up period). Callers
must ensure the DataFrame is long enough — at least 60 rows is recommended
so that EMA_50 has fully converged before the last row is evaluated.
"""

import logging

import pandas as pd
import pandas_ta as ta

logger = logging.getLogger(__name__)

# Minimum bars needed for the slowest indicator (EMA_50) to produce a value.
MIN_BARS = 60


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Append all strategy indicators to a OHLCV DataFrame.

    Args:
        df: DataFrame with columns [timestamp, open, high, low, close, volume]
            as returned by data.get_historical_bars(). Must have at least
            MIN_BARS rows for all indicators to converge on the final row.

    Returns:
        A new DataFrame with the original columns plus all indicator columns.
        The original DataFrame is not modified.

    Raises:
        ValueError: if required input columns are missing or fewer than
                    MIN_BARS rows are provided.
    """
    _validate_input(df)

    out = df.copy()

    out["RSI_14"] = ta.rsi(out["close"], length=14)

    out["EMA_21"] = ta.ema(out["close"], length=21)
    out["EMA_50"] = ta.ema(out["close"], length=50)

    macd_df = ta.macd(out["close"], fast=12, slow=26, signal=9)
    out["MACD"]        = macd_df["MACD_12_26_9"]
    out["MACD_signal"] = macd_df["MACDs_12_26_9"]
    out["MACD_hist"]   = macd_df["MACDh_12_26_9"]

    out["VOL_SMA_20"] = ta.sma(out["volume"], length=20)

    out["ATR_14"] = ta.atr(out["high"], out["low"], out["close"], length=14)

    adx_df = ta.adx(out["high"], out["low"], out["close"], length=14)
    out["ADX_14"] = adx_df["ADX_14"]
    out["DMP_14"] = adx_df["DMP_14"]
    out["DMN_14"] = adx_df["DMN_14"]

    stoch_df = ta.stoch(out["high"], out["low"], out["close"], k=14, d=3, smooth_k=3)
    out["STOCHk_14_3_3"] = stoch_df["STOCHk_14_3_3"]
    out["STOCHd_14_3_3"] = stoch_df["STOCHd_14_3_3"]

    out["OBV"] = ta.obv(out["close"], out["volume"])

    logger.debug(
        "compute_indicators: %d rows, last bar %s — RSI=%.1f ATR=%.2f",
        len(out),
        out["timestamp"].iloc[-1].date() if "timestamp" in out.columns else "?",
        out["RSI_14"].iloc[-1] if not pd.isna(out["RSI_14"].iloc[-1]) else float("nan"),
        out["ATR_14"].iloc[-1] if not pd.isna(out["ATR_14"].iloc[-1]) else float("nan"),
    )

    return out


def _validate_input(df: pd.DataFrame) -> None:
    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"compute_indicators: missing required columns: {missing}")

    if len(df) < MIN_BARS:
        raise ValueError(
            f"compute_indicators: need at least {MIN_BARS} rows for indicators "
            f"to converge, got {len(df)}."
        )
