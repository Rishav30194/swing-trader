"""
notifier.py — Telegram notifications and reply handling.

All public functions are synchronous. python-telegram-bot v20+ uses asyncio
internally; each function wraps its async work with asyncio.run() so the rest
of the codebase stays synchronous.

Five public functions:
  send_signal_alert(symbol, signal_context)         — buy signal, full indicator snapshot
  send_execution_alert(symbol, order, position)     — entry fill confirmation
  send_exit_alert(symbol, position, reason, price)  — exit confirmation with P&L
  send_error_alert(error)                           — crash/error, never raises
  listen_for_reply(timeout_seconds)                 — poll for YES or NO reply

Note: send_exit_alert takes exit_price as an explicit argument. The spec lists
three positional args, but the price is needed for the P&L line and the caller
(main.py) always has it at the point of exit.
"""

import asyncio
import html
import logging
import time

import telegram
from telegram.constants import ParseMode

from src.config import settings
from src.risk import Position

logger = logging.getLogger(__name__)

# Created once at module import — reused for every send/poll call.
_bot = telegram.Bot(token=settings.telegram_bot_token)

_CHAT_ID    = settings.telegram_chat_id
_POLL_SECS  = 5   # long-poll timeout per getUpdates call

_EXIT_LABELS: dict[str, str] = {
    "sl":       "Hard stop-loss",
    "trailing": "Trailing stop",
    "tp":       "Take-profit",
    "day5":     "Day-5 force close",
    "manual":   "Manual close",
}


# ---------------------------------------------------------------------------
# Formatting helpers — pure functions, no I/O, straightforward to unit-test
# ---------------------------------------------------------------------------

def _fmt_signal(symbol: str, ctx: dict) -> str:
    close  = ctx.get("close",  0.0)
    ema_50 = ctx.get("ema_50", 0.0)
    rsi    = ctx.get("rsi",    0.0)
    atr    = ctx.get("atr",    0.0)
    sl     = ctx.get("stop_loss")
    tp     = ctx.get("take_profit")
    rsi_lo = ctx.get("rsi_lower_bound", 40)
    rsi_hi = ctx.get("rsi_upper_bound", 55)

    ema_pct = (close - ema_50) / ema_50 * 100 if ema_50 else 0.0
    sl_str  = f"${sl:.2f}  ({(sl - close) / close * 100:+.1f}%)" if sl else "—"
    tp_str  = f"${tp:.2f}  ({(tp - close) / close * 100:+.1f}%)" if tp else "—"

    return (
        f"🔔 <b>BUY SIGNAL — {symbol}</b>\n\n"
        f"<pre>"
        f"Price  : ${close:.2f}  (EMA50 ${ema_50:.2f}  {ema_pct:+.1f}%)\n"
        f"RSI    : {rsi:.1f}  [{rsi_lo:.0f}–{rsi_hi:.0f} ✓]\n"
        f"MACD   : bullish crossover ✓\n"
        f"ATR    : ${atr:.2f}\n\n"
        f"SL     : {sl_str}\n"
        f"TP     : {tp_str}"
        f"</pre>\n\n"
        f"Reply <b>YES</b> to approve or <b>NO</b> to skip."
    )


def _fmt_execution(symbol: str, order: dict, position: Position) -> str:
    fill  = order.get("filled_avg_price", position.entry_price)
    qty   = order.get("filled_qty", position.shares)
    value = fill * qty
    return (
        f"✅ <b>ENTRY FILLED — {symbol}</b>\n\n"
        f"<pre>"
        f"Shares : {qty}\n"
        f"Fill   : ${fill:.2f}  (${value:,.2f} total)\n"
        f"SL     : ${position.stop_loss:.2f}\n"
        f"TP     : ${position.take_profit:.2f}"
        f"</pre>"
    )


def _fmt_exit(
    symbol: str,
    position: Position,
    reason: str,
    exit_price: float,
) -> str:
    pnl_dollars = (exit_price - position.entry_price) * position.shares
    pnl_pct     = (exit_price - position.entry_price) / position.entry_price * 100
    label       = _EXIT_LABELS.get(reason, reason)
    icon        = "✅" if pnl_dollars >= 0 else "🔴"
    return (
        f"{icon} <b>EXIT — {symbol}</b>\n\n"
        f"<pre>"
        f"Reason : {label}\n"
        f"Exit   : ${exit_price:.2f}\n"
        f"Entry  : ${position.entry_price:.2f}\n"
        f"P&amp;L    : ${pnl_dollars:+,.2f}  ({pnl_pct:+.2f}%)"
        f"</pre>"
    )


def _fmt_error(error: Exception | str) -> str:
    # html.escape prevents malformed HTML if the error message contains < > &
    safe = html.escape(str(error))
    return f"🚨 <b>ERROR</b>\n\n<pre>{safe}</pre>"


# ---------------------------------------------------------------------------
# Async helpers
# ---------------------------------------------------------------------------

async def _send(text: str) -> None:
    await _bot.send_message(
        chat_id=_CHAT_ID,
        text=text,
        parse_mode=ParseMode.HTML,
    )


async def _listen_async(timeout_seconds: int) -> bool | None:
    # Drain the existing update queue so we only react to messages sent
    # AFTER this alert was dispatched. Without draining, a stale YES/NO
    # from a previous signal would immediately approve or reject this one.
    existing = await _bot.get_updates(timeout=0)
    offset: int | None = (existing[-1].update_id + 1) if existing else None

    deadline = time.monotonic() + timeout_seconds

    while time.monotonic() < deadline:
        remaining = int(deadline - time.monotonic())
        if remaining <= 0:
            break

        fetch_kwargs: dict = {
            "timeout":         min(_POLL_SECS, remaining),
            "allowed_updates": ["message"],
        }
        if offset is not None:
            fetch_kwargs["offset"] = offset

        try:
            updates = await _bot.get_updates(**fetch_kwargs)
        except Exception:
            logger.exception("getUpdates failed — will retry")
            await asyncio.sleep(2)
            continue

        for upd in updates:
            offset = upd.update_id + 1
            if not (upd.message and upd.message.text):
                continue
            reply = upd.message.text.strip().upper()
            if reply in ("YES", "Y"):
                logger.info("Received YES reply")
                return True
            if reply in ("NO", "N"):
                logger.info("Received NO reply")
                return False

    logger.info("listen_for_reply timed out after %ds", timeout_seconds)
    return None


# ---------------------------------------------------------------------------
# Public synchronous API
# ---------------------------------------------------------------------------

def send_signal_alert(symbol: str, signal_context: dict) -> None:
    """
    Send a buy signal alert to Telegram.

    signal_context is the dict from SignalResult.context, optionally enriched
    with 'stop_loss' and 'take_profit' keys by the caller before sending.
    The message must give the user enough information to approve or reject
    the trade without opening a laptop.
    """
    asyncio.run(_send(_fmt_signal(symbol, signal_context)))
    logger.info("Signal alert sent for %s", symbol)


def send_execution_alert(symbol: str, order: dict, position: Position) -> None:
    """
    Confirm an entry fill. order is the Alpaca order dict with at minimum
    'filled_avg_price' and 'filled_qty' keys.
    """
    asyncio.run(_send(_fmt_execution(symbol, order, position)))
    logger.info("Execution alert sent for %s", symbol)


def send_exit_alert(
    symbol: str,
    position: Position,
    reason: str,
    exit_price: float,
) -> None:
    """
    Confirm an exit. reason is one of: sl | trailing | tp | day5 | manual.
    exit_price is the actual fill price used to compute the P&L line.
    """
    asyncio.run(_send(_fmt_exit(symbol, position, reason, exit_price)))
    logger.info("Exit alert sent for %s reason=%s", symbol, reason)


def send_error_alert(error: Exception | str) -> None:
    """
    Send a crash or error notification. Never raises — catches its own
    exceptions and logs them. Must not depend on any state that could
    itself be broken when this is called.
    """
    try:
        asyncio.run(_send(_fmt_error(error)))
    except Exception:
        logger.exception("send_error_alert failed — could not reach Telegram")


def listen_for_reply(timeout_seconds: int) -> bool | None:
    """
    Poll Telegram for a YES or NO reply after a signal alert.

    Returns:
        True   — user replied YES or Y (approve trade)
        False  — user replied NO or N (skip trade)
        None   — no reply received within timeout_seconds
    """
    return asyncio.run(_listen_async(timeout_seconds))
