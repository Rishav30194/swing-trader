"""
test_data.py — Unit tests for src/data.py.

The bar-selection logic decides which close the strategy trades on, so it is
money-critical: evaluating a still-forming intraday bar means deciding against
a close that has not happened yet.

No network calls — the Alpaca client is never touched.
"""

import zoneinfo
from datetime import datetime, timezone

import pandas as pd
import pytest
from unittest.mock import patch

import src.data as data

UTC = timezone.utc
ET = zoneinfo.ZoneInfo("America/New_York")


def _frame(*dates: str) -> pd.DataFrame:
    return pd.DataFrame({
        "timestamp": pd.to_datetime(list(dates), utc=True),
        "open": [1.0] * len(dates),
        "high": [1.0] * len(dates),
        "low": [1.0] * len(dates),
        "close": [float(i + 1) for i in range(len(dates))],
        "volume": [100.0] * len(dates),
    })


def _at(et_datetime: datetime):
    """Patch data.datetime so 'now' is a fixed ET wall-clock time."""
    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            aware = et_datetime.replace(tzinfo=ET)
            return aware.astimezone(tz) if tz else aware
    return patch.object(data, "datetime", FakeDatetime)


# ---------------------------------------------------------------------------
# _drop_forming_bar
# ---------------------------------------------------------------------------

class TestDropFormingBarDuringSession:
    def test_drops_todays_bar_while_market_is_open(self):
        df = _frame("2026-07-30", "2026-07-31")
        with _at(datetime(2026, 7, 31, 11, 0)):        # 11:00 ET, mid-session
            out = data._drop_forming_bar(df, "NVDA")
        assert list(out["timestamp"].dt.date.astype(str)) == ["2026-07-30"]

    def test_drops_at_the_opening_bell(self):
        df = _frame("2026-07-30", "2026-07-31")
        with _at(datetime(2026, 7, 31, 9, 30)):
            assert len(data._drop_forming_bar(df, "NVDA")) == 1

    def test_drops_premarket(self):
        df = _frame("2026-07-30", "2026-07-31")
        with _at(datetime(2026, 7, 31, 7, 0)):
            assert len(data._drop_forming_bar(df, "NVDA")) == 1

    def test_drops_one_minute_before_close(self):
        df = _frame("2026-07-30", "2026-07-31")
        with _at(datetime(2026, 7, 31, 15, 59)):
            assert len(data._drop_forming_bar(df, "NVDA")) == 1


class TestKeepsCompletedBarAfterClose:
    """
    The rebalance runs after the close. Dropping today's bar there would give
    live a two-bar execution lag where the backtest has one.
    """

    def test_keeps_todays_bar_at_the_rebalance_time(self):
        df = _frame("2026-07-30", "2026-07-31")
        with _at(datetime(2026, 7, 31, 16, 15)):       # scheduled rebalance
            out = data._drop_forming_bar(df, "NVDA")
        assert len(out) == 2
        assert out["close"].iloc[-1] == 2.0            # Friday's close, not Thursday's

    def test_keeps_todays_bar_exactly_at_the_close(self):
        df = _frame("2026-07-30", "2026-07-31")
        with _at(datetime(2026, 7, 31, 16, 0)):
            assert len(data._drop_forming_bar(df, "NVDA")) == 2

    def test_keeps_todays_bar_late_evening(self):
        df = _frame("2026-07-30", "2026-07-31")
        with _at(datetime(2026, 7, 31, 23, 30)):
            assert len(data._drop_forming_bar(df, "NVDA")) == 2


class TestNoTodayBar:
    def test_weekend_leaves_fridays_bar_alone(self):
        df = _frame("2026-07-30", "2026-07-31")
        with _at(datetime(2026, 8, 1, 9, 0)):          # Saturday
            assert len(data._drop_forming_bar(df, "NVDA")) == 2

    def test_stale_data_is_not_truncated(self):
        """If the feed is days behind, nothing should be dropped."""
        df = _frame("2026-07-28", "2026-07-29")
        with _at(datetime(2026, 7, 31, 12, 0)):
            assert len(data._drop_forming_bar(df, "NVDA")) == 2


class TestDropFormingBarEdges:
    def test_raises_rather_than_returning_empty(self):
        """An empty frame downstream would look like 'no data' instead of an error."""
        df = _frame("2026-07-31")
        with _at(datetime(2026, 7, 31, 11, 0)):
            with pytest.raises(ValueError, match="No completed bars"):
                data._drop_forming_bar(df, "NVDA")

    def test_does_not_mutate_the_caller_frame(self):
        df = _frame("2026-07-30", "2026-07-31")
        with _at(datetime(2026, 7, 31, 11, 0)):
            data._drop_forming_bar(df, "NVDA")
        assert len(df) == 2


# ---------------------------------------------------------------------------
# _validate_and_clean — the firewall between Alpaca and the strategy
# ---------------------------------------------------------------------------

class TestValidateAndClean:
    def test_accepts_a_well_formed_frame(self):
        out = data._validate_and_clean(_frame("2026-07-30", "2026-07-31"), "NVDA")
        assert list(out.columns) == ["timestamp", "open", "high", "low", "close", "volume"]

    def test_rejects_empty_frame(self):
        with pytest.raises(ValueError, match="No bars returned"):
            data._validate_and_clean(_frame(), "NVDA")

    def test_rejects_missing_columns(self):
        df = _frame("2026-07-31").drop(columns=["close"])
        with pytest.raises(ValueError, match="missing expected columns"):
            data._validate_and_clean(df, "NVDA")

    def test_rejects_null_close(self):
        """A null close would silently corrupt SMA_200 rather than fail."""
        df = _frame("2026-07-30", "2026-07-31")
        df.loc[1, "close"] = None
        with pytest.raises(ValueError, match="Null values"):
            data._validate_and_clean(df, "NVDA")

    def test_sorts_ascending_by_timestamp(self):
        df = _frame("2026-07-31", "2026-07-30")
        out = data._validate_and_clean(df, "NVDA")
        assert out["timestamp"].is_monotonic_increasing

    def test_drops_extra_columns(self):
        df = _frame("2026-07-31")
        df["trade_count"] = 5
        assert "trade_count" not in data._validate_and_clean(df, "NVDA").columns
