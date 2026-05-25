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
    # False → connect to the live endpoint (real money — Phase 4 only)
    alpaca_paper: bool

    # --- Telegram ---
    telegram_bot_token: str
    telegram_chat_id: str

    # --- Asset universe ---
    # The four symbols we scan. Stored as a tuple so it's immutable.
    symbols: tuple[str, ...]

    # --- Strategy thresholds ---
    # RSI must be at or above this value (no panic-sell entries).
    rsi_lower_bound: float

    # RSI must be strictly below this value (only buy actual dips, not breakouts).
    rsi_upper_bound: float

    # --- ATR-based exit multipliers ---
    # Hard stop-loss = entry_price - (atr_stop_multiplier × ATR)
    atr_stop_multiplier: float

    # Take-profit    = entry_price + (atr_tp_multiplier × ATR)
    atr_tp_multiplier: float

    # Trailing stop activates once price rises by this many ATRs above entry.
    atr_trailing_activation: float

    # --- Position sizing ---
    # Fraction of account equity to risk on each trade.
    # 0.02 = risk at most 2% of the account per position.
    risk_per_trade_pct: float

    # Hard cap: never hold more than this many open positions at once.
    max_open_positions: int

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
    symbols_raw = _get("SYMBOLS", "NVDA,ASML,VOO,QQQM,MSFT,AAPL,AMD,META,TSM,SPY")
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

        # Strategy thresholds — all have sensible defaults matching the spec,
        # but can be overridden in .env without touching this file.
        rsi_lower_bound=float(_get("RSI_LOWER_BOUND", "40")),
        rsi_upper_bound=float(_get("RSI_UPPER_BOUND", "55")),

        atr_stop_multiplier=float(_get("ATR_STOP_MULTIPLIER", "1.5")),
        atr_tp_multiplier=float(_get("ATR_TP_MULTIPLIER", "2.0")),
        atr_trailing_activation=float(_get("ATR_TRAILING_ACTIVATION", "0.5")),

        risk_per_trade_pct=float(_get("RISK_PER_TRADE_PCT", "0.02")),
        max_open_positions=int(_get("MAX_OPEN_POSITIONS", "2")),
    )


# Module-level singleton. Import this in every other module:
#   from src.config import settings
settings = _load_settings()
