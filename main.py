"""
main.py — APScheduler entry point for the swing trader.

Two jobs run every 15 minutes during NYSE market hours (9:45–15:45 ET):
  _monitor_positions — check and execute exits for all open positions
  _scan_for_signals  — evaluate buy signals on the 8-symbol universe

Hard rules enforced here (from CLAUDE.md):
  - Maximum 2 open positions at any time
  - Stop-loss / trailing exits bypass the human Telegram gate
  - Day-5 force-close executes even if Telegram is unreachable
  - Stop-loss computed before every buy order
  - Position size from live account equity, never hardcoded
"""

import logging
import logging.handlers
import sqlite3
import zoneinfo
from datetime import date, datetime, time as dtime, timezone
from pathlib import Path

import pandas as pd
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetCalendarRequest
from apscheduler.schedulers.blocking import BlockingScheduler

from src.config import settings
from src.data import get_historical_bars
from src.database import (
    get_open_positions,
    init_db,
    log_event,
    save_position,
    update_position,
)
from src.executor import get_account_equity, place_buy_order, place_sell_order
from src.indicators import compute_indicators
from src.notifier import (
    listen_for_reply,
    send_error_alert,
    send_execution_alert,
    send_exit_alert,
    send_signal_alert,
)
from src.risk import (
    Position,
    check_exit_conditions,
    compute_exit_levels,
    compute_position_size,
    update_trailing_stop,
)
from src.signals import SignalResult, evaluate_buy_signal

logger = logging.getLogger(__name__)

_BARS_LOOKBACK = 90   # days of history for indicator warm-up
_ET = zoneinfo.ZoneInfo("America/New_York")

_cal_client = TradingClient(
    api_key=settings.alpaca_api_key,
    secret_key=settings.alpaca_api_secret,
    paper=settings.alpaca_paper,
)


# ---------------------------------------------------------------------------
# Market hours
# ---------------------------------------------------------------------------

def _is_market_open() -> bool:
    """Return True if NYSE is open and we're within the 9:45–15:45 ET window."""
    now_utc = datetime.now(timezone.utc)
    if now_utc.weekday() >= 5:
        return False

    et_now  = now_utc.astimezone(_ET)
    et_time = et_now.time()
    if not (dtime(9, 45) <= et_time <= dtime(15, 45)):
        return False

    today = et_now.date()
    try:
        calendars = _cal_client.get_calendar(GetCalendarRequest(start=today, end=today))
    except Exception:
        logger.exception("NYSE calendar fetch failed — assuming closed")
        return False

    return len(calendars) > 0


# ---------------------------------------------------------------------------
# Signal processing
# ---------------------------------------------------------------------------

def _process_signal(
    conn: sqlite3.Connection,
    symbol: str,
    signal: SignalResult,
) -> None:
    """Send Telegram alert, wait for reply, then hand off to _execute_entry."""
    ctx = dict(signal.context)
    atr   = ctx["atr"]
    close = ctx["close"]

    try:
        exits = compute_exit_levels(
            entry_price=close,
            atr=atr,
            atr_stop_multiplier=settings.atr_stop_multiplier,
            atr_tp_multiplier=settings.atr_tp_multiplier,
        )
    except ValueError:
        logger.exception("%s: invalid exit levels — skipping signal", symbol)
        send_error_alert(f"{symbol}: could not compute exit levels (ATR={atr:.4f})")
        return

    ctx["stop_loss"]   = exits.stop_loss
    ctx["take_profit"] = exits.take_profit

    log_event(conn, symbol, "signal", ctx)

    try:
        send_signal_alert(symbol, ctx)
    except Exception:
        logger.exception("%s: could not send signal alert — skipping", symbol)
        return

    reply = listen_for_reply(settings.reply_timeout_secs)

    if reply is not True:
        reason = "timeout" if reply is None else "rejected"
        log_event(conn, symbol, "rejected", {"reason": reason})
        logger.info("%s: signal %s", symbol, reason)
        return

    log_event(conn, symbol, "approved", {})
    _execute_entry(conn, symbol, close, exits)


def _execute_entry(
    conn: sqlite3.Connection,
    symbol: str,
    close: float,
    exits,
) -> None:
    """Fetch equity, size, place notional buy, persist to DB, confirm via Telegram."""
    try:
        equity = get_account_equity()
    except Exception:
        logger.exception("%s: equity fetch failed — aborting entry", symbol)
        send_error_alert(f"{symbol}: equity fetch failed — entry aborted")
        return

    shares_float = compute_position_size(
        equity=equity,
        risk_pct=settings.risk_per_trade_pct,
        entry_price=close,
        stop_loss=exits.stop_loss,
    )
    # Cap position at max_position_pct of equity regardless of risk sizing.
    # On a $1,000 account with max_position_pct=0.25, no single position
    # can exceed $250 — prevents any one trade consuming the entire account.
    uncapped_notional = shares_float * close
    max_notional      = equity * settings.max_position_pct
    notional          = min(uncapped_notional, max_notional)

    if notional < 1.0:
        logger.warning("%s: notional $%.2f too small — entry skipped", symbol, notional)
        send_error_alert(f"{symbol}: notional too small (${notional:.2f}) — skipped")
        return

    logger.info(
        "%s: notional=$%.2f (risk-sized=$%.2f cap=$%.2f)",
        symbol, notional, uncapped_notional, max_notional,
    )

    try:
        order = place_buy_order(symbol, notional)
    except Exception:
        logger.exception("%s: buy order failed", symbol)
        send_error_alert(f"{symbol}: buy order failed — see logs")
        return

    fill_price = order["filled_avg_price"] or close
    filled_qty = order["filled_qty"] or (notional / close)

    position = Position(
        symbol=symbol,
        entry_price=fill_price,
        shares=filled_qty,
        stop_loss=exits.stop_loss,
        trailing_stop=exits.stop_loss,
        take_profit=exits.take_profit,
        entry_date=date.today(),
    )
    position.db_id = save_position(conn, position)

    log_event(conn, symbol, "bought", {
        "order_id":    order["id"],
        "fill_price":  fill_price,
        "shares":      filled_qty,
        "notional":    notional,
        "stop_loss":   exits.stop_loss,
        "take_profit": exits.take_profit,
    })

    try:
        send_execution_alert(symbol, order, position)
    except Exception:
        logger.exception("%s: could not send execution alert", symbol)


# ---------------------------------------------------------------------------
# Position monitoring
# ---------------------------------------------------------------------------

def _check_position(conn: sqlite3.Connection, pos: Position) -> None:
    """Evaluate one open position: check exits, then update trailing stop."""
    try:
        df = get_historical_bars(pos.symbol, days=_BARS_LOOKBACK)
    except Exception:
        logger.exception("%s: bar fetch failed — skipping monitor", pos.symbol)
        return

    df          = compute_indicators(df)
    current_bar = df.iloc[-1]

    # Check exits with the trailing stop as it stood entering this bar.
    # If an exit fires, we sell and return — no trailing stop update needed.
    reason = check_exit_conditions(pos, current_bar)

    if reason is not None:
        _execute_exit(conn, pos, reason, current_bar)
        return

    # No exit — ratchet the trailing stop for the next cycle.
    if not pd.isna(current_bar["ATR_14"]):
        new_stop = update_trailing_stop(
            pos,
            float(current_bar["close"]),
            float(current_bar["ATR_14"]),
            atr_stop_multiplier=settings.atr_stop_multiplier,
            atr_trailing_activation=settings.atr_trailing_activation,
        )
        if new_stop > pos.trailing_stop:
            update_position(conn, pos.db_id, trailing_stop=new_stop)
            log_event(conn, pos.symbol, "stop_updated", {"trailing_stop": new_stop})


def _execute_exit(
    conn: sqlite3.Connection,
    pos: Position,
    reason: str,
    current_bar: pd.Series,
) -> None:
    """Place sell, update DB, and send notification. DB update always runs."""
    try:
        order = place_sell_order(pos.symbol, pos.shares, reason)
    except Exception:
        logger.exception("%s: SELL failed (reason=%s) — position stays open", pos.symbol, reason)
        send_error_alert(f"{pos.symbol}: SELL FAILED ({reason}) — MANUAL ACTION REQUIRED")
        return

    exit_price  = order.get("filled_avg_price") or float(current_bar["close"])
    pnl_dollars = (exit_price - pos.entry_price) * pos.shares
    pnl_pct     = (exit_price - pos.entry_price) / pos.entry_price * 100

    # DB update runs before the notification so Day-5 close is always persisted
    # even if Telegram is unreachable (CLAUDE.md hard rule #7).
    update_position(
        conn, pos.db_id,
        status="closed",
        exit_date=date.today().isoformat(),
        exit_price=exit_price,
        exit_reason=reason,
        pnl_dollars=pnl_dollars,
        pnl_pct=pnl_pct,
    )
    log_event(conn, pos.symbol, "sold", {
        "order_id":    order["id"],
        "exit_price":  exit_price,
        "reason":      reason,
        "pnl_dollars": pnl_dollars,
        "pnl_pct":     pnl_pct,
    })

    try:
        send_exit_alert(pos.symbol, pos, reason, exit_price)
    except Exception:
        logger.exception("%s: exit alert failed — trade is closed in DB", pos.symbol)


def _monitor_positions(conn: sqlite3.Connection) -> None:
    """Check all open positions for exit conditions."""
    positions = get_open_positions(conn)
    if not positions:
        return

    for pos in positions:
        try:
            _check_position(conn, pos)
        except Exception:
            logger.exception("Monitor error for %s (db_id=%s)", pos.symbol, pos.db_id)
            send_error_alert(f"Monitor error for {pos.symbol} — see logs")


# ---------------------------------------------------------------------------
# Signal scanning
# ---------------------------------------------------------------------------

def _evaluate_symbol(conn: sqlite3.Connection, symbol: str) -> None:
    """Fetch bars, compute indicators, and process any triggered signal."""
    # Re-check position count: a YES reply earlier in this scan cycle may
    # have already filled the maximum, so we stop before placing a new order.
    if len(get_open_positions(conn)) >= settings.max_open_positions:
        logger.info("Max open positions reached mid-scan — stopping")
        return

    try:
        df = get_historical_bars(symbol, days=_BARS_LOOKBACK)
    except Exception:
        logger.exception("%s: bar fetch failed — skipping", symbol)
        return

    df = compute_indicators(df)

    signal = evaluate_buy_signal(
        df,
        rsi_lower_bound=settings.rsi_lower_bound,
        rsi_upper_bound=settings.rsi_upper_bound,
    )

    if signal.triggered:
        logger.info("%s: BUY SIGNAL", symbol)
        _process_signal(conn, symbol, signal)


def _scan_for_signals(conn: sqlite3.Connection) -> None:
    """Scan each unoccupied symbol for a buy signal."""
    open_positions = get_open_positions(conn)

    if len(open_positions) >= settings.max_open_positions:
        logger.info("Max open positions (%d) reached — skipping scan", settings.max_open_positions)
        return

    held = {pos.symbol for pos in open_positions}

    for symbol in settings.symbols:
        if symbol in held:
            continue
        try:
            _evaluate_symbol(conn, symbol)
        except Exception:
            logger.exception("%s: unhandled error during scan", symbol)


# ---------------------------------------------------------------------------
# Scheduler entry points
# ---------------------------------------------------------------------------

def _run_cycle(conn: sqlite3.Connection) -> None:
    """One 15-minute tick: monitor exits first, then scan for entries."""
    if not _is_market_open():
        logger.debug("Market closed — skipping cycle")
        return

    logger.info("=== Cycle start ===")
    try:
        _monitor_positions(conn)
    except Exception:
        logger.exception("Unhandled error in _monitor_positions")
        send_error_alert("Unhandled error in monitor loop — see logs")

    try:
        _scan_for_signals(conn)
    except Exception:
        logger.exception("Unhandled error in _scan_for_signals")
        send_error_alert("Unhandled error in scan loop — see logs")

    logger.info("=== Cycle end ===")


def _configure_logging() -> None:
    """Configure console and rotating file logging."""
    Path("logs").mkdir(exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console = logging.StreamHandler()
    console.setFormatter(fmt)

    file_handler = logging.handlers.RotatingFileHandler(
        "logs/app.log", maxBytes=10_000_000, backupCount=5,
    )
    file_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(console)
    root.addHandler(file_handler)


def main() -> None:
    """Initialise state, then start the blocking 15-minute scheduler."""
    _configure_logging()

    conn = init_db("trades.db")

    scheduler = BlockingScheduler(timezone="America/New_York")
    scheduler.add_job(
        lambda: _run_cycle(conn),
        trigger="interval",
        minutes=15,
        next_run_time=datetime.now(timezone.utc),
        id="main_cycle",
        misfire_grace_time=60,
    )

    logger.info(
        "Scheduler starting — paper=%s  symbols=%s",
        settings.alpaca_paper, list(settings.symbols),
    )

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped by user")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
