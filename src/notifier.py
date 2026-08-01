"""
notifier.py — Telegram notifications and reply handling.

All public functions are synchronous. python-telegram-bot v20+ uses asyncio
internally; each function wraps its async work with asyncio.run() so the rest
of the codebase stays synchronous.

Five public functions:
  send_rebalance_plan(orders, states, equity)  — weekly plan, asks for one YES
  send_rebalance_result(results)               — what actually executed
  send_error_alert(error)                      — crash/error, never raises
  send_weekly_summary(summary, equity)         — weekly heartbeat, never raises
  listen_for_reply(timeout_seconds)            — poll for YES or NO reply

Every send function is failure-tolerant and returns a bool rather than raising.
Hard rule 3 requires exposure reductions to execute even when Telegram is
unreachable, so no caller may be forced into an exception path by a failed send.
"""

import asyncio
import html
import logging
import time

import telegram
from telegram.constants import ParseMode

from src.config import settings
from src.database import WeeklySummary
from src.portfolio import RebalanceOrder, RegimeState

logger = logging.getLogger(__name__)

# Created once at module import — reused for every send/poll call.
_bot = telegram.Bot(token=settings.telegram_bot_token)

_CHAT_ID   = settings.telegram_chat_id
_POLL_SECS = 5   # long-poll timeout per getUpdates call

_REASON_LABELS: dict[str, str] = {
    "regime_entry": "regime entry",
    "regime_exit":  "regime exit",
    "drift":        "drift trim",
}


# ---------------------------------------------------------------------------
# Formatting helpers — pure functions, no I/O, straightforward to unit-test
# ---------------------------------------------------------------------------

def _fmt_sleeve_line(symbol: str, state: RegimeState) -> str:
    ctx = state.context
    mark = "●" if state.on else "○"
    close = ctx.get("close")
    sma = ctx.get("sma_200")
    if close is None or sma is None:
        return f"{mark} {symbol:<6} (no data)"
    gap = (close / sma - 1) * 100 if sma else 0.0
    return f"{mark} {symbol:<6} {close:>9,.2f}  SMA200 {sma:>9,.2f}  {gap:+.1f}%"


def _fmt_order_line(order: RebalanceOrder) -> str:
    label = _REASON_LABELS.get(order.reason, order.reason)
    return f"{order.side.upper():<4} {order.symbol:<6} ${order.notional:>9,.2f}  {label}"


def _fmt_rebalance_plan(
    orders: list[RebalanceOrder],
    states: dict[str, RegimeState],
    equity: float,
) -> str:
    increases = [o for o in orders if o.increases_exposure]
    reductions = [o for o in orders if not o.increases_exposure]

    sleeves = "\n".join(_fmt_sleeve_line(s, st) for s, st in sorted(states.items()))

    if orders:
        plan_lines = "\n".join(_fmt_order_line(o) for o in orders)
    else:
        plan_lines = "no changes needed"

    buy_total = sum(o.notional for o in increases)
    if increases:
        ask = (
            f"Reply <b>YES</b> to approve {len(increases)} increase(s) "
            f"totalling ${buy_total:,.2f}, or <b>NO</b> to skip them."
        )
    else:
        ask = "No increases to approve — nothing to reply to."

    reduction_note = (
        f"\n{len(reductions)} reduction(s) execute automatically."
        if reductions else ""
    )

    return (
        f"🔄 <b>Weekly Rebalance</b>\n\n"
        f"<pre>"
        f"Equity : ${equity:,.2f}\n\n"
        f"Sleeves\n{sleeves}\n\n"
        f"Plan\n{plan_lines}"
        f"</pre>"
        f"{reduction_note}\n\n{ask}"
    )


def _fmt_rebalance_result(results: list[dict]) -> str:
    if not results:
        return "✅ <b>Rebalance complete</b>\n\nNo orders were required."

    filled = [r for r in results if r["status"] == "filled"]
    partial = [r for r in results if r["status"] == "partial"]
    failed = [r for r in results if r["status"] == "failed"]
    skipped = [r for r in results if r["status"] == "skipped"]

    lines = []
    for r in results:
        icon = {"filled": "✓", "partial": "◐",
                "failed": "✗", "skipped": "–"}.get(r["status"], "?")
        lines.append(f"{icon} {r['side'].upper():<4} {r['symbol']:<6} ${r['notional']:>9,.2f}")

    counts = f"filled {len(filled)}  failed {len(failed)}  skipped {len(skipped)}"
    if partial:
        counts += f"  partial {len(partial)}"

    problems = len(failed) + len(partial)
    icon = "🚨" if problems else "✅"
    tail = (f"\n\n{problems} order(s) did not fill cleanly — portfolio is NOT at "
            f"target. Check the logs.") if problems else ""

    return (
        f"{icon} <b>Rebalance complete</b>\n\n"
        f"<pre>"
        f"{chr(10).join(lines)}\n\n"
        f"{counts}"
        f"</pre>{tail}"
    )


def _fmt_error(error: Exception | str) -> str:
    # html.escape prevents malformed HTML if the error message contains < > &
    safe = html.escape(str(error))
    return f"🚨 <b>ERROR</b>\n\n<pre>{safe}</pre>"


def _fmt_weekly(summary: WeeklySummary, equity: float | None) -> str:
    start = summary.period_start.strftime("%b %d")
    # Drop the repeated month on the end date within the same month: "Jun 02–08".
    end_fmt = "%d" if summary.period_start.month == summary.period_end.month else "%b %d"
    end = summary.period_end.strftime(end_fmt)

    equity_str = f"${equity:,.2f}" if equity is not None else "unavailable ⚠️"
    on_str = ", ".join(summary.sleeves_on) if summary.sleeves_on else "none"
    off_str = ", ".join(summary.sleeves_off) if summary.sleeves_off else "none"

    traded = summary.notional_bought + summary.notional_sold
    if summary.orders_filled or summary.orders_failed:
        orders_str = (
            f"{summary.orders_filled} filled / {summary.orders_failed} failed  "
            f"(${traded:,.2f})"
        )
    else:
        orders_str = "none"

    return (
        f"📊 <b>Weekly Summary — {start}–{end}</b>\n\n"
        f"<pre>"
        f"Status   : ✅ Running\n"
        f"Equity   : {equity_str}\n"
        f"Invested : {on_str}\n"
        f"In cash  : {off_str}\n"
        f"Orders   : {orders_str}"
        f"</pre>"
    )


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
    # from a previous week would immediately approve or reject this one.
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

def send_rebalance_plan(
    orders: list[RebalanceOrder],
    states: dict[str, RegimeState],
    equity: float,
) -> bool:
    """
    Send the weekly rebalance plan: every sleeve's regime state and the orders
    that follow from it.

    The message must let the user approve or reject without opening a laptop,
    so it shows the close, the 200-day SMA, and the gap for every sleeve.

    Returns True if the message was delivered. Never raises — a failed send
    must not stop reductions from executing (hard rule 3).
    """
    try:
        asyncio.run(_send(_fmt_rebalance_plan(orders, states, equity)))
        logger.info("Rebalance plan sent (%d orders)", len(orders))
        return True
    except Exception:
        logger.exception("send_rebalance_plan failed — proceeding with reductions anyway")
        return False


def send_rebalance_result(results: list[dict]) -> bool:
    """
    Report what actually executed. `results` entries need keys:
    symbol, side, notional, status (filled | failed | skipped).

    Never raises.
    """
    try:
        asyncio.run(_send(_fmt_rebalance_result(results)))
        logger.info("Rebalance result sent (%d orders)", len(results))
        return True
    except Exception:
        logger.exception("send_rebalance_result failed — could not reach Telegram")
        return False


def send_error_alert(error: Exception | str) -> bool:
    """
    Send a crash or error notification. Never raises — catches its own
    exceptions and logs them. Must not depend on any state that could
    itself be broken when this is called.
    """
    try:
        asyncio.run(_send(_fmt_error(error)))
        return True
    except Exception:
        logger.exception("send_error_alert failed — could not reach Telegram")
        return False


def send_weekly_summary(summary: WeeklySummary, equity: float | None) -> bool:
    """
    Send the weekly heartbeat summary. Never raises — a failed heartbeat must
    not disturb the scheduler. equity is None when the account fetch failed,
    which the message renders as 'unavailable' so the problem is still visible.
    """
    try:
        asyncio.run(_send(_fmt_weekly(summary, equity)))
        logger.info("Weekly summary sent")
        return True
    except Exception:
        logger.exception("send_weekly_summary failed — could not reach Telegram")
        return False


def listen_for_reply(timeout_seconds: int) -> bool | None:
    """
    Poll Telegram for a YES or NO reply after the rebalance plan is sent.

    Returns:
        True   — user replied YES or Y (approve exposure increases)
        False  — user replied NO or N (skip increases)
        None   — no reply received within timeout_seconds (treated as skip)
    """
    return asyncio.run(_listen_async(timeout_seconds))
