#!/usr/bin/env python3
"""
test_notional_order.py — Integration smoke-test for Alpaca notional order flow.

Run this ONCE during market hours (9:30–16:00 ET) against the paper account
before the scheduler fires its first real signal. It verifies that the Alpaca
paper API behaves as expected after switching from whole-share qty orders to
fractional notional orders.

Usage:
    python scripts/test_notional_order.py

What it checks:
  1. Each strategy symbol is fractionable on Alpaca (key risk — ADRs can be excluded)
  2. A $1.00 notional market buy fills with a real fractional qty > 0
  3. The filled_qty from the buy can be sold back as a fractional qty order
  4. The sell fills successfully

Must run in PAPER mode (ALPACA_PAPER=true in .env). Will refuse to run live.
"""

import sys
import time
from pathlib import Path

# Resolve project root so the script can be run from any directory.
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from src.config import settings
from src.executor import _client, _wait_for_fill

# Test a single liquid US-listed symbol. AAPL is reliably fractionable
# and cheap enough that $1 gives a meaningful fractional qty.
_TEST_SYMBOL   = "AAPL"
_TEST_NOTIONAL = 1.00   # $1 — smallest meaningful notional order


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _check_fractionable() -> bool:
    """Print fractionable status for all strategy symbols. Returns True if all pass."""
    print("\n── Fractional trading support ──────────────────────────────")
    all_ok = True
    for symbol in settings.symbols:
        try:
            asset = _client.get_asset(symbol)
            ok = getattr(asset, "fractionable", False)
            status = "FRACTIONABLE ✓" if ok else "NOT fractionable ✗"
            print(f"  {symbol:<6s}  {status}")
            if not ok:
                all_ok = False
        except Exception as exc:
            print(f"  {symbol:<6s}  ERROR — {exc}")
            all_ok = False
    return all_ok


def _place_notional_buy(symbol: str, notional: float) -> dict:
    """Submit a notional market buy and wait for fill."""
    print(f"\n── BUY  {symbol}  ${notional:.2f} notional ────────────────────────")
    order = _client.submit_order(MarketOrderRequest(
        symbol=symbol,
        notional=round(notional, 2),
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
    ))
    print(f"  Submitted  id={order.id}  status={order.status}")
    result = _wait_for_fill(str(order.id))
    print(f"  Filled     qty={result['filled_qty']:.6f}  "
          f"avg_price=${result['filled_avg_price']:.4f}  "
          f"status={result['status']}")
    return result


def _place_fractional_sell(symbol: str, shares: float) -> dict:
    """Submit a fractional qty market sell and wait for fill."""
    print(f"\n── SELL {symbol}  {shares:.6f} shares ────────────────────────")
    order = _client.submit_order(MarketOrderRequest(
        symbol=symbol,
        qty=shares,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
    ))
    print(f"  Submitted  id={order.id}  status={order.status}")
    result = _wait_for_fill(str(order.id))
    print(f"  Filled     qty={result['filled_qty']:.6f}  "
          f"avg_price=${result['filled_avg_price']:.4f}  "
          f"status={result['status']}")
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("═══════════════════════════════════════════════════════════")
    print("  Notional order integration test — PAPER mode")
    print("═══════════════════════════════════════════════════════════")

    # Safety guard — never run against live account
    if not settings.alpaca_paper:
        print("\nERROR: ALPACA_PAPER is not 'true'. This test must run in paper mode only.")
        sys.exit(1)

    print(f"\n  API key prefix : {settings.alpaca_api_key[:8]}…")
    print(f"  Test symbol    : {_TEST_SYMBOL}")
    print(f"  Test notional  : ${_TEST_NOTIONAL:.2f}")

    # ── 1. Fractionable check ────────────────────────────────────────────
    all_fractionable = _check_fractionable()
    if not all_fractionable:
        print("\nWARNING: One or more symbols are NOT fractionable.")
        print("         Signals on those symbols will fail at order placement.")
        print("         Consider removing them from SYMBOLS in .env before going live.\n")

    # ── 2. Notional buy ──────────────────────────────────────────────────
    try:
        buy = _place_notional_buy(_TEST_SYMBOL, _TEST_NOTIONAL)
    except Exception as exc:
        print(f"\nFAIL: notional buy raised an exception — {exc}")
        sys.exit(1)

    filled_qty   = buy["filled_qty"]
    fill_price   = buy["filled_avg_price"]
    buy_status   = buy["status"]

    if buy_status not in ("filled", "partially_filled"):
        print(f"\nFAIL: buy status is '{buy_status}' — expected 'filled'")
        sys.exit(1)

    if filled_qty <= 0.0:
        print(f"\nFAIL: filled_qty={filled_qty} — notional buy did not produce fractional shares")
        sys.exit(1)

    implied_notional = fill_price * filled_qty
    print(f"\n  Implied notional traded: ${implied_notional:.4f}  "
          f"(requested ${_TEST_NOTIONAL:.2f})")

    # Brief pause so the paper engine settles the position before selling
    time.sleep(2)

    # ── 3. Fractional sell ───────────────────────────────────────────────
    try:
        sell = _place_fractional_sell(_TEST_SYMBOL, filled_qty)
    except Exception as exc:
        print(f"\nFAIL: fractional sell raised an exception — {exc}")
        print(f"       You may have a residual {filled_qty:.6f} share position in paper account.")
        sys.exit(1)

    sell_status = sell["status"]

    if sell_status not in ("filled", "partially_filled"):
        print(f"\nFAIL: sell status is '{sell_status}' — expected 'filled'")
        sys.exit(1)

    # ── 4. Result ────────────────────────────────────────────────────────
    print("\n═══════════════════════════════════════════════════════════")
    if all_fractionable:
        print("  PASS — notional buy and fractional sell both work correctly.")
        print("         All symbols are fractionable. Ready for paper trading.")
    else:
        print("  PARTIAL PASS — order flow works, but review non-fractionable symbols above.")
    print("═══════════════════════════════════════════════════════════\n")


if __name__ == "__main__":
    main()
