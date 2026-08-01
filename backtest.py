#!/usr/bin/env python3
"""
backtest.py — Regime-overlay backtester.

Fetches historical OHLCV data from Alpaca and simulates the strategy by calling
the SAME functions main.py calls: compute_regime_state, compute_target_weights,
diff_to_orders. Nothing about the strategy is reimplemented here, so a passing
backtest validates the production code rather than a parallel copy of it. The
previous strategy failed partly because the live path and the backtest path
disagreed about which bar to evaluate; sharing portfolio.py removes that class
of bug entirely.

Execution convention (matches live):
  Regime is evaluated on a completed daily close; orders fill at the NEXT
  session's open. Rebalances happen every `--rebalance` trading days.

Usage:
    python backtest.py                                        # full history
    python backtest.py --start 2022-01-01 --end 2022-12-31    # bear market
    python backtest.py --band 0.0 --rebalance 1               # daily, no band
    python backtest.py --benchmark                            # vs buy & hold

Requirements:
    .env with ALPACA_API_KEY and ALPACA_API_SECRET
"""

import argparse
import logging
import pickle
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from src.config import settings
from src.data import get_historical_bars
from src.indicators import compute_indicators
from src.portfolio import (
    compute_regime_state,
    compute_target_weights,
    diff_to_orders,
    validate_target_weights,
)

logging.basicConfig(level=logging.WARNING)

_CACHE_DIR = Path(__file__).parent / "data" / "cache"
_TRADING_DAYS = 252


# ---------------------------------------------------------------------------
# Local data cache
# ---------------------------------------------------------------------------

def _get_cached(symbol: str, start: date) -> pd.DataFrame | None:
    """Return cached DataFrame if it was written today and covers `start`. Otherwise None."""
    path = _CACHE_DIR / f"{symbol}_daily.pkl"
    if not path.exists():
        return None
    if datetime.fromtimestamp(path.stat().st_mtime).date() != date.today():
        return None
    try:
        with open(path, "rb") as fh:
            df = pickle.load(fh)
    except Exception:
        return None
    if df["timestamp"].dt.date.min() > start:
        return None  # cache doesn't reach back far enough for this run
    return df


def _save_cache(symbol: str, df: pd.DataFrame) -> None:
    """Write DataFrame to disk cache. Failures are non-fatal."""
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(_CACHE_DIR / f"{symbol}_daily.pkl", "wb") as fh:
            pickle.dump(df, fh)
    except Exception:
        pass


def load_data(symbols: list[str], start: date, end: date) -> dict[str, pd.DataFrame]:
    """
    Fetch and enrich data for each symbol, indexed by trading date.

    Requests 300 extra calendar days beyond `start` so SMA_200 is converged on
    the first bar actually simulated — a short warm-up would silently hold every
    sleeve flat instead of failing loudly.
    """
    days_needed = (date.today() - start).days + 300
    loaded: dict[str, pd.DataFrame] = {}

    for symbol in symbols:
        cached = _get_cached(symbol, start)
        if cached is not None:
            print(f"  Loading {symbol} from cache...", end=" ", flush=True)
            df = cached
        else:
            print(f"  Fetching {symbol}...", end=" ", flush=True)
            try:
                df = get_historical_bars(symbol, days=days_needed)
                df = compute_indicators(df)
                _save_cache(symbol, df)
            except Exception as exc:
                print(f"FAILED — {exc}")
                continue

        df = df.copy()
        df["date"] = df["timestamp"].dt.date
        in_window = ((df["date"] >= start) & (df["date"] <= end)).sum()
        loaded[symbol] = df.set_index("date")
        print(f"{in_window} trading days  ({start} → {end})")

    return loaded


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

@dataclass
class SimResult:
    equity: pd.Series
    orders: list[dict] = field(default_factory=list)
    invested_pct: float = 0.0
    turnover_per_year: float = 0.0


def run_simulation(
    data: dict[str, pd.DataFrame],
    start: date,
    end: date,
    initial_equity: float,
    *,
    band: float,
    rebalance_days: int,
    cost_bps: float,
) -> SimResult:
    """
    Walk forward, rebalancing every `rebalance_days` trading days.

    Regime is read from the bar at index i (a completed close) and the resulting
    orders fill at bar i+1's open — the same one-bar lag the live scheduler has.
    """
    symbols = sorted(data)
    dates = sorted({d for df in data.values() for d in df.index if start <= d <= end})
    cost = cost_bps / 10_000.0

    cash = initial_equity
    shares = {s: 0.0 for s in symbols}
    regime = {s: False for s in symbols}
    curve: dict[date, float] = {}
    orders_log: list[dict] = []
    invested_samples: list[float] = []
    traded_notional = 0.0

    for i, today in enumerate(dates):
        prior = dates[i - 1] if i else None

        if prior is not None and i % rebalance_days == 0:
            equity = cash + sum(
                shares[s] * _px(data[s], today, "open") for s in symbols
                if today in data[s].index
            )
            states = {}
            for s in symbols:
                if prior not in data[s].index:
                    continue
                row = data[s].loc[[prior]]
                states[s] = compute_regime_state(
                    row, band=band, currently_held=regime[s])

            if states and equity > 0:
                weights = compute_target_weights(
                    states,
                    universe_size=len(symbols),
                    max_position_pct=settings.max_position_pct,
                )
                validate_target_weights(
                    weights, states, max_position_pct=settings.max_position_pct)

                current = {
                    s: shares[s] * _px(data[s], today, "open")
                    for s in symbols if today in data[s].index
                }
                plan = diff_to_orders(
                    current, weights, equity,
                    min_order_notional=settings.min_order_notional,
                    drift_tolerance=settings.drift_tolerance,
                )
                for order in plan:
                    price = _px(data[order.symbol], today, "open")
                    if price <= 0:
                        continue
                    qty = order.notional / price
                    if order.side == "buy":
                        cash -= order.notional * (1 + cost)
                        shares[order.symbol] += qty
                    else:
                        cash += order.notional * (1 - cost)
                        shares[order.symbol] = max(0.0, shares[order.symbol] - qty)
                    traded_notional += order.notional
                    orders_log.append({
                        "date": today, "symbol": order.symbol, "side": order.side,
                        "notional": order.notional, "reason": order.reason,
                    })

                for s, st in states.items():
                    regime[s] = st.on

        mtm = sum(
            shares[s] * _px(data[s], today, "close") for s in symbols
            if today in data[s].index
        )
        total = cash + mtm
        curve[today] = total
        invested_samples.append(mtm / total if total > 0 else 0.0)

    years = max(len(dates) / _TRADING_DAYS, 1e-9)
    return SimResult(
        equity=pd.Series(curve),
        orders=orders_log,
        invested_pct=float(np.mean(invested_samples)) * 100 if invested_samples else 0.0,
        turnover_per_year=traded_notional / initial_equity / years,
    )


def buy_and_hold(
    data: dict[str, pd.DataFrame], start: date, end: date, initial_equity: float,
) -> pd.Series:
    """Equal-weight buy & hold benchmark, bought once at the first available open."""
    symbols = sorted(data)
    dates = sorted({d for df in data.values() for d in df.index if start <= d <= end})
    per = initial_equity / len(symbols)
    shares = {}
    for s in symbols:
        first = next((d for d in dates if d in data[s].index), None)
        shares[s] = per / _px(data[s], first, "open") if first else 0.0
    return pd.Series({
        d: sum(shares[s] * _px(data[s], d, "close")
               for s in symbols if d in data[s].index)
        for d in dates
    })


def _px(df: pd.DataFrame, when: date | None, field_name: str) -> float:
    if when is None or when not in df.index:
        return 0.0
    value = df.loc[when, field_name]
    return float(value) if not pd.isna(value) else 0.0


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _stats(equity: pd.Series, initial: float) -> dict:
    eq = equity.dropna()
    if len(eq) < 2:
        return {}
    rets = eq.pct_change().dropna()
    years = len(eq) / _TRADING_DAYS
    cagr = (eq.iloc[-1] / initial) ** (1 / years) - 1 if years > 0 else 0.0
    sharpe = (rets.mean() / rets.std() * np.sqrt(_TRADING_DAYS)
              if rets.std() > 0 else 0.0)
    max_dd = ((eq - eq.cummax()) / eq.cummax()).min()
    return {
        "final": eq.iloc[-1],
        "total_return": (eq.iloc[-1] - initial) / initial * 100,
        "cagr": cagr * 100,
        "sharpe": sharpe,
        "max_dd": max_dd * 100,
        "mar": cagr / abs(max_dd) if max_dd else float("nan"),
    }


def print_report(
    result: SimResult,
    initial_equity: float,
    start: date,
    end: date,
    benchmark: pd.Series | None = None,
) -> None:
    s = _stats(result.equity, initial_equity)
    if not s:
        print("\n  Not enough data to report.\n")
        return

    W = 62
    print("\n" + "=" * W)
    print("  REGIME OVERLAY BACKTEST REPORT")
    print(f"  {start}  →  {end}")
    print("=" * W)
    print(f"  Initial equity       : ${initial_equity:>12,.2f}")
    print(f"  Final equity         : ${s['final']:>12,.2f}")
    print(f"  Total return         : {s['total_return']:>+11.2f}%")
    print(f"  CAGR                 : {s['cagr']:>+11.2f}%")
    print(f"  Sharpe ratio         : {s['sharpe']:>12.2f}")
    print(f"  Max drawdown         : {s['max_dd']:>11.2f}%")
    print(f"  MAR (CAGR/maxDD)     : {s['mar']:>12.2f}")
    print("-" * W)
    print(f"  Rebalance orders     : {len(result.orders):>12}")
    print(f"  Turnover             : {result.turnover_per_year:>11.1f}x/yr")
    print(f"  Time invested        : {result.invested_pct:>11.1f}%")

    reasons: dict[str, int] = {}
    for o in result.orders:
        reasons[o["reason"]] = reasons.get(o["reason"], 0) + 1
    if reasons:
        print("-" * W)
        print("  Order reasons:")
        for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
            print(f"    {reason:<20}: {count:>4}")

    if benchmark is not None and len(benchmark) > 1:
        b = _stats(benchmark, initial_equity)
        print("-" * W)
        print("  vs equal-weight buy & hold:")
        print(f"    {'':<18}{'overlay':>12}{'buy & hold':>14}")
        print(f"    {'CAGR':<18}{s['cagr']:>11.2f}%{b['cagr']:>13.2f}%")
        print(f"    {'Sharpe':<18}{s['sharpe']:>12.2f}{b['sharpe']:>14.2f}")
        print(f"    {'Max drawdown':<18}{s['max_dd']:>11.2f}%{b['max_dd']:>13.2f}%")
        print(f"    {'MAR':<18}{s['mar']:>12.2f}{b['mar']:>14.2f}")
        print()
        print("  Expected shape: lower CAGR, materially shallower drawdown.")
        print("  This strategy is a drawdown-control device, not an alpha source —")
        print("  see docs/strategy_validation.md before reading anything else into it.")
    print("=" * W + "\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Regime overlay backtester")
    p.add_argument("--start", default="2018-11-01", help="Start date YYYY-MM-DD")
    p.add_argument("--end", default=date.today().isoformat(), help="End date YYYY-MM-DD")
    p.add_argument("--equity", type=float, default=100_000.0,
                   help="Starting equity in USD (default: 100000)")
    p.add_argument("--band", type=float, default=None,
                   help=f"Hysteresis band (default: settings value {settings.sma_band})")
    p.add_argument("--rebalance", type=int, default=5,
                   help="Trading days between rebalances (default: 5 = weekly)")
    p.add_argument("--cost-bps", type=float, default=5.0,
                   help="Per-side cost in basis points (default: 5)")
    p.add_argument("--benchmark", action="store_true",
                   help="Also report equal-weight buy & hold")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    band = args.band if args.band is not None else settings.sma_band

    print("\nRegime Overlay Backtest")
    print(f"  Period    : {start}  →  {end}")
    print(f"  Symbols   : {', '.join(settings.symbols)}")
    print(f"  Equity    : ${args.equity:,.0f}")
    print(f"  Band      : {band:.1%}   Rebalance: every {args.rebalance} trading days")
    print(f"  Cost      : {args.cost_bps:.1f} bps/side\n")
    print("Loading data:")

    data = load_data(list(settings.symbols), start, end)
    if not data:
        print("ERROR: no data loaded. Check .env and Alpaca subscription.")
        sys.exit(1)

    print("\nRunning simulation...", flush=True)
    result = run_simulation(
        data, start, end, args.equity,
        band=band, rebalance_days=args.rebalance, cost_bps=args.cost_bps,
    )
    bench = buy_and_hold(data, start, end, args.equity) if args.benchmark else None
    print_report(result, args.equity, start, end, bench)


if __name__ == "__main__":
    main()
