"""
signals.py — Buy signal evaluation.

Single public function: evaluate_buy_signal(df, ...) checks whether the
latest bar in an indicator-enriched DataFrame satisfies all four buy conditions
simultaneously. Returns a SignalResult with a triggered flag and a context dict
carrying every value a human needs to approve or reject the trade via Telegram.

The four conditions (AND gate — all must be true):
  1. RSI(14) < rsi_threshold          — asset has pulled back, not overbought
  2. Price within ema_proximity_pct of EMA_21 — still in the trend, not free-falling
  3. MACD bullish crossover on this bar — MACD crossed above signal (not just above)
  4. Volume >= volume_spike_multiplier × VOL_SMA_20 — institutional participation

Callers must run compute_indicators() on the DataFrame before passing it here.
Thresholds are passed explicitly so this module has no .env dependency and
is straightforward to unit-test with any values.
"""

import logging
from dataclasses import dataclass, field

import pandas as pd

logger = logging.getLogger(__name__)

_REQUIRED_COLUMNS = {
    "close", "RSI_14", "EMA_21", "MACD", "MACD_signal", "VOL_SMA_20", "ATR_14", "volume",
}


@dataclass
class SignalResult:
    triggered: bool
    context: dict = field(default_factory=dict)


def evaluate_buy_signal(
    df: pd.DataFrame,
    *,
    rsi_threshold: float,
    ema_proximity_pct: float,
    volume_spike_multiplier: float,
) -> SignalResult:
    """
    Evaluate whether the last bar of `df` triggers a buy signal.

    Args:
        df: Indicator-enriched DataFrame from compute_indicators(). Must have
            at least 2 rows so the MACD crossover can compare current vs prior bar.
        rsi_threshold: RSI must be strictly below this value (e.g. 45.0).
        ema_proximity_pct: Max fractional distance from EMA_21 (e.g. 0.02 = 2%).
        volume_spike_multiplier: Volume must be at least this multiple of VOL_SMA_20.

    Returns:
        SignalResult with triggered=True only if all four conditions are met.
        context always contains the full indicator snapshot for alerting/logging.

    Raises:
        ValueError: if required indicator columns are missing or df has < 2 rows.
    """
    _validate_input(df)

    current = df.iloc[-1]
    prev    = df.iloc[-2]

    # --- NaN guard: warm-up rows produce NaN indicators ---
    # Check current bar first, then previous bar's MACD (needed for crossover).
    for col in _REQUIRED_COLUMNS:
        if pd.isna(current[col]):
            logger.debug("Signal skipped: %s is NaN on current bar (still in warm-up)", col)
            return SignalResult(triggered=False, context={"skip_reason": f"{col}_nan"})

    if pd.isna(prev["MACD"]) or pd.isna(prev["MACD_signal"]):
        logger.debug("Signal skipped: MACD is NaN on previous bar (still in warm-up)")
        return SignalResult(triggered=False, context={"skip_reason": "prev_macd_nan"})

    # --- Condition 1: RSI cooldown ---
    rsi = float(current["RSI_14"])
    cond_rsi = rsi < rsi_threshold

    # --- Condition 2: EMA proximity ---
    close  = float(current["close"])
    ema_21 = float(current["EMA_21"])
    ema_dist_pct = abs(close - ema_21) / ema_21
    cond_ema = ema_dist_pct <= ema_proximity_pct

    # --- Condition 3: MACD bullish crossover on this specific bar ---
    # "Crossover" means MACD was at or below signal on the prior bar and is
    # strictly above it now. A signal that has been above for multiple bars
    # does not qualify — that setup has already played out.
    macd_curr   = float(current["MACD"])
    signal_curr = float(current["MACD_signal"])
    macd_prev   = float(prev["MACD"])
    signal_prev = float(prev["MACD_signal"])
    cond_macd = (macd_curr > signal_curr) and (macd_prev <= signal_prev)

    # --- Condition 4: Volume spike ---
    volume  = float(current["volume"])
    vol_sma = float(current["VOL_SMA_20"])
    cond_volume = volume >= (volume_spike_multiplier * vol_sma)

    triggered = cond_rsi and cond_ema and cond_macd and cond_volume

    context = {
        # Price & trend
        "close":                  round(close, 4),
        "ema_21":                 round(ema_21, 4),
        "ema_dist_pct":           round(ema_dist_pct * 100, 3),   # as percentage
        "ema_proximity_max_pct":  round(ema_proximity_pct * 100, 3),
        # Momentum
        "rsi":                    round(rsi, 2),
        "rsi_threshold":          rsi_threshold,
        # MACD
        "macd":                   round(macd_curr, 4),
        "macd_signal":            round(signal_curr, 4),
        "macd_prev":              round(macd_prev, 4),
        "macd_signal_prev":       round(signal_prev, 4),
        # Volume
        "volume":                 int(volume),
        "vol_sma_20":             round(vol_sma, 0),
        "volume_ratio":           round(volume / vol_sma, 2),
        "volume_spike_min":       volume_spike_multiplier,
        # ATR (needed by risk.py after approval)
        "atr":                    round(float(current["ATR_14"]), 4),
        # Condition verdicts — lets the notifier flag which ones passed/failed
        "cond_rsi":               cond_rsi,
        "cond_ema":               cond_ema,
        "cond_macd":              cond_macd,
        "cond_volume":            cond_volume,
    }

    if triggered:
        logger.info(
            "BUY SIGNAL triggered — close=%.2f RSI=%.1f EMA_dist=%.2f%% "
            "MACD_cross=True vol_ratio=%.2fx",
            close, rsi, ema_dist_pct * 100, volume / vol_sma,
        )
    else:
        failed = [k for k in ("cond_rsi", "cond_ema", "cond_macd", "cond_volume")
                  if not context[k]]
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
