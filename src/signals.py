"""
signals.py — Buy signal evaluation.

Single public function: evaluate_buy_signal(df, ...) checks whether the
latest bar in an indicator-enriched DataFrame satisfies all three buy conditions
simultaneously. Returns a SignalResult with a triggered flag and a context dict
carrying every value a human needs to approve or reject the trade via Telegram.

The three conditions (AND gate — all must be true):
  1. Close > EMA_50         — structural uptrend; never buy falling knives
  2. rsi_lower_bound ≤ RSI(14) < rsi_upper_bound — controlled pullback in uptrend
  3. MACD bullish crossover — MACD crossed above signal on this bar (not just above)

Callers must run compute_indicators() on the DataFrame before passing it here.
Thresholds are passed explicitly so this module has no .env dependency and
is straightforward to unit-test with any values.
"""

import logging
from dataclasses import dataclass, field

import pandas as pd

logger = logging.getLogger(__name__)

_REQUIRED_COLUMNS = {
    "close", "RSI_14", "EMA_50", "MACD", "MACD_signal", "ATR_14",
}


@dataclass
class SignalResult:
    triggered: bool
    context: dict = field(default_factory=dict)


def evaluate_buy_signal(
    df: pd.DataFrame,
    *,
    rsi_lower_bound: float,
    rsi_upper_bound: float,
) -> SignalResult:
    """
    Evaluate whether the last bar of `df` triggers a buy signal.

    Args:
        df: Indicator-enriched DataFrame from compute_indicators(). Must have
            at least 2 rows so the MACD crossover can compare current vs prior bar.
        rsi_lower_bound: RSI must be at or above this value (e.g. 40.0).
        rsi_upper_bound: RSI must be strictly below this value (e.g. 55.0).

    Returns:
        SignalResult with triggered=True only if all three conditions are met.
        context always contains the full indicator snapshot for alerting/logging.

    Raises:
        ValueError: if required indicator columns are missing or df has < 2 rows.
    """
    _validate_input(df)

    current = df.iloc[-1]
    prev    = df.iloc[-2]

    # --- NaN guard: warm-up rows produce NaN indicators ---
    for col in _REQUIRED_COLUMNS:
        if pd.isna(current[col]):
            logger.debug("Signal skipped: %s is NaN on current bar (still in warm-up)", col)
            return SignalResult(triggered=False, context={"skip_reason": f"{col}_nan"})

    if pd.isna(prev["MACD"]) or pd.isna(prev["MACD_signal"]):
        logger.debug("Signal skipped: MACD is NaN on previous bar (still in warm-up)")
        return SignalResult(triggered=False, context={"skip_reason": "prev_macd_nan"})

    # --- Condition 1: Trend filter — close above EMA_50 ---
    close  = float(current["close"])
    ema_50 = float(current["EMA_50"])
    cond_trend = close > ema_50

    # --- Condition 2: RSI pullback range ---
    rsi = float(current["RSI_14"])
    cond_rsi = rsi_lower_bound <= rsi < rsi_upper_bound

    # --- Condition 3: MACD bullish crossover on this specific bar ---
    # "Crossover" means MACD was at or below signal on the prior bar and is
    # strictly above it now. A signal that has been above for multiple bars
    # does not qualify — that setup has already played out.
    macd_curr   = float(current["MACD"])
    signal_curr = float(current["MACD_signal"])
    macd_prev   = float(prev["MACD"])
    signal_prev = float(prev["MACD_signal"])
    cond_macd = (macd_curr > signal_curr) and (macd_prev <= signal_prev)

    triggered = cond_trend and cond_rsi and cond_macd

    context = {
        # Trend
        "close":           round(close, 4),
        "ema_50":          round(ema_50, 4),
        # RSI
        "rsi":             round(rsi, 2),
        "rsi_lower_bound": rsi_lower_bound,
        "rsi_upper_bound": rsi_upper_bound,
        # MACD
        "macd":            round(macd_curr, 4),
        "macd_signal":     round(signal_curr, 4),
        "macd_prev":       round(macd_prev, 4),
        "macd_signal_prev": round(signal_prev, 4),
        # ATR (needed by risk.py after approval)
        "atr":             round(float(current["ATR_14"]), 4),
        # Condition verdicts — lets the notifier flag which ones passed/failed
        "cond_trend":      cond_trend,
        "cond_rsi":        cond_rsi,
        "cond_macd":       cond_macd,
    }

    if triggered:
        logger.info(
            "BUY SIGNAL triggered — close=%.2f EMA_50=%.2f RSI=%.1f MACD_cross=True",
            close, ema_50, rsi,
        )
    else:
        failed = [k for k in ("cond_trend", "cond_rsi", "cond_macd") if not context[k]]
        logger.debug("No signal — failed conditions: %s", failed)

    return SignalResult(triggered=triggered, context=context)


def _validate_input(df: pd.DataFrame) -> None:
    if len(df) < 2:
        raise ValueError(
            "evaluate_buy_signal: need at least 2 rows to evaluate MACD crossover."
        )
    missing = _REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(
            f"evaluate_buy_signal: missing columns {missing}. "
            "Call compute_indicators() before evaluate_buy_signal()."
        )
