#!/usr/bin/env python3
"""
backtest.py — Swing strategy backtester.

Fetches historical OHLCV data from Alpaca, simulates the full strategy
using our production modules, and prints a performance report.

Why not backtrader (which is listed in the implementation spec)?
Backtrader requires re-implementing strategy logic inside a bt.Strategy
class. Duplicating indicators.py + signals.py + risk.py into a parallel
framework introduces divergence risk — the backtest could pass while the
live code contains a different bug. A direct pandas simulation calls the
exact same functions the live scheduler calls, so a passing backtest
actually validates the production code.

Usage:
    python backtest.py                                         # 2022-2024
    python backtest.py --start 2022-01-01 --end 2022-12-31    # bear market
    python backtest.py --start 2023-01-01 --end 2024-12-31    # bull market
    python backtest.py --equity 50000                         # custom capital

Requirements:
    .env with ALPACA_API_KEY and ALPACA_API_SECRET
"""

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from src.config import settings
from src.data import get_historical_bars
from src.indicators import compute_indicators
from src.signals import evaluate_buy_signal
from src.risk import (
    Position,
    compute_exit_levels,
    compute_position_size,
    update_trailing_stop,
    check_exit_conditions,
)

logging.basicConfig(level=logging.WARNING)


# ---------------------------------------------------------------------------
# Trade record
# ---------------------------------------------------------------------------

@dataclass
class Trade:
    symbol:      str
    entry_date:  date
    entry_price: float
    shares:      int
    stop_loss:   float
    take_profit: float
    exit_date:   date  | None = None
    exit_price:  float | None = None
    exit_reason: str   | None = None

    @property
    def pnl_dollars(self) -> float:
        return (self.exit_price - self.entry_price) * self.shares  # type: ignore[operator]

    @property
    def pnl_pct(self) -> float:
        return (self.exit_price - self.entry_price) / self.entry_price * 100  # type: ignore[operator]

    @property
    def hold_days(self) -> int:
        return (self.exit_date - self.entry_date).days  # type: ignore[operator]


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_data(symbols: list[str], start: date, end: date) -> dict[str, pd.DataFrame]:
    """
    Fetch and enrich data for each symbol.
    Fetches extra bars before `start` so indicators are fully converged
    by the first simulation date.
    """
    days_needed = (date.today() - start).days + 90  # 90-day indicator warm-up buffer
    loaded: dict[str, pd.DataFrame] = {}

    for symbol in symbols:
        print(f"  Fetching {symbol}...", end=" ", flush=True)
        try:
            df = get_historical_bars(symbol, days=days_needed)
            df = compute_indicators(df)
            in_window = (
                (df["timestamp"].dt.date >= start) &
                (df["timestamp"].dt.date <= end)
            ).sum()
            loaded[symbol] = df
            print(f"{in_window} trading days  ({start} → {end})")
        except Exception as exc:
            print(f"FAILED — {exc}")

    return loaded


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

def run_simulation(
    data:           dict[str, pd.DataFrame],
    start:          date,
    end:            date,
    initial_equity: float,
) -> tuple[list[Trade], pd.Series]:
    """
    Walk forward bar by bar across all symbols.

    Entry: signal fires on bar close → enter at that bar's close price.
    Exit:  not checked on entry bar; from the next bar onward, check
           bar low vs stops and bar high vs take-profit.

    Returns:
        (completed_trades, daily_equity_series)
    """
    equity = initial_equity
    open_positions: list[tuple[Position, Trade]] = []
    completed:      list[Trade]                  = []
    daily_equity:   dict[date, float]            = {}

    all_dates = sorted({
        ts.date()
        for df in data.values()
        for ts in df["timestamp"]
        if start <= ts.date() <= end
    })

    for current_date in all_dates:

        # --- 1. Check exits for open positions --------------------------------
        still_open: list[tuple[Position, Trade]] = []

        for pos, trade in open_positions:
            df    = data[pos.symbol]
            today = df[df["timestamp"].dt.date == current_date]

            if today.empty or current_date == pos.entry_date:
                still_open.append((pos, trade))
                continue

            bar = today.iloc[0]

            if not pd.isna(bar["ATR_14"]):
                pos.trailing_stop = update_trailing_stop(
                    pos, float(bar["close"]), float(bar["ATR_14"]),
                    atr_stop_multiplier=settings.atr_stop_multiplier,
                    atr_trailing_activation=settings.atr_trailing_activation,
                )

            reason = check_exit_conditions(pos, bar)
            if reason:
                trade.exit_date   = current_date
                trade.exit_price  = _fill_price(pos, bar, reason)
                trade.exit_reason = reason
                equity += trade.pnl_dollars
                completed.append(trade)
            else:
                still_open.append((pos, trade))

        open_positions = still_open
        daily_equity[current_date] = equity

        # --- 2. Scan for new entries ------------------------------------------
        if len(open_positions) >= settings.max_open_positions:
            continue

        open_symbols = {pos.symbol for pos, _ in open_positions}

        for symbol, df in data.items():
            if len(open_positions) >= settings.max_open_positions:
                break
            if symbol in open_symbols:
                continue

            rows = df[df["timestamp"].dt.date <= current_date]
            if len(rows) < 2:
                continue
            if rows.iloc[-1]["timestamp"].date() != current_date:
                continue  # no bar for this symbol today

            signal = evaluate_buy_signal(
                rows,
                rsi_lower_bound=settings.rsi_lower_bound,
                rsi_upper_bound=settings.rsi_upper_bound,
            )
            if not signal.triggered:
                continue

            entry_price = float(rows.iloc[-1]["close"])
            atr         = signal.context["atr"]

            try:
                levels = compute_exit_levels(
                    entry_price, atr,
                    atr_stop_multiplier=settings.atr_stop_multiplier,
                    atr_tp_multiplier=settings.atr_tp_multiplier,
                )
            except ValueError:
                continue

            shares = compute_position_size(
                equity, settings.risk_per_trade_pct, entry_price, levels.stop_loss,
            )
            if shares == 0:
                continue

            pos = Position(
                symbol=symbol,
                entry_price=entry_price,
                shares=shares,
                stop_loss=levels.stop_loss,
                trailing_stop=levels.stop_loss,
                take_profit=levels.take_profit,
                entry_date=current_date,
            )
            trade = Trade(
                symbol=symbol,
                entry_date=current_date,
                entry_price=entry_price,
                shares=shares,
                stop_loss=levels.stop_loss,
                take_profit=levels.take_profit,
            )
            open_positions.append((pos, trade))

    # --- Force-close positions still open at backtest end --------------------
    for pos, trade in open_positions:
        df       = data[pos.symbol]
        last_bar = df[df["timestamp"].dt.date <= end].iloc[-1]
        trade.exit_date   = last_bar["timestamp"].date()
        trade.exit_price  = float(last_bar["close"])
        trade.exit_reason = "end_of_backtest"
        equity += trade.pnl_dollars
        completed.append(trade)

    return completed, pd.Series(daily_equity)


def _fill_price(pos: Position, bar: pd.Series, reason: str) -> float:
    """Assumed fill price for an exit. Fills exactly at the trigger level."""
    if reason == "sl":
        return pos.stop_loss
    if reason == "trailing":
        return pos.trailing_stop
    if reason == "tp":
        return pos.take_profit
    return float(bar["close"])


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def print_report(
    trades:         list[Trade],
    equity:         pd.Series,
    initial_equity: float,
    start:          date,
    end:            date,
) -> None:
    closed = [t for t in trades if t.exit_price is not None]

    if not closed:
        print("\n  No trades executed in this period.\n")
        return

    n       = len(closed)
    winners = [t for t in closed if t.pnl_dollars > 0]
    losers  = [t for t in closed if t.pnl_dollars <= 0]

    win_rate     = len(winners) / n * 100
    avg_hold     = sum(t.hold_days for t in closed) / n
    total_return = (equity.iloc[-1] - initial_equity) / initial_equity * 100

    gross_profit  = sum(t.pnl_dollars for t in winners) if winners else 0.0
    gross_loss    = abs(sum(t.pnl_dollars for t in losers)) if losers else 0.0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    best  = max(closed, key=lambda t: t.pnl_pct)
    worst = min(closed, key=lambda t: t.pnl_pct)

    daily_returns = equity.pct_change().dropna()
    sharpe = (
        daily_returns.mean() / daily_returns.std() * np.sqrt(252)
        if daily_returns.std() > 0 else 0.0
    )

    rolling_max = equity.cummax()
    max_dd      = ((equity - rolling_max) / rolling_max).min() * 100

    reasons: dict[str, int] = {}
    for t in closed:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1  # type: ignore[index]

    W = 58
    print("\n" + "=" * W)
    print("  SWING TRADER BACKTEST REPORT")
    print(f"  {start}  →  {end}")
    print("=" * W)
    print(f"  Initial equity       : ${initial_equity:>12,.2f}")
    print(f"  Final equity         : ${equity.iloc[-1]:>12,.2f}")
    print(f"  Total return         : {total_return:>+11.2f}%")
    print(f"  Sharpe ratio         : {sharpe:>12.3f}")
    print(f"  Max drawdown         : {max_dd:>11.2f}%")
    print("-" * W)
    print(f"  Total trades         : {n:>12}")
    print(f"  Win rate             : {win_rate:>11.1f}%")
    print(f"  Avg hold (days)      : {avg_hold:>12.1f}")
    print(f"  Profit factor        : {profit_factor:>12.2f}")
    print("-" * W)
    print(f"  Best  trade : {best.symbol}  {best.pnl_pct:>+.2f}%  (entered {best.entry_date})")
    print(f"  Worst trade : {worst.symbol}  {worst.pnl_pct:>+.2f}%  (entered {worst.entry_date})")
    print("-" * W)
    print("  Exit reasons:")
    for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
        bar_fill = "█" * int(count / n * 24)
        print(f"    {reason:<24}: {count:>3}  {bar_fill}")
    print("=" * W)
    print("\n  Phase 2 gate  (Sharpe > 0.5 and max drawdown < 25%):")
    sharpe_ok = sharpe > 0.5
    dd_ok     = max_dd > -25.0
    print(f"    Sharpe > 0.5     : {'PASS' if sharpe_ok else 'FAIL'}  ({sharpe:.3f})")
    print(f"    Max DD < 25%     : {'PASS' if dd_ok     else 'FAIL'}  ({max_dd:.2f}%)")
    gate = "PASS — ready for Phase 3" if (sharpe_ok and dd_ok) else "FAIL — do not proceed"
    print(f"    Verdict          : {gate}")
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Swing strategy backtester")
    p.add_argument("--start",  default="2022-01-01", help="Start date YYYY-MM-DD")
    p.add_argument("--end",    default="2024-12-31", help="End date   YYYY-MM-DD")
    p.add_argument("--equity", type=float, default=100_000.0,
                   help="Starting equity in USD (default: 100000)")
    return p.parse_args()


def main() -> None:
    args   = _parse_args()
    start  = date.fromisoformat(args.start)
    end    = date.fromisoformat(args.end)
    equity = args.equity

    print(f"\nSwing Trader Backtest")
    print(f"  Period   : {start}  →  {end}")
    print(f"  Symbols  : {', '.join(settings.symbols)}")
    print(f"  Equity   : ${equity:,.0f}\n")
    print("Loading data:")

    data = load_data(list(settings.symbols), start, end)
    if not data:
        print("ERROR: no data loaded. Check .env and Alpaca subscription.")
        sys.exit(1)

    print("\nRunning simulation...", flush=True)
    trades, equity_series = run_simulation(data, start, end, equity)
    print_report(trades, equity_series, equity, start, end)


if __name__ == "__main__":
    main()
