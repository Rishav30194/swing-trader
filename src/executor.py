"""
executor.py — Alpaca order placement for the swing trader.

Three public functions:
  get_account_equity()                      — live equity fetch, never cached
  place_buy_order(symbol, notional)         — notional market buy, polls until filled
  place_sell_order(symbol, shares, reason)  — fractional market sell, polls until filled

Paper/live mode is set at startup from ALPACA_PAPER in .env. Each order
function re-reads os.getenv("ALPACA_PAPER") directly — not the cached
settings value — to satisfy the CLAUDE.md hard rule that this env var must
be checked on every call, not just at startup.

Fill polling: submit_order returns with status "new". Both place_* functions
call _wait_for_fill, which polls get_order_by_id every FILL_POLL_SECS until
the order is filled or FILL_TIMEOUT_SECS elapses. This gives callers an
actual filled_avg_price for the execution alert and DB record.
"""

import logging
import os
import time

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from src.config import settings

logger = logging.getLogger(__name__)

FILL_TIMEOUT_SECS = 30
FILL_POLL_SECS    = 1

_FILLED_STATUSES = {"filled", "partially_filled"}

_client = TradingClient(
    api_key=settings.alpaca_api_key,
    secret_key=settings.alpaca_api_secret,
    paper=settings.alpaca_paper,
)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _paper_mode() -> bool:
    """Re-read ALPACA_PAPER from the live env — never trust a cached value."""
    return os.getenv("ALPACA_PAPER", "true").strip().lower() == "true"


def _check_mode(symbol: str, side: str) -> None:
    """
    Log the current trading mode and raise if ALPACA_PAPER changed since startup.

    A changed value means the env was modified without restarting the process.
    Raising here prevents an ambiguous state where the TradingClient targets
    paper but the env says live (or vice versa).
    """
    paper_now = _paper_mode()
    logger.info("%s %s order — mode=%s", side, symbol, "PAPER" if paper_now else "LIVE")
    if paper_now != settings.alpaca_paper:
        raise RuntimeError(
            f"ALPACA_PAPER changed since startup "
            f"(startup={settings.alpaca_paper}, current={paper_now}). "
            "Restart the process to apply the new value safely."
        )


def _get_status(order) -> str:
    """Extract status string from an Order object (handles both enum and plain string)."""
    try:
        return order.status.value
    except AttributeError:
        return str(order.status)


def _order_to_dict(order) -> dict:
    """Convert an Alpaca Order object to a plain dict for logging and alerts."""
    try:
        side = order.side.value
    except AttributeError:
        side = str(order.side)

    return {
        "id":               str(order.id),
        "symbol":           str(order.symbol),
        "qty":              float(order.qty or 0),
        "filled_qty":       float(order.filled_qty or 0),
        "filled_avg_price": float(order.filled_avg_price or 0.0),
        "status":           _get_status(order),
        "side":             side,
        "created_at":       str(order.created_at),
    }


def _wait_for_fill(order_id: str) -> dict:
    """
    Poll order status until filled or FILL_TIMEOUT_SECS elapses.

    Alpaca paper fills are fast but submit_order returns with status "new".
    Polling here means place_buy_order / place_sell_order return an order
    dict with a real filled_avg_price rather than 0.0.
    Returns the last known state on timeout — the caller logs the warning.
    """
    deadline = time.monotonic() + FILL_TIMEOUT_SECS

    while time.monotonic() < deadline:
        order = _client.get_order_by_id(order_id)
        if _get_status(order) in _FILLED_STATUSES:
            logger.info(
                "Order %s filled: avg_price=%s filled_qty=%s",
                order_id, order.filled_avg_price, order.filled_qty,
            )
            return _order_to_dict(order)
        time.sleep(FILL_POLL_SECS)

    logger.warning(
        "Order %s not confirmed filled after %ds — returning last known state",
        order_id, FILL_TIMEOUT_SECS,
    )
    return _order_to_dict(_client.get_order_by_id(order_id))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_account_equity() -> float:
    """
    Fetch current account equity from Alpaca.

    Called fresh on every position size computation — never use a cached value.
    CLAUDE.md hard rule: position size must reflect current equity, not startup equity.

    Raises on API failure — caller is responsible for sending an error alert.
    """
    try:
        account = _client.get_account()
        equity  = float(account.equity)
        logger.info("Account equity: $%.2f", equity)
        return equity
    except Exception:
        logger.exception("Failed to fetch account equity")
        raise


def get_current_holdings() -> dict[str, dict]:
    """
    Fetch every open position from Alpaca as {symbol: {shares, notional}}.

    Alpaca is the source of truth for what is held — never the local database
    (hard rule 7). Re-deriving here means a partially-applied rebalance, a
    manual trade, or a crash mid-run all self-correct on the next cycle.

    Raises on API failure — the caller must abort the rebalance rather than
    act on an unknown portfolio state.
    """
    try:
        positions = _client.get_all_positions()
    except Exception:
        logger.exception("Failed to fetch current holdings")
        raise

    holdings = {
        str(p.symbol): {
            "shares":   float(p.qty),
            "notional": float(p.market_value),
        }
        for p in positions
    }
    logger.info("Current holdings: %s", {s: round(v["notional"], 2) for s, v in holdings.items()})
    return holdings


def place_sell_notional(symbol: str, notional: float, reason: str) -> dict:
    """
    Sell a dollar amount of `symbol` — used to trim a sleeve back to target.

    Full sleeve exits should use place_sell_order() with the exact share count
    instead, so no fractional dust is left behind.

    Raises on submission failure — caller must alert and handle.
    """
    _check_mode(symbol, "SELL")
    logger.info("Submitting SELL notional order: %s  $%.2f  reason=%s", symbol, notional, reason)

    try:
        order = _client.submit_order(MarketOrderRequest(
            symbol=symbol,
            notional=round(notional, 2),
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        ))
    except Exception:
        logger.exception("SELL notional submission failed for %s", symbol)
        raise

    result = _wait_for_fill(str(order.id))
    result["reason"] = reason
    logger.info("SELL complete: %s", result)
    return result


def place_buy_order(symbol: str, notional: float) -> dict:
    """
    Place a notional market buy order and wait for fill confirmation.

    Uses Alpaca's notional (dollar-amount) ordering so fractional shares are
    supported for all assets. The broker determines the fractional qty filled.

    Returns a dict with keys: id, symbol, qty, filled_qty, filled_avg_price,
    status, side, created_at.  filled_qty is the fractional shares received.
    Raises on submission failure — caller must send an error alert and not
    record a position in the database.
    """
    _check_mode(symbol, "BUY")
    logger.info("Submitting BUY notional order: %s  $%.2f", symbol, notional)

    try:
        order = _client.submit_order(MarketOrderRequest(
            symbol=symbol,
            notional=round(notional, 2),
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
        ))
    except Exception:
        logger.exception("BUY order submission failed for %s", symbol)
        raise

    result = _wait_for_fill(str(order.id))
    logger.info("BUY complete: %s", result)
    return result


def place_sell_order(symbol: str, shares: float, reason: str) -> dict:
    """
    Place a market sell order and wait for fill confirmation.

    shares may be fractional (the exact qty recorded at entry).
    reason is one of: sl | trailing | tp | day5 | manual. It is logged and
    included in the returned dict so the caller can pass it to send_exit_alert.
    Raises on submission failure — caller must alert and handle.
    """
    _check_mode(symbol, "SELL")
    logger.info(
        "Submitting SELL market order: %s × %.4f shares  reason=%s",
        symbol, shares, reason,
    )

    try:
        order = _client.submit_order(MarketOrderRequest(
            symbol=symbol,
            qty=shares,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        ))
    except Exception:
        logger.exception("SELL order submission failed for %s reason=%s", symbol, reason)
        raise

    result = _wait_for_fill(str(order.id))
    result["reason"] = reason
    logger.info("SELL complete: %s", result)
    return result
