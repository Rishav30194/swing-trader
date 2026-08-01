"""
portfolio.py — Regime state, target weights, and rebalance order generation.

This module holds the entire strategy. It is pure: no I/O, no API calls, no
config import. Every threshold is an explicit keyword argument so the same
functions run in the backtest and in production without divergence — the class
of bug that broke the previous strategy.

Three stages, in order:

  compute_regime_state(df, band, currently_held)
      → RegimeState(on, context)      per symbol, from its own 200-day SMA

  compute_target_weights(states, max_position_pct)
      → dict[symbol, weight]          equal weight across ALL symbols; a sleeve
                                      whose regime is off gets exactly 0

  diff_to_orders(current_notional, target_weights, equity, ...)
      → list[RebalanceOrder]          sells first, then buys

The band is hysteresis, not a threshold: a held sleeve exits only below
(1 − band) × SMA_200 and a flat sleeve enters only above (1 + band) × SMA_200.
Between those lines the current state persists. This is what keeps turnover at
~7×/yr instead of ~20×/yr when price oscillates around the average.
"""

import logging
from dataclasses import dataclass, field

import pandas as pd

logger = logging.getLogger(__name__)

_REQUIRED_COLUMNS = {"close", "SMA_200"}


@dataclass(frozen=True)
class RegimeState:
    """Whether a sleeve should be invested, plus the values behind the decision."""
    on: bool
    context: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RebalanceOrder:
    """One order in a rebalance plan. `notional` is always a positive dollar amount."""
    symbol: str
    side: str                    # "buy" | "sell"
    notional: float
    reason: str                  # "regime_entry" | "regime_exit" | "drift"
    increases_exposure: bool     # gates it behind the weekly YES (hard rule 3)


# ---------------------------------------------------------------------------
# Stage 1 — regime state
# ---------------------------------------------------------------------------

def compute_regime_state(
    df: pd.DataFrame,
    *,
    band: float,
    currently_held: bool,
) -> RegimeState:
    """
    Decide whether `df`'s symbol should be invested, using a hysteresis band.

    Args:
        df: Indicator-enriched DataFrame whose LAST row is the most recent
            completed daily bar. Must contain `close` and `SMA_200`.
        band: Fractional half-width of the hysteresis band (0.02 = 2%).
        currently_held: Whether this sleeve is invested right now. Determines
            which edge of the band applies.

    Returns:
        RegimeState. `context` always carries the values behind the decision so
        the weekly Telegram message can explain itself.

    Raises:
        ValueError: if required columns are missing or `df` is empty.
    """
    _validate_input(df)

    current = df.iloc[-1]
    close = float(current["close"])
    sma = current["SMA_200"]

    # A NaN SMA means insufficient history. Never enter on unknown state; hold
    # an existing sleeve rather than churning it out on missing data.
    if pd.isna(sma):
        logger.warning("SMA_200 is NaN — holding current state (held=%s)", currently_held)
        return RegimeState(on=currently_held, context={"skip_reason": "sma_200_nan"})

    sma = float(sma)
    lower = sma * (1.0 - band)
    upper = sma * (1.0 + band)

    if currently_held:
        on = close >= lower
        reason = "exit_band_broken" if not on else "held"
    else:
        on = close > upper
        reason = "entry_band_cleared" if on else "flat"

    return RegimeState(on=on, context={
        "close": round(close, 4),
        "sma_200": round(sma, 4),
        "lower_band": round(lower, 4),
        "upper_band": round(upper, 4),
        "was_held": currently_held,
        "reason": reason,
    })


# ---------------------------------------------------------------------------
# Stage 2 — target weights
# ---------------------------------------------------------------------------

def compute_target_weights(
    states: dict[str, RegimeState],
    *,
    universe_size: int,
    max_position_pct: float,
) -> dict[str, float]:
    """
    Convert regime states into target portfolio weights.

    Equal weight is 1/`universe_size` — the CONFIGURED symbol count, not the
    number of sleeves in `states`. The two differ whenever a symbol could not be
    evaluated (data outage, or a symbol not yet listed at this point in a
    backtest). Dividing by len(states) in that situation would hand the missing
    sleeve's capital to the survivors, concentrating the portfolio precisely
    when information is missing. The freed weight must stay in cash instead.

    Sleeves that are off also go to cash rather than being redistributed, for
    the same reason: this strategy reduces risk by holding less, never by
    holding fewer things more heavily.

    Args:
        states: Regime state per evaluable symbol.
        universe_size: len(settings.symbols) — the full configured universe.
        max_position_pct: Per-sleeve cap as a fraction of equity.

    Raises:
        ValueError: if `states` is empty, `universe_size` is smaller than the
            number of states, or `max_position_pct` is not in (0, 1].
    """
    if not states:
        raise ValueError("compute_target_weights: states must not be empty")
    if universe_size < len(states):
        raise ValueError(
            f"universe_size ({universe_size}) is smaller than the number of "
            f"evaluated sleeves ({len(states)})"
        )
    if not 0.0 < max_position_pct <= 1.0:
        raise ValueError(
            f"max_position_pct must be in (0, 1], got {max_position_pct}"
        )

    equal = 1.0 / universe_size
    capped = min(equal, max_position_pct)

    return {sym: (capped if state.on else 0.0) for sym, state in states.items()}


def validate_target_weights(
    weights: dict[str, float],
    states: dict[str, RegimeState],
    *,
    max_position_pct: float,
) -> None:
    """
    Assert the hard rules hold before any order is generated.

    Raises:
        ValueError: on any violation. Callers must not place orders if this raises.
    """
    for sym, w in weights.items():
        if w < 0:
            raise ValueError(f"{sym}: negative target weight {w}")
        if w > max_position_pct + 1e-9:
            raise ValueError(
                f"{sym}: target weight {w:.4f} exceeds max_position_pct "
                f"{max_position_pct:.4f} (hard rule 5)"
            )
        if sym in states and not states[sym].on and abs(w) > 1e-12:
            raise ValueError(
                f"{sym}: regime is off but target weight is {w} — must be 0 (hard rule 2)"
            )

    total = sum(weights.values())
    if total > 1.0 + 1e-9:
        raise ValueError(f"target weights sum to {total:.4f}, exceeding 1.0")


# ---------------------------------------------------------------------------
# Stage 3 — orders
# ---------------------------------------------------------------------------

def diff_to_orders(
    current_notional: dict[str, float],
    target_weights: dict[str, float],
    equity: float,
    *,
    min_order_notional: float = 1.0,
    drift_tolerance: float = 0.001,
) -> list[RebalanceOrder]:
    """
    Turn the gap between current and target holdings into executable orders.

    Args:
        current_notional: Current dollar value held per symbol. Symbols absent
            from this dict are treated as flat.
        target_weights: Output of compute_target_weights().
        equity: Live account equity, fetched fresh (hard rule 4).
        min_order_notional: Alpaca rejects notional orders below $1.
        drift_tolerance: Skip rebalancing trades smaller than this fraction of
            equity. Suppresses dust churn without affecting regime transitions,
            which are always a full sleeve and far above the threshold.

    Returns:
        Orders sorted sells-first so proceeds are available before buys run.

    Raises:
        ValueError: if equity is non-positive.
    """
    if equity <= 0:
        raise ValueError(f"equity must be positive, got {equity}")

    threshold = max(min_order_notional, drift_tolerance * equity)
    orders: list[RebalanceOrder] = []

    for symbol, weight in target_weights.items():
        held = current_notional.get(symbol, 0.0)
        target = weight * equity
        delta = target - held

        if abs(delta) < threshold:
            continue

        reason = _classify(held, target)
        orders.append(RebalanceOrder(
            symbol=symbol,
            side="buy" if delta > 0 else "sell",
            notional=abs(delta),
            reason=reason,
            increases_exposure=delta > 0,
        ))

    # Sells first: they free the cash the buys will consume.
    orders.sort(key=lambda o: (o.side != "sell", o.symbol))
    return orders


def _classify(held: float, target: float) -> str:
    """Label an order so the Telegram plan and the trade log can explain it."""
    if held <= 0 and target > 0:
        return "regime_entry"
    if held > 0 and target <= 0:
        return "regime_exit"
    return "drift"


def _validate_input(df: pd.DataFrame) -> None:
    if len(df) == 0:
        raise ValueError("compute_regime_state: DataFrame is empty")
    missing = _REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(
            f"compute_regime_state: missing columns {missing}. "
            "Call compute_indicators() first."
        )
