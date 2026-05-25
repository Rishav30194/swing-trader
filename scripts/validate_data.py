"""
validate_data.py — Phase 1 gate check for the data pipeline.

Run this script manually after setting up your .env file to confirm
that the Alpaca connection works and all four symbols return clean,
usable historical data before moving on to Phase 2.

Usage (from the project root):
    python scripts/validate_data.py

What it checks:
  1. Each symbol returns at least ~200 trading-day bars over the past year
     (markets are open ~252 days/year; we allow some tolerance for holidays)
  2. No null values in any OHLCV column
  3. No zero or negative prices/volumes (data corruption check)
  4. No gaps larger than 5 calendar days between consecutive bars
     (catches missing weeks of data)
  5. The most recent bar is within the last 5 calendar days
     (confirms we're getting current data, not stale cache)

Exit codes:
    0 — all symbols passed every check
    1 — one or more symbols failed; details printed to stdout
"""

import sys
import os
import logging
from datetime import datetime, timedelta, timezone

# Add the project root (one level up from scripts/) to sys.path so that
# `from src.config import settings` resolves correctly regardless of
# which directory you run this script from.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---------------------------------------------------------------------------
# Minimal logging setup so INFO messages from data.py are visible when
# running this script directly. The main app configures logging more
# fully in main.py; here we just need to see fetch progress.
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)-8s %(name)s: %(message)s",
)

# Silence the verbose httpx/httpcore logs that alpaca-py uses internally.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

from src.config import settings
from src.data import get_historical_bars

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

# A full trading year is ~252 days. We fetch 365 calendar days which
# typically yields 250–252 bars. Allow down to 200 to tolerate extended
# holiday periods or symbols that were halted briefly.
MIN_BARS = 200

# Warn if the latest bar is older than this (stale data indicator).
MAX_STALENESS_DAYS = 5

# Gaps larger than this between consecutive bars are suspicious.
# 5 calendar days covers a long weekend (Friday close → Tuesday open = 4 days).
MAX_GAP_DAYS = 5


def validate_symbol(symbol: str) -> tuple[bool, list[str]]:
    """
    Run all checks for one symbol. Returns (passed, list_of_issues).

    `passed` is True only if the list of issues is empty.
    Issues are human-readable strings describing what failed.
    """
    issues: list[str] = []

    # --- Fetch ---
    # If the fetch itself fails (network error, bad credentials, invalid
    # symbol) we catch it here so other symbols still get validated.
    try:
        df = get_historical_bars(symbol, days=365)
    except Exception as exc:
        return False, [f"Fetch failed: {exc}"]

    # --- Check 1: Minimum bar count ---
    bar_count = len(df)
    if bar_count < MIN_BARS:
        issues.append(
            f"Only {bar_count} bars returned — expected at least {MIN_BARS}. "
            f"Check symbol validity or Alpaca subscription tier."
        )

    # --- Check 2: No nulls in OHLCV columns ---
    # data.py already validates this and raises, so if we got here the
    # DataFrame is clean. This is a belt-and-suspenders double-check that
    # also gives a clear report line in the output.
    ohlcv_cols = ["open", "high", "low", "close", "volume"]
    null_counts = df[ohlcv_cols].isnull().sum()
    total_nulls = null_counts.sum()
    if total_nulls > 0:
        issues.append(f"Null values found: {null_counts[null_counts > 0].to_dict()}")

    # --- Check 3: No zero or negative values ---
    # A zero close price means either the data feed glitched or the asset
    # was delisted. Either way it would corrupt every indicator calculation.
    for col in ["open", "high", "low", "close"]:
        bad_rows = df[df[col] <= 0]
        if not bad_rows.empty:
            issues.append(
                f"Column '{col}' has {len(bad_rows)} rows with zero or "
                f"negative values. Dates: {bad_rows['timestamp'].dt.date.tolist()[:5]}"
            )

    zero_vol = df[df["volume"] <= 0]
    if not zero_vol.empty:
        issues.append(
            f"Volume is zero or negative on {len(zero_vol)} bars. "
            f"Dates: {zero_vol['timestamp'].dt.date.tolist()[:5]}"
        )

    # --- Check 4: No large gaps between consecutive bars ---
    # Compute the difference between consecutive timestamps in calendar days.
    # A gap > MAX_GAP_DAYS suggests missing data (e.g., a full week of bars
    # wasn't returned).
    if len(df) > 1:
        # .diff() gives NaT for the first row; dropna removes it.
        gaps = df["timestamp"].diff().dropna()
        large_gaps = gaps[gaps > timedelta(days=MAX_GAP_DAYS)]
        if not large_gaps.empty:
            gap_details = []
            for idx in large_gaps.index:
                gap_days = large_gaps[idx].days
                gap_date = df.loc[idx, "timestamp"].date()
                gap_details.append(f"{gap_date} ({gap_days}d gap)")
            issues.append(
                f"{len(large_gaps)} gap(s) larger than {MAX_GAP_DAYS} calendar "
                f"days: {', '.join(gap_details[:3])}"
            )

    # --- Check 5: Data is current ---
    # The most recent bar should be within MAX_STALENESS_DAYS of today.
    # A larger gap suggests the API key is invalid or the feed is broken.
    latest_ts = df["timestamp"].iloc[-1]
    # Make naive datetime UTC-aware for comparison
    now_utc = datetime.now(timezone.utc)
    # latest_ts may already be tz-aware (UTC); normalise just in case
    if latest_ts.tzinfo is None:
        latest_ts = latest_ts.replace(tzinfo=timezone.utc)
    staleness = now_utc - latest_ts
    if staleness > timedelta(days=MAX_STALENESS_DAYS):
        issues.append(
            f"Most recent bar is {staleness.days} days old "
            f"(date: {latest_ts.date()}). Expected data within last "
            f"{MAX_STALENESS_DAYS} days."
        )

    passed = len(issues) == 0
    return passed, issues


def print_summary(symbol: str, df, passed: bool, issues: list[str]) -> None:
    """Print a formatted per-symbol result block to stdout."""
    status = "PASS" if passed else "FAIL"
    bar = "=" * 60

    print(f"\n{bar}")
    print(f"  {symbol}  —  {status}")
    print(bar)

    if df is not None:
        print(f"  Bars returned : {len(df)}")
        print(f"  Date range    : {df['timestamp'].iloc[0].date()} → {df['timestamp'].iloc[-1].date()}")
        print(f"  Latest close  : ${df['close'].iloc[-1]:.2f}")
        print(f"  Latest volume : {int(df['volume'].iloc[-1]):,}")

    if issues:
        print("  Issues:")
        for issue in issues:
            print(f"    ✗ {issue}")
    else:
        print("  All checks passed.")


def main() -> int:
    """
    Run validation for all symbols in settings.SYMBOLS.
    Returns 0 if all pass, 1 if any fail.
    """
    print("\nSwing Trader — Data Pipeline Validation")
    print(f"Symbols  : {', '.join(settings.symbols)}")
    print(f"Paper mode: {settings.alpaca_paper}")

    all_passed = True

    for symbol in settings.symbols:
        # We fetch again here (outside validate_symbol) so we can pass the
        # DataFrame to print_summary for the stats block even when it passes.
        # If the fetch fails, validate_symbol handles it and df stays None.
        df = None
        try:
            df = get_historical_bars(symbol, days=365)
        except Exception:
            pass  # validate_symbol will capture and report the error

        passed, issues = validate_symbol(symbol)
        print_summary(symbol, df, passed, issues)

        if not passed:
            all_passed = False

    # --- Final verdict ---
    print("\n" + "=" * 60)
    if all_passed:
        print("  RESULT: ALL SYMBOLS PASSED — ready to proceed to Phase 2")
    else:
        print("  RESULT: ONE OR MORE SYMBOLS FAILED — fix issues before Phase 2")
    print("=" * 60 + "\n")

    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
