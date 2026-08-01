#!/usr/bin/env python3
"""
validate_oos.py — Pressure-testing for the regime overlay.

The previous version tested discrete trade P&Ls (permutation on win rate,
bootstrap CIs). The overlay does not produce independent trades — it produces an
exposure path — so those tests do not apply. These three do, and they are the
ones that decided the strategy:

  1. Matched-exposure control — the overlay runs ~71% invested, so the fair null
     is a portfolio held at a CONSTANT 71%, not buy & hold. Cash earns 0%, so
     scaling by a constant leaves Sharpe unchanged; if the static control matches
     the overlay, the timing machinery earns nothing. This is the test that
     rejected volatility targeting.

  2. Train/test split — an edge that only exists in the half containing the
     crashes is regime dependence, not skill.

  3. Block bootstrap on drawdown — resample 63-day blocks and ask how often the
     overlay's drawdown is actually shallower. The point estimate alone cannot
     distinguish a real effect from one lucky crash.

Usage:
    python validate_oos.py
    python validate_oos.py --split 2023-01-01
    python validate_oos.py --resamples 5000
"""

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from src.config import settings
from backtest import buy_and_hold, load_data, run_simulation, _px, _stats

logging.basicConfig(level=logging.WARNING)

_DATA_START = date(2018, 1, 1)
_DEFAULT_START = date(2018, 11, 1)
_DEFAULT_SPLIT = date(2023, 1, 1)
_TRADING_DAYS = 252
_BLOCK = 63          # one quarter — long enough to preserve drawdown structure


@dataclass
class Row:
    label: str
    stats: dict
    invested: float


def run_static(
    data: dict[str, pd.DataFrame],
    start: date,
    end: date,
    initial_equity: float,
    exposure: float,
    *,
    rebalance_days: int = 5,
    cost_bps: float = 5.0,
) -> pd.Series:
    """
    Hold every symbol at a constant `exposure` fraction, rest in cash.

    This is the null hypothesis for any exposure-management strategy.
    """
    symbols = sorted(data)
    dates = sorted({d for df in data.values() for d in df.index if start <= d <= end})
    cost = cost_bps / 10_000.0
    cash = initial_equity
    shares = {s: 0.0 for s in symbols}
    curve: dict[date, float] = {}

    for i, today in enumerate(dates):
        if i % rebalance_days == 0:
            live = [s for s in symbols if today in data[s].index]
            equity = cash + sum(shares[s] * _px(data[s], today, "open") for s in live)
            for s in live:
                price = _px(data[s], today, "open")
                if price <= 0:
                    continue
                want = (equity * exposure / len(symbols)) / price
                delta = want - shares[s]
                if abs(delta * price) < equity * 0.001:
                    continue
                cash -= delta * price * (1 + cost if delta > 0 else 1 - cost)
                shares[s] = want
        curve[today] = cash + sum(
            shares[s] * _px(data[s], today, "close")
            for s in symbols if today in data[s].index
        )
    return pd.Series(curve)


def _max_dd(returns: np.ndarray) -> float:
    eq = np.cumprod(1 + returns)
    return float((eq / np.maximum.accumulate(eq) - 1).min())


def block_bootstrap_drawdown(
    overlay: pd.Series, benchmark: pd.Series, resamples: int, seed: int = 11,
) -> float:
    """Fraction of block resamples in which the overlay's drawdown is shallower."""
    rng = np.random.default_rng(seed)
    a = overlay.pct_change().dropna().to_numpy()
    b = benchmark.pct_change().dropna().to_numpy()
    n = min(len(a), len(b))
    a, b = a[-n:], b[-n:]
    if n <= _BLOCK:
        return float("nan")

    wins = 0
    for _ in range(resamples):
        starts = rng.integers(0, n - _BLOCK, size=max(1, n // _BLOCK))
        idx = np.concatenate([np.arange(s, s + _BLOCK) for s in starts])
        if _max_dd(a[idx]) > _max_dd(b[idx]):
            wins += 1
    return wins / resamples


def evaluate_window(
    data: dict[str, pd.DataFrame], start: date, end: date, equity: float, band: float,
) -> tuple[list[Row], pd.Series, pd.Series]:
    """Run overlay, matched-exposure static control, and buy & hold over one window."""
    result = run_simulation(
        data, start, end, equity, band=band, rebalance_days=5, cost_bps=5.0)
    exposure = result.invested_pct / 100.0
    static = run_static(data, start, end, equity, exposure)
    bench = buy_and_hold(data, start, end, equity)

    rows = [
        Row("buy & hold", _stats(bench, equity), 100.0),
        Row(f"STATIC {exposure:.0%} (matched)", _stats(static, equity), exposure * 100),
        Row("regime overlay", _stats(result.equity, equity), result.invested_pct),
    ]
    return rows, result.equity, bench


def print_window(label: str, rows: list[Row]) -> None:
    print(f"\n--- {label} ---")
    print(f"  {'design':<28}{'CAGR%':>9}{'Sharpe':>9}{'maxDD%':>9}{'MAR':>7}{'invested%':>11}")
    for r in rows:
        if not r.stats:
            continue
        s = r.stats
        print(f"  {r.label:<28}{s['cagr']:>8.2f}%{s['sharpe']:>9.2f}"
              f"{s['max_dd']:>8.2f}%{s['mar']:>7.2f}{r.invested:>10.1f}%")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Regime overlay validation")
    p.add_argument("--start", default=_DEFAULT_START.isoformat())
    p.add_argument("--end", default=date.today().isoformat())
    p.add_argument("--split", default=_DEFAULT_SPLIT.isoformat(),
                   help="Train/test boundary (default 2023-01-01)")
    p.add_argument("--equity", type=float, default=100_000.0)
    p.add_argument("--band", type=float, default=None)
    p.add_argument("--resamples", type=int, default=2000)
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    split = date.fromisoformat(args.split)
    band = args.band if args.band is not None else settings.sma_band

    print("\nRegime Overlay Validation")
    print(f"  Period  : {start} → {end}   (split {split})")
    print(f"  Symbols : {', '.join(settings.symbols)}")
    print(f"  Band    : {band:.1%}\n")
    print("Loading data:")

    data = load_data(list(settings.symbols), _DATA_START, end)
    if not data:
        print("ERROR: no data loaded. Check .env and Alpaca subscription.")
        sys.exit(1)

    print("\n" + "=" * 78)
    print("  1 + 2.  MATCHED-EXPOSURE CONTROL, ACROSS TRAIN / TEST")
    print("=" * 78)

    windows = [("full", start, end), ("train", start, split), ("test", split, end)]
    curves: dict[str, tuple[pd.Series, pd.Series]] = {}
    for label, w_start, w_end in windows:
        rows, overlay_curve, bench_curve = evaluate_window(
            data, w_start, w_end, args.equity, band)
        print_window(f"{label}  ({w_start} → {w_end})", rows)
        curves[label] = (overlay_curve, bench_curve)

    print("\n" + "=" * 78)
    print(f"  3.  BLOCK BOOTSTRAP ON DRAWDOWN  ({args.resamples} resamples, "
          f"{_BLOCK}-day blocks)")
    print("=" * 78)
    for label, (overlay_curve, bench_curve) in curves.items():
        frac = block_bootstrap_drawdown(overlay_curve, bench_curve, args.resamples)
        print(f"  {label:<8} overlay drawdown shallower in {frac:>6.1%} of resamples")

    print("\n" + "=" * 78)
    print("  HOW TO READ THIS")
    print("=" * 78)
    print("  The overlay EARNS its complexity only if it beats the matched STATIC")
    print("  control — not buy & hold — on drawdown and MAR, in BOTH halves.")
    print("  Expect no Sharpe advantage in the test half; that is a known limit,")
    print("  not a regression. A bootstrap figure near 50% would mean the drawdown")
    print("  reduction is noise.\n")


if __name__ == "__main__":
    main()
