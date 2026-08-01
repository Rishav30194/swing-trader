"""
indicators.py — Technical indicator computation.

Single public function: compute_indicators(df) takes a raw OHLCV DataFrame
(as returned by data.py) and returns the same DataFrame with indicator columns
appended.

Column contract added by this module:
  SMA_200 : float — Simple Moving Average, 200-period (the regime filter)

A plain pandas rolling mean, deliberately: pulling in pandas-ta for this single
call would drag numba and llvmlite along with it. Values are identical to 1e-13.

Only SMA_200 is computed. The retired signal strategy also produced RSI, EMA_21,
EMA_50, MACD, VOL_SMA_20, ATR_14, ADX_14, Stochastic and OBV columns; nothing
read them after the strategy changed, so they were removed rather than
recalculated for every symbol every week. Git history has them if a future
strategy needs them back.

The first 199 rows of SMA_200 will be NaN (warm-up). Callers must ensure the
DataFrame is long enough — see MIN_BARS_FOR_STRATEGY.
"""

import logging

import pandas as pd

logger = logging.getLogger(__name__)

# Minimum bars for compute_indicators() to run at all. Short frames still
# produce every column; the slow ones are simply NaN in the warm-up region.
MIN_BARS = 60

# Bars required before SMA_200 — and therefore the regime filter — is usable.
# main.py validates against this and refuses to trade a symbol without it,
# rather than silently treating a NaN regime as "stay flat".
MIN_BARS_FOR_STRATEGY = 200


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Append the strategy's indicator columns to an OHLCV DataFrame.

    Args:
        df: DataFrame with columns [timestamp, open, high, low, close, volume]
            as returned by data.get_historical_bars(). Must have at least
            MIN_BARS rows; MIN_BARS_FOR_STRATEGY rows are needed before
            SMA_200 carries a value on the final row.

    Returns:
        A new DataFrame with the original columns plus SMA_200.
        The original DataFrame is not modified.

    Raises:
        ValueError: if required input columns are missing or fewer than
                    MIN_BARS rows are provided.
    """
    _validate_input(df)

    out = df.copy()
    out["SMA_200"] = out["close"].rolling(window=200).mean()

    logger.debug(
        "compute_indicators: %d rows, last bar %s — SMA_200=%s",
        len(out),
        out["timestamp"].iloc[-1].date() if "timestamp" in out.columns else "?",
        out["SMA_200"].iloc[-1],
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
