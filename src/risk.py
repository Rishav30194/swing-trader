"""
risk.py — Position sizing and exit level computation.

Four public functions:

  compute_exit_levels(entry_price, atr, *, atr_stop_multiplier, atr_tp_multiplier)
      → ExitLevels(stop_loss, take_profit)

  compute_position_size(equity, risk_pct, entry_price, stop_loss)
      → float  (fractional shares; caller converts to notional dollars)

  update_trailing_stop(position, current_price, atr, *, atr_stop_multiplier)
      → float  (new trailing stop level; never lower than current)

  check_exit_conditions(position, current_bar)
      → "sl" | "trailing" | "tp" | "day5" | None

All functions are pure (no I/O, no API calls). Multipliers are explicit keyword
arguments so the functions are testable without a .env file.
"""

import logging
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ExitLevels:
    stop_loss:   float
    take_profit: float


@dataclass
class Position:
    """
    Represents one open trade. Fields match the `positions` table schema
    defined in docs/architecture.md so Phase 3 database persistence is a
    straight mapping with no structural changes.
    """
    symbol:        str
    entry_price:   float
    shares:        float
    stop_loss:     float
    trailing_stop: float
    take_profit:   float
    entry_date:    date
    db_id:         int | None = None  # set when loaded from SQLite; None for backtest


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

def compute_exit_levels(
    entry_price: float,
    atr: float,
    *,
    atr_stop_multiplier: float,
    atr_tp_multiplier: float,
) -> ExitLevels:
    """
    Compute ATR-based stop-loss and take-profit prices.

      stop_loss   = entry_price − (atr_stop_multiplier × ATR)
      take_profit = entry_price + (atr_tp_multiplier  × ATR)

    Raises:
        ValueError: if entry_price or atr are non-positive, or if the
                    resulting stop_loss would be at or above entry_price.
    """
    if entry_price <= 0:
        raise ValueError(f"entry_price must be positive, got {entry_price}")
    if atr <= 0:
        raise ValueError(f"atr must be positive, got {atr}")

    stop_loss   = entry_price - (atr_stop_multiplier * atr)
    take_profit = entry_price + (atr_tp_multiplier  * atr)

    if stop_loss >= entry_price:
        raise ValueError(
            f"Computed stop_loss ({stop_loss:.4f}) is at or above entry_price "
            f"({entry_price:.4f}). Check atr_stop_multiplier."
        )

    return ExitLevels(stop_loss=stop_loss, take_profit=take_profit)


def compute_position_size(
    equity: float,
    risk_pct: float,
    entry_price: float,
    stop_loss: float,
) -> float:
    """
    Compute fractional shares to buy based on fixed fractional risk.

      risk_amount    = equity × risk_pct
      risk_per_share = entry_price − stop_loss
      shares         = risk_amount / risk_per_share  (fractional, not floored)

    The caller is responsible for converting to a notional dollar amount and
    applying any position-size caps before submitting an order.

    Raises:
        ValueError: if stop_loss >= entry_price (invalid risk setup), or if
                    equity or risk_pct are non-positive.
    """
    if equity <= 0:
        raise ValueError(f"equity must be positive, got {equity}")
    if risk_pct <= 0:
        raise ValueError(f"risk_pct must be positive, got {risk_pct}")
    if stop_loss >= entry_price:
        raise ValueError(
            f"stop_loss ({stop_loss}) must be below entry_price ({entry_price}). "
            "Call compute_exit_levels() before compute_position_size()."
        )

    risk_amount    = equity * risk_pct
    risk_per_share = entry_price - stop_loss
    shares         = risk_amount / risk_per_share

    logger.debug(
        "Position size: equity=%.2f risk_pct=%.1f%% → risk=$%.2f "
        "risk/share=%.4f → %.4f shares",
        equity, risk_pct * 100, risk_amount, risk_per_share, shares,
    )

    return shares


def update_trailing_stop(
    position: Position,
    current_price: float,
    atr: float,
    *,
    atr_stop_multiplier: float,
    atr_trailing_activation: float,
) -> float:
    """
    Ratchet the trailing stop upward if the trade has moved in our favor.

    Activation rule: the trailing stop only begins moving once price is
    more than atr_trailing_activation × ATR above entry. Before that, the
    hard stop provides the only protection and the trailing stop stays put.

    Once active, the new trailing stop candidate is:
        candidate = current_price − (atr_stop_multiplier × ATR)

    The returned value is max(position.trailing_stop, candidate), ensuring
    the stop never moves down.

    Returns:
        The new trailing stop level (may equal the current level if price
        has not moved enough to improve it).
    """
    # Activation threshold: price must clear entry by atr_trailing_activation × ATR
    if current_price <= position.entry_price + atr_trailing_activation * atr:
        return position.trailing_stop

    candidate = current_price - (atr_stop_multiplier * atr)
    new_stop  = max(position.trailing_stop, candidate)

    if new_stop > position.trailing_stop:
        logger.debug(
            "Trailing stop raised: %.4f → %.4f  (price=%.4f entry=%.4f)",
            position.trailing_stop, new_stop, current_price, position.entry_price,
        )

    return new_stop


def check_exit_conditions(
    position: Position,
    current_bar: pd.Series,
) -> str | None:
    """
    Check whether any exit condition is triggered on the current daily bar.

    Uses bar low to check stops (conservative: assumes the low was hit during
    the session) and bar high to check the take-profit target.

    Priority matches the spec:
        1. "sl"       — hard stop-loss hit (bar low ≤ stop_loss)
        2. "trailing" — trailing stop hit   (bar low ≤ trailing_stop)
        3. "tp"       — take-profit hit     (bar high ≥ take_profit)
        4. "day5"     — 5 trading days (Mon–Fri) elapsed since entry

    Returns None if no exit condition is triggered.
    """
    bar_low  = float(current_bar["low"])
    bar_high = float(current_bar["high"])
    bar_date = pd.Timestamp(current_bar["timestamp"]).date()

    if bar_low <= position.stop_loss:
        logger.info(
            "%s: hard stop hit — low=%.4f ≤ stop=%.4f",
            position.symbol, bar_low, position.stop_loss,
        )
        return "sl"

    if bar_low <= position.trailing_stop:
        logger.info(
            "%s: trailing stop hit — low=%.4f ≤ trailing=%.4f",
            position.symbol, bar_low, position.trailing_stop,
        )
        return "trailing"

    if bar_high >= position.take_profit:
        logger.info(
            "%s: take-profit hit — high=%.4f ≥ tp=%.4f",
            position.symbol, bar_high, position.take_profit,
        )
        return "tp"

    trading_days_held = int(np.busday_count(position.entry_date, bar_date))
    if trading_days_held >= 5:
        logger.info(
            "%s: day-5 force-close — held %d trading days",
            position.symbol, trading_days_held,
        )
        return "day5"

    return None
