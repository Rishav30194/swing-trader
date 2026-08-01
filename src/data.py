"""
data.py — Market data fetching via the Alpaca API.

This module is the only place in the codebase that talks to Alpaca's
data endpoint. All other modules receive plain pandas DataFrames and
have no knowledge of where the data came from.

Two public functions:
  - get_historical_bars(symbol, days)  →  DataFrame with ~N rows (one per trading day)
  - get_latest_bar(symbol)             →  DataFrame with exactly 1 row

Column contract (both functions return the same schema):
  timestamp  : datetime64[ns, UTC]  — bar date
  open       : float64
  high       : float64
  low        : float64
  close      : float64
  volume     : float64

Any fetch error is logged and re-raised so the caller can decide
whether to skip that symbol or abort the run.
"""

import logging
import zoneinfo
from datetime import datetime, time as dtime, timedelta, timezone

import pandas as pd
from alpaca.data import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestBarRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import Adjustment, DataFeed

from src.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Client — created once at module import, reused for every request.
#
# StockHistoricalDataClient takes API key + secret and targets the data
# endpoint (data.alpaca.markets) automatically. We don't need to pass a
# base URL here — that's only required for the trading/broker client.
# ---------------------------------------------------------------------------
_client = StockHistoricalDataClient(
    api_key=settings.alpaca_api_key,
    secret_key=settings.alpaca_api_secret,
)

# Columns we expect in every DataFrame we return. Used for validation.
_REQUIRED_COLUMNS = {"open", "high", "low", "close", "volume"}

_ET = zoneinfo.ZoneInfo("America/New_York")

# Regular-session close. A bar dated today is still forming before this time and
# complete after it. See _drop_forming_bar.
_SESSION_CLOSE = dtime(16, 0)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_historical_bars(
    symbol: str,
    days: int = 365,
    *,
    completed_only: bool = False,
) -> pd.DataFrame:
    """
    Fetch daily OHLCV bars for `symbol` going back `days` calendar days.

    Returns a DataFrame indexed by date (one row per trading day) with
    columns: open, high, low, close, volume, timestamp.

    Args:
        symbol: Ticker to fetch.
        days: Calendar days of history. The strategy needs 200 completed bars
            for SMA_200, so callers should request ~365 or more — a shorter
            window leaves slow indicators under-converged rather than absent,
            which is the silent-failure mode that broke the previous strategy.
        completed_only: Drop the final bar when it is today's still-forming
            session. During market hours Alpaca returns a partial bar for the
            current day; evaluating it means acting on an incomplete close and
            never seeing that bar in its final form. Set True for anything that
            makes trading decisions.

    Raises:
        ValueError  — if the returned DataFrame is missing required columns
                      or contains null values in OHLCV columns.
        Exception   — any Alpaca API or network error is logged then re-raised.

    Why split-adjusted prices?
        Comparing a pre-split bar to a post-split bar would make RSI, EMA,
        and MACD calculations meaningless. Adjustment.ALL corrects for both
        splits and dividends so the price series is continuous.
    """
    # Compute the start date in UTC. Alpaca ignores weekends/holidays
    # automatically — requesting a Saturday start just moves to Monday.
    start_dt = datetime.now(timezone.utc) - timedelta(days=days)

    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Day,
        start=start_dt,
        # No explicit `end` → defaults to the most recent available bar.
        adjustment=Adjustment.ALL,   # split + dividend adjusted
        feed=DataFeed.SIP,           # consolidated tape (NYSE + NASDAQ + ARCA)
    )

    logger.info("Fetching %d days of daily bars for %s", days, symbol)

    try:
        bar_set = _client.get_stock_bars(request)
    except Exception:
        logger.exception("Failed to fetch historical bars for %s", symbol)
        raise

    # bar_set.df returns a MultiIndex DataFrame (symbol, timestamp).
    # .loc[symbol] drops the outer symbol level, leaving timestamp as the index.
    df = bar_set.df.loc[symbol].copy()

    # Reset so timestamp becomes a regular column — easier to work with
    # downstream when we need to compare dates or slice by position.
    df = df.reset_index()  # timestamp moves from index → column

    # Rename to our standard column contract (alpaca-py already uses lowercase
    # names, but this makes the contract explicit and safe against SDK changes).
    df = df.rename(columns={
        "timestamp": "timestamp",
        "open": "open",
        "high": "high",
        "low": "low",
        "close": "close",
        "volume": "volume",
    })

    df = _validate_and_clean(df, symbol)

    if completed_only:
        df = _drop_forming_bar(df, symbol)

    logger.info(
        "Fetched %d bars for %s  (%s → %s)",
        len(df),
        symbol,
        df["timestamp"].iloc[0].date(),
        df["timestamp"].iloc[-1].date(),
    )
    return df


def _drop_forming_bar(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """
    Remove the trailing bar only while today's session is still in progress.

    Alpaca includes the current session as a bar from the opening bell, so during
    market hours the last row is a partial day and acting on it means deciding
    against a close that has not happened yet.

    After the close that same bar is complete and must be KEPT. Dropping it would
    make the weekly rebalance decide on the previous day's close, giving live a
    two-bar execution lag where the backtest has one — the live/backtest
    divergence that hid the previous strategy's defect.

    The 16:00 ET cutoff is deliberately not holiday- or early-close-aware: on an
    early-close day the bar is complete sooner, so keeping it after 16:00 is
    still correct, just later than strictly necessary.
    """
    now_et = datetime.now(timezone.utc).astimezone(_ET)
    last_bar_date = df["timestamp"].iloc[-1].date()

    if last_bar_date != now_et.date() or now_et.time() >= _SESSION_CLOSE:
        return df

    df = df.iloc[:-1].copy()
    logger.info("%s: dropped today's forming bar (%s, session still open)",
                symbol, last_bar_date)
    if df.empty:
        raise ValueError(f"No completed bars for {symbol} after dropping today's.")
    return df


def get_latest_bar(symbol: str) -> pd.DataFrame:
    """
    Fetch the single most recent completed daily bar for `symbol`.

    Returns a one-row DataFrame with the same column schema as
    get_historical_bars(). Use this during live scanning to get today's
    (or last trading day's) OHLCV snapshot without pulling a full year.

    Note: Alpaca returns the *latest completed* bar, not an intraday
    snapshot. During market hours this is yesterday's close; after 4pm ET
    it is today's close.

    Raises:
        Exception — any Alpaca API or network error is logged then re-raised.
    """
    request = StockLatestBarRequest(
        symbol_or_symbols=symbol,
        feed=DataFeed.SIP,
    )

    logger.info("Fetching latest bar for %s", symbol)

    try:
        latest = _client.get_stock_latest_bar(request)
    except Exception:
        logger.exception("Failed to fetch latest bar for %s", symbol)
        raise

    # latest[symbol] is a Bar object. Convert to a single-row DataFrame
    # using its __dict__ so we get a consistent column structure.
    bar = latest[symbol]
    df = pd.DataFrame([{
        "timestamp": bar.timestamp,
        "open":      bar.open,
        "high":      bar.high,
        "low":       bar.low,
        "close":     bar.close,
        "volume":    float(bar.volume),
    }])

    df = _validate_and_clean(df, symbol)

    logger.info(
        "Latest bar for %s: close=%.2f  volume=%.0f  date=%s",
        symbol,
        df["close"].iloc[0],
        df["volume"].iloc[0],
        df["timestamp"].iloc[0].date(),
    )
    return df


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _validate_and_clean(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """
    Validate that the DataFrame meets our column contract and has no nulls
    in the OHLCV columns. Raises ValueError if either check fails.

    This is the firewall between Alpaca's output and our strategy code.
    A DataFrame with null values in the close column would silently corrupt
    RSI, EMA, and MACD calculations downstream.
    """
    if df.empty:
        raise ValueError(f"No bars returned for {symbol}. Market may be closed or symbol invalid.")

    # Check that all required columns are present.
    missing = _REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(
            f"DataFrame for {symbol} is missing expected columns: {missing}"
        )

    # Check for nulls in OHLCV columns only.
    # Allowing nulls in auxiliary columns (like trade_count) is fine,
    # but a null close price would break every downstream calculation.
    null_counts = df[list(_REQUIRED_COLUMNS)].isnull().sum()
    cols_with_nulls = null_counts[null_counts > 0]
    if not cols_with_nulls.empty:
        raise ValueError(
            f"Null values found in {symbol} OHLCV data: {cols_with_nulls.to_dict()}"
        )

    # Ensure numeric types — Alpaca SDK should already return floats,
    # but an explicit cast prevents surprises if the schema changes.
    for col in _REQUIRED_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="raise")

    # Keep only the columns we care about, in a consistent order.
    # This drops any extra columns Alpaca might add in future SDK versions.
    df = df[["timestamp", "open", "high", "low", "close", "volume"]].copy()

    # Sort by date ascending so iloc[0] is always the oldest bar.
    df = df.sort_values("timestamp").reset_index(drop=True)

    return df
