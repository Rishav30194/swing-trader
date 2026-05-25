#!/usr/bin/env python3
"""
validate_oos.py — Statistical pressure-testing of OOS strategy results.

Three tests:
  1. Walk-forward  — run frozen strategy on each calendar year independently
                     to check regime consistency (no re-optimisation between years).
  2. Permutation   — sign-randomise OOS trade P&Ls 10,000× to test whether
                     the observed win rate is statistically above 50% (H₀: p=0.5).
  3. Bootstrap CI  — resample OOS trade P&Ls with replacement 10,000× to
                     report 90% confidence intervals on key metrics.

Usage:
    python validate_oos.py
    python validate_oos.py --oos-start 2025-01-01 --oos-end 2025-12-31
    python validate_oos.py --permutations 5000
    python validate_oos.py --no-walk-forward
    python validate_oos.py --equity 50000
"""

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from src.config import settings
from backtest import Trade, load_data, run_simulation

logging.basicConfig(level=logging.WARNING)

# Earliest date for data fetch — covers 2019+ walk-forward windows with warm-up.
# Alpaca SIP feed provides this depth; earlier dates may fail for some symbols.
_DATA_START         = date(2018, 1, 1)
_DEFAULT_OOS_START  = date(2025, 1, 1)
_DEFAULT_OOS_END    = date(2025, 12, 31)
_WALK_FORWARD_YEARS = [2019, 2020, 2021, 2022, 2023, 2024, 2025]


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class WalkForwardResult:
    year:           int
    n_trades:       int
    win_rate_pct:   float
    sharpe:         float
    max_dd_pct:     float
    net_return_pct: float


@dataclass
class PermutationResult:
    n_trades:        int
    actual_win_rate: float   # fraction, e.g. 0.76
    p_value:         float
    n_permutations:  int
    null_p50:        float   # median of null distribution (should be ~0.50)
    null_p95:        float   # 95th pct of null (upper tail of "expected by luck")


@dataclass
class BootstrapCI:
    metric: str
    actual: float
    p5:     float
    p50:    float
    p95:    float


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------

def _equity_stats(equity: pd.Series, initial: float) -> tuple[float, float, float]:
    """Return (annualised_sharpe, max_dd_pct, net_return_pct) from daily equity series."""
    if equity.empty:
        return 0.0, 0.0, 0.0
    net_return  = (equity.iloc[-1] - initial) / initial * 100
    daily_ret   = equity.pct_change().dropna()
    sharpe = (
        float(daily_ret.mean() / daily_ret.std() * np.sqrt(252))
        if len(daily_ret) > 1 and daily_ret.std() > 0 else 0.0
    )
    rolling_max = equity.cummax()
    max_dd      = float(((equity - rolling_max) / rolling_max).min() * 100)
    return round(sharpe, 3), round(max_dd, 2), round(float(net_return), 2)


# ---------------------------------------------------------------------------
# Test 1: Walk-Forward
# ---------------------------------------------------------------------------

def run_walk_forward(
    data:           dict[str, pd.DataFrame],
    initial_equity: float,
    years:          list[int],
) -> list[WalkForwardResult]:
    """
    Run the frozen strategy on each calendar year independently.

    Each year starts with `initial_equity` — years are not compounded.
    Positions open at year-end are force-closed at the last available bar.
    A year with no signals scores zero across all metrics.
    """
    results: list[WalkForwardResult] = []

    for year in years:
        start  = date(year, 1, 1)
        end    = date(year, 12, 31)
        trades, equity = run_simulation(data, start, end, initial_equity)
        closed = [t for t in trades if t.exit_price is not None]

        if not closed:
            results.append(WalkForwardResult(year, 0, 0.0, 0.0, 0.0, 0.0))
            continue

        winners   = sum(1 for t in closed if t.pnl_dollars > 0)
        win_rate  = winners / len(closed) * 100
        sharpe, max_dd, net_ret = _equity_stats(equity, initial_equity)
        results.append(WalkForwardResult(
            year=year,
            n_trades=len(closed),
            win_rate_pct=round(win_rate, 1),
            sharpe=sharpe,
            max_dd_pct=max_dd,
            net_return_pct=net_ret,
        ))

    return results


# ---------------------------------------------------------------------------
# Test 2: Permutation Test (sign randomisation)
# ---------------------------------------------------------------------------

def run_permutation_test(
    trades:         list[Trade],
    n_permutations: int = 10_000,
    rng_seed:       int = 42,
) -> PermutationResult | None:
    """
    Sign-randomisation test on trade P&L signs.

    Null hypothesis: each trade is equally likely to be a win or a loss (p = 0.5).
    Under H₀ we simulate the null distribution by randomly assigning +/- signs
    to each trade 10,000 times and recording the resulting win rate.

    p-value = fraction of permutations whose win rate ≥ actual win rate.

    Note: magnitude of individual P&Ls does not affect win-rate significance —
    only whether a trade was positive or negative. Profit factor and mean return
    are addressed by the bootstrap.

    Returns None if fewer than 5 closed trades (result would be meaningless).
    """
    closed = [t for t in trades if t.exit_price is not None]
    if len(closed) < 5:
        return None

    pnls            = np.array([t.pnl_pct for t in closed])
    actual_win_rate = float((pnls > 0).mean())

    rng = np.random.default_rng(rng_seed)
    # Shape: (n_permutations, n_trades). Each entry is +1 (win) or -1 (loss).
    signs          = rng.choice([-1.0, 1.0], size=(n_permutations, len(pnls)))
    null_win_rates = (signs > 0).mean(axis=1)

    p_value = float((null_win_rates >= actual_win_rate).mean())

    return PermutationResult(
        n_trades=len(closed),
        actual_win_rate=actual_win_rate,
        p_value=p_value,
        n_permutations=n_permutations,
        null_p50=float(np.percentile(null_win_rates, 50)),
        null_p95=float(np.percentile(null_win_rates, 95)),
    )


# ---------------------------------------------------------------------------
# Test 3: Bootstrap Confidence Intervals
# ---------------------------------------------------------------------------

def run_bootstrap(
    trades:       list[Trade],
    n_iterations: int = 10_000,
    rng_seed:     int = 42,
) -> list[BootstrapCI] | None:
    """
    Bootstrap resampling of OOS trade P&L% values.

    Draws N samples with replacement (N = actual trade count) n_iterations times.
    Reports 5th / 50th / 95th percentiles on three metrics:

      win_rate_%       — fraction of winning trades × 100
      mean_trade_ret_% — average per-trade P&L %
      profit_factor    — gross profit / gross loss (capped at 50 to handle inf)

    Wide CI bands reveal low sample power — the honest cost of a short OOS window.
    Returns None if fewer than 5 closed trades.
    """
    closed = [t for t in trades if t.exit_price is not None]
    if len(closed) < 5:
        return None

    pnls = np.array([t.pnl_pct for t in closed])
    n    = len(pnls)
    rng  = np.random.default_rng(rng_seed)

    # samples shape: (n_iterations, n_trades)
    samples   = rng.choice(pnls, size=(n_iterations, n), replace=True)
    win_rates = (samples > 0).mean(axis=1) * 100
    mean_rets = samples.mean(axis=1)
    wins_sum  = np.where(samples > 0, samples, 0.0).sum(axis=1)
    loss_sum  = np.abs(np.where(samples < 0, samples, 0.0).sum(axis=1))
    safe_loss = np.where(loss_sum > 0, loss_sum, 1.0)   # prevent division by zero
    pf_dist   = np.clip(np.where(loss_sum > 0, wins_sum / safe_loss, 50.0), 0, 50)

    def _actual_pf(p: np.ndarray) -> float:
        w = p[p > 0].sum()
        l = abs(p[p < 0].sum())
        return min(w / l, 50.0) if l > 0 else 50.0

    def _ci(name: str, actual: float, dist: np.ndarray) -> BootstrapCI:
        return BootstrapCI(
            metric=name,
            actual=round(actual, 3),
            p5=round(float(np.percentile(dist, 5)), 3),
            p50=round(float(np.percentile(dist, 50)), 3),
            p95=round(float(np.percentile(dist, 95)), 3),
        )

    return [
        _ci("win_rate_%",        float((pnls > 0).mean() * 100), win_rates),
        _ci("mean_trade_ret_%",  float(pnls.mean()),              mean_rets),
        _ci("profit_factor",     _actual_pf(pnls),               pf_dist),
    ]


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _p_value_label(p: float) -> str:
    if p < 0.01:
        return "strong evidence of edge (p < 0.01)"
    if p < 0.05:
        return "significant (p < 0.05)"
    if p < 0.10:
        return "marginal (p < 0.10) — treat with caution"
    return "NOT significant (p >= 0.10) — may be luck"


def _wf_summary(results: list[WalkForwardResult]) -> str:
    with_trades    = [r for r in results if r.n_trades > 0]
    profitable     = [r for r in with_trades if r.net_return_pct > 0]
    positive_sharpe = [r for r in with_trades if r.sharpe > 0]
    if not with_trades:
        return "no years with trades"
    return (
        f"profitable in {len(profitable)}/{len(with_trades)} years with trades  |  "
        f"positive Sharpe in {len(positive_sharpe)}/{len(with_trades)}"
    )


def print_report(
    oos_trades:     list[Trade],
    oos_equity:     pd.Series,
    wf_results:     list[WalkForwardResult] | None,
    perm:           PermutationResult | None,
    bootstrap:      list[BootstrapCI] | None,
    oos_start:      date,
    oos_end:        date,
    initial_equity: float,
) -> None:
    W = 68
    closed = [t for t in oos_trades if t.exit_price is not None]

    print("\n" + "=" * W)
    print("  OOS STATISTICAL VALIDATION REPORT")
    print(f"  OOS window : {oos_start}  →  {oos_end}")
    print("=" * W)

    # --- OOS summary ----------------------------------------------------------
    if not closed:
        print("\n  No OOS trades executed — cannot run statistical tests.")
        print("=" * W + "\n")
        return

    winners      = sum(1 for t in closed if t.pnl_dollars > 0)
    oos_win_rate = winners / len(closed) * 100
    sharpe, max_dd, net_ret = _equity_stats(oos_equity, initial_equity)

    print(f"\n  OOS completed trades : {len(closed)}")
    print(f"  OOS win rate         : {oos_win_rate:.1f}%")
    print(f"  OOS net return       : {net_ret:+.2f}%")
    print(f"  OOS Sharpe           : {sharpe:.3f}")
    print(f"  OOS max drawdown     : {max_dd:.2f}%")

    # --- Walk-forward ---------------------------------------------------------
    if wf_results:
        print("\n" + "-" * W)
        print("  WALK-FORWARD  (frozen strategy, each year independently)")
        print(f"  {'Year':<8}{'Trades':>8}{'Win%':>8}{'Sharpe':>10}{'MaxDD%':>10}{'Return%':>10}")
        print("  " + "-" * 54)
        for r in wf_results:
            if r.n_trades == 0:
                print(f"  {r.year:<8}{'—':>8}{'—':>8}{'—':>10}{'—':>10}{'—':>10}  (no signals)")
                continue
            flag = "  <-- OOS" if r.year == oos_start.year else ""
            print(
                f"  {r.year:<8}{r.n_trades:>8}{r.win_rate_pct:>7.1f}%"
                f"{r.sharpe:>10.3f}{r.max_dd_pct:>9.2f}%{r.net_return_pct:>9.2f}%{flag}"
            )
        print(f"\n  Summary: {_wf_summary(wf_results)}")

    # --- Permutation test -----------------------------------------------------
    print("\n" + "-" * W)
    if perm:
        print("  PERMUTATION TEST  (sign randomisation, H₀: win rate = 50%)")
        print(f"  Permutations         : {perm.n_permutations:,}")
        print(f"  Actual win rate      : {perm.actual_win_rate:.1%}")
        print(f"  Null distribution    : median {perm.null_p50:.1%}  |  p95 {perm.null_p95:.1%}")
        print(f"  p-value              : {perm.p_value:.4f}")
        print(f"  Verdict              : {_p_value_label(perm.p_value)}")
        print()
        if perm.n_trades < 20:
            print(f"  ⚠  Only {perm.n_trades} trades — even a significant p-value has wide error bars.")
            print(f"     A single lucky streak in {perm.n_trades} trades can produce p < 0.05.")
    else:
        print("  PERMUTATION TEST  — skipped (fewer than 5 OOS trades).")

    # --- Bootstrap CIs --------------------------------------------------------
    print("\n" + "-" * W)
    if bootstrap:
        print("  BOOTSTRAP 90% CONFIDENCE INTERVALS  (10,000 resamples)")
        print(f"  {'Metric':<24}{'Actual':>10}{'p5':>10}{'p50':>10}{'p95':>10}")
        print("  " + "-" * 54)
        for ci in bootstrap:
            print(
                f"  {ci.metric:<24}{ci.actual:>10.3f}"
                f"{ci.p5:>10.3f}{ci.p50:>10.3f}{ci.p95:>10.3f}"
            )
        print()
        print("  Interpretation:")
        print("  • Narrow bands = reliable estimate.  Wide bands = small sample, high uncertainty.")
        wr_ci = next((c for c in bootstrap if c.metric == "win_rate_%"), None)
        if wr_ci and wr_ci.p5 < 50.0:
            print(f"  ⚠  p5 win rate {wr_ci.p5:.1f}% < 50% — on an unlucky run the edge disappears.")
        elif wr_ci:
            print(f"  ✓  p5 win rate {wr_ci.p5:.1f}% > 50% — edge persists even on unlucky resamples.")
    else:
        print("  BOOTSTRAP CI — skipped (fewer than 5 OOS trades).")

    print("=" * W + "\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Statistical OOS validation for the swing strategy")
    p.add_argument("--oos-start",       default="2025-01-01", help="OOS start date YYYY-MM-DD")
    p.add_argument("--oos-end",         default="2025-12-31", help="OOS end date   YYYY-MM-DD")
    p.add_argument("--equity",          type=float, default=100_000.0, help="Starting equity (default: 100000)")
    p.add_argument("--permutations",    type=int,   default=10_000,    help="Permutation test iterations (default: 10000)")
    p.add_argument("--no-walk-forward", action="store_true",           help="Skip walk-forward (faster)")
    return p.parse_args()


def main() -> None:
    args      = _parse_args()
    oos_start = date.fromisoformat(args.oos_start)
    oos_end   = date.fromisoformat(args.oos_end)
    equity    = args.equity

    print(f"\nOOS Statistical Validation")
    print(f"  OOS window : {oos_start}  →  {oos_end}")
    print(f"  Symbols    : {', '.join(settings.symbols)}")
    print(f"  Equity     : ${equity:,.0f}\n")

    print("Loading data (from 2018 for walk-forward warm-up):")
    data = load_data(list(settings.symbols), _DATA_START, oos_end)
    if not data:
        print("ERROR: no data loaded. Check .env and Alpaca subscription.")
        sys.exit(1)

    print("\nRunning OOS simulation...", flush=True)
    oos_trades, oos_equity = run_simulation(data, oos_start, oos_end, equity)

    wf_results: list[WalkForwardResult] | None = None
    if not args.no_walk_forward:
        print("Running walk-forward analysis...", flush=True)
        wf_results = run_walk_forward(data, equity, _WALK_FORWARD_YEARS)

    print("Running permutation test...", flush=True)
    perm = run_permutation_test(oos_trades, n_permutations=args.permutations)

    print("Running bootstrap CI...", flush=True)
    bootstrap = run_bootstrap(oos_trades)

    print_report(oos_trades, oos_equity, wf_results, perm, bootstrap, oos_start, oos_end, equity)


if __name__ == "__main__":
    main()
