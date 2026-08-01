"""
config.py — Central configuration loader for the swing trader.

All runtime settings come from a .env file in the project root.
This module reads that file once at import time and exposes a single
`Settings` dataclass. Every other module imports `settings` from here
instead of reading environment variables directly.

Why a dataclass instead of raw os.environ calls scattered around the code?
- One place to see every configurable parameter
- Type annotations catch mistakes (a string "45" vs int 45)
- Missing variables raise a clear error at startup, not mid-run
"""

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

# Load the .env file from the project root into os.environ.
# If the file doesn't exist this is a no-op (existing env vars are kept).
load_dotenv()


def _require(key: str) -> str:
    """
    Read an environment variable and raise immediately if it isn't set.

    Failing at startup (before any trades run) is much safer than
    discovering a missing variable after the scheduler has started.
    """
    value = os.getenv(key)
    if not value:
        raise ValueError(
            f"Required environment variable '{key}' is missing or empty. "
            f"Add it to your .env file and restart."
        )
    return value


def _get(key: str, default: str) -> str:
    """Read an optional environment variable, returning a default if absent."""
    return os.getenv(key, default)


@dataclass(frozen=True)
class Settings:
    """
    All runtime configuration in one place.

    frozen=True means the object is immutable after creation — you can't
    accidentally overwrite a setting mid-run. If a setting needs to change,
    restart the process so the .env file is re-read cleanly.
    """

    # --- Alpaca credentials ---
    # These are loaded from .env and never hardcoded.
    alpaca_api_key: str
    alpaca_api_secret: str

    # True  → connect to Alpaca's paper trading sandbox (fake money)
    # False → connect to the live endpoint (real money)
    alpaca_paper: bool

    # --- Telegram ---
    telegram_bot_token: str
    telegram_chat_id: str

    # --- Asset universe ---
    # The eight symbols we scan. Stored as a tuple so it's immutable.
    symbols: tuple[str, ...]

    # --- Strategy thresholds ---
    # Hysteresis half-width around SMA_200, as a fraction. 0.02 means a held
    # sleeve exits below 0.98 × SMA_200 and a flat sleeve enters above 1.02 ×.
    # The band is what keeps turnover near 7×/yr instead of ~20×/yr.
    sma_band: float

    # Calendar days of history to request. SMA_200 needs 200 completed bars,
    # so this must comfortably exceed 200 trading days.
    bars_lookback_days: int

    # --- Rebalancing ---
    # Skip DRIFT trades smaller than this fraction of equity. Regime entries and
    # exits ignore it entirely — they are decisions, not sizing adjustments.
    # This is primarily a tax dial: drift trades are ~93% of all orders at 0.1%
    # and realise capital gains for no measurable benefit.
    drift_tolerance: float

    # Alpaca rejects notional orders below $1.
    min_order_notional: float

    # When the weekly rebalance runs (ET). Operational, not a strategy parameter.
    rebalance_day: str
    rebalance_hour: int
    rebalance_minute: int

    # Seconds to wait for the weekly YES/NO reply before skipping increases.
    reply_timeout_secs: int

    # Max fraction of strategy capital to deploy in a single sleeve.
    max_position_pct: float

    # Capital allocated to this strategy, in dollars. Sizing uses THIS, not the
    # account balance: a paper account funded with $100,000 must still trade the
    # $1,000 you intend. Profits compound on top of it — once the strategy's own
    # holdings are worth $1,100, it sizes off $1,100 — but money sitting in the
    # account that was never allocated is never touched.
    trading_capital: float

    # --- Derived convenience properties ---

    @property
    def alpaca_base_url(self) -> str:
        """Return the correct Alpaca REST base URL based on paper/live mode."""
        if self.alpaca_paper:
            return "https://paper-api.alpaca.markets"
        return "https://api.alpaca.markets"

    @property
    def alpaca_data_url(self) -> str:
        """Alpaca's market data endpoint (same for paper and live)."""
        return "https://data.alpaca.markets"


def _load_settings() -> Settings:
    """
    Build a Settings instance by reading environment variables.

    Called once at module import. The result is stored in the module-level
    `settings` constant below. All other modules should import that constant
    rather than calling this function themselves.
    """

    # Parse ALPACA_PAPER: treat any value other than "true" (case-insensitive)
    # as False. This makes the default safe — you must explicitly opt into live.
    alpaca_paper_raw = _get("ALPACA_PAPER", "true")
    alpaca_paper = alpaca_paper_raw.strip().lower() == "true"

    # Parse SYMBOLS: a comma-separated string like "NVDA,ASML,VOO,QQQM"
    # is split into ("NVDA", "ASML", "VOO", "QQQM").
    symbols_raw = _get("SYMBOLS", "NVDA,ASML,VOO,QQQM,MSFT,AAPL,AMD,TSM")
    symbols = tuple(s.strip().upper() for s in symbols_raw.split(",") if s.strip())
    if not symbols:
        raise ValueError("SYMBOLS env var is set but contains no valid ticker symbols.")

    return Settings(
        # Required — startup fails immediately if either key is absent.
        alpaca_api_key=_require("ALPACA_API_KEY"),
        alpaca_api_secret=_require("ALPACA_API_SECRET"),
        alpaca_paper=alpaca_paper,

        telegram_bot_token=_require("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_require("TELEGRAM_CHAT_ID"),

        symbols=symbols,

        # Strategy thresholds — all have sensible defaults matching the
        # validated configuration, but can be overridden in .env without
        # touching this file.
        sma_band=float(_get("SMA_BAND", "0.02")),
        bars_lookback_days=int(_get("BARS_LOOKBACK_DAYS", "365")),

        # 5%: 124 orders over the 2018-2026 backtest versus 1,727 at 0.1%, with
        # slightly BETTER Sharpe and CAGR, and ~$16k less tax on a $100k base.
        drift_tolerance=float(_get("DRIFT_TOLERANCE", "0.05")),
        min_order_notional=float(_get("MIN_ORDER_NOTIONAL", "1.0")),

        rebalance_day=_get("REBALANCE_DAY", "fri"),
        rebalance_hour=int(_get("REBALANCE_HOUR", "16")),
        rebalance_minute=int(_get("REBALANCE_MINUTE", "15")),

        # 4 hours: the plan is sent after Friday's close and the orders queue to
        # Monday's open, so there is no rush — this just needs to be long enough
        # that stepping away from the phone does not silently skip the increases.
        reply_timeout_secs=int(_get("REPLY_TIMEOUT_SECS", "14400")),
        max_position_pct=float(_get("MAX_POSITION_PCT", "0.25")),
        trading_capital=float(_require("TRADING_CAPITAL")),
    )


# Module-level singleton. Import this in every other module:
#   from src.config import settings
settings = _load_settings()
