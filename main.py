"""
main.py — APScheduler entry point for the regime-overlay rebalancer.

Two jobs:
  _run_rebalance        — weekly (default Fri 16:15 ET, after the close)
  _send_weekly_heartbeat — weekly (Sat 09:00 ET), independent liveness signal

Why after the close, and why weekly:
  The strategy reads completed daily bars and was validated executing at the
  NEXT session's open. Running after Friday's close and letting Alpaca queue
  market orders to Monday's open reproduces that exactly. Weekly rebalancing
  scored Sharpe 1.28 against daily's 1.33 at a third of the turnover, which is
  why there is no intraday scanner. See docs/strategy_validation.md.

Hard rules enforced here (from CLAUDE.md):
  - Target weights computed and validated before any order (rule 2)
  - Exposure reductions execute unconditionally, even if Telegram is down;
    only increases wait for the weekly YES (rule 3)
  - Equity fetched live on every rebalance (rule 4)
  - One sleeve per symbol, each capped at MAX_POSITION_PCT (rule 5)
  - Partial rebalances are logged, alerted, and never assumed complete (rule 7)
"""

import logging
import logging.handlers
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from src.config import settings
from src.data import get_historical_bars
from src.database import (
    get_regime_states,
    get_weekly_summary,
    init_db,
    log_event,
    log_rebalance_order,
    set_regime_state,
)
from src.executor import (
    get_account_equity,
    get_current_holdings,
    place_buy_order,
    place_sell_notional,
    place_sell_order,
)
from src.indicators import MIN_BARS_FOR_STRATEGY, compute_indicators
from src.notifier import (
    listen_for_reply,
    send_error_alert,
    send_rebalance_plan,
    send_rebalance_result,
    send_weekly_summary,
)
from src.portfolio import (
    RebalanceOrder,
    RegimeState,
    compute_regime_state,
    compute_target_weights,
    diff_to_orders,
    validate_target_weights,
)

logger = logging.getLogger(__name__)

_HEARTBEAT_DAY, _HEARTBEAT_HOUR, _HEARTBEAT_MINUTE = "sat", 9, 0
_SUMMARY_WINDOW_DAYS = 7


# ---------------------------------------------------------------------------
# Regime evaluation
# ---------------------------------------------------------------------------

def _evaluate_regimes(conn: sqlite3.Connection) -> dict[str, RegimeState]:
    """
    Compute the regime state for every symbol we can price.

    A symbol whose data fetch fails, or which lacks the history for SMA_200, is
    omitted entirely rather than defaulted. Omitting it means no target weight
    and therefore no order — an unpriceable sleeve is left exactly as it is,
    never liquidated on missing data.
    """
    previous = get_regime_states(conn)
    states: dict[str, RegimeState] = {}

    for symbol in settings.symbols:
        try:
            df = get_historical_bars(
                symbol,
                days=settings.bars_lookback_days,
                completed_only=True,
            )
        except Exception:
            logger.exception("%s: bar fetch failed — sleeve left untouched", symbol)
            continue

        if len(df) < MIN_BARS_FOR_STRATEGY:
            logger.warning(
                "%s: only %d bars, need %d for SMA_200 — sleeve left untouched",
                symbol, len(df), MIN_BARS_FOR_STRATEGY,
            )
            continue

        df = compute_indicators(df)
        state = compute_regime_state(
            df,
            band=settings.sma_band,
            currently_held=previous.get(symbol, False),
        )
        states[symbol] = state
        set_regime_state(
            conn, symbol, state.on,
            last_close=state.context.get("close"),
            last_sma_200=state.context.get("sma_200"),
        )

    return states


# ---------------------------------------------------------------------------
# Order execution
# ---------------------------------------------------------------------------

def _execute_order(
    conn: sqlite3.Connection,
    order: RebalanceOrder,
    holdings: dict[str, dict],
) -> dict:
    """
    Place one rebalance order and record the outcome.

    A full sleeve exit sells the exact share count so no fractional dust is
    left behind; a partial trim sells a dollar amount. Never raises — the
    result dict carries the status so one bad symbol cannot abort the rest
    of the rebalance (hard rule 7).
    """
    result = {
        "symbol": order.symbol, "side": order.side,
        "notional": order.notional, "status": "failed",
    }

    try:
        if order.side == "buy":
            resp = place_buy_order(order.symbol, order.notional)
        elif order.reason == "regime_exit":
            shares = holdings.get(order.symbol, {}).get("shares", 0.0)
            resp = place_sell_order(order.symbol, shares, order.reason)
        else:
            resp = place_sell_notional(order.symbol, order.notional, order.reason)
    except Exception as exc:
        logger.exception("%s: %s order failed", order.symbol, order.side)
        log_rebalance_order(
            conn, order.symbol, order.side, order.notional, order.reason,
            "failed", detail={"error": str(exc)},
        )
        return result

    result["status"] = "filled"
    log_rebalance_order(
        conn, order.symbol, order.side, order.notional, order.reason,
        "filled", order_id=resp.get("id"), detail=resp,
    )
    return result


def _execute_plan(
    conn: sqlite3.Connection,
    orders: list[RebalanceOrder],
    holdings: dict[str, dict],
    approved: bool,
) -> list[dict]:
    """
    Execute reductions unconditionally, then increases only if approved.

    Hard rule 3: a reduction must never depend on the Telegram reply, or on
    Telegram being reachable at all.
    """
    results: list[dict] = []

    for order in orders:
        if order.increases_exposure and not approved:
            logger.info("%s: increase skipped (not approved)", order.symbol)
            log_rebalance_order(
                conn, order.symbol, order.side, order.notional, order.reason,
                "skipped", detail={"reason": "not_approved"},
            )
            results.append({
                "symbol": order.symbol, "side": order.side,
                "notional": order.notional, "status": "skipped",
            })
            continue

        results.append(_execute_order(conn, order, holdings))

    return results


# ---------------------------------------------------------------------------
# Weekly rebalance
# ---------------------------------------------------------------------------

def _run_rebalance(conn: sqlite3.Connection) -> None:
    """One weekly cycle: evaluate regimes, plan, approve, execute, report."""
    logger.info("=== Rebalance start ===")

    try:
        equity = get_account_equity()
        holdings = get_current_holdings()
    except Exception:
        logger.exception("Account state fetch failed — aborting rebalance")
        send_error_alert("Rebalance aborted: could not fetch equity or holdings")
        return

    states = _evaluate_regimes(conn)
    if not states:
        logger.error("No symbols could be evaluated — aborting rebalance")
        send_error_alert("Rebalance aborted: no symbols could be evaluated")
        return

    # universe_size is the configured symbol count, not len(states) — a symbol
    # that failed to evaluate must leave its weight in cash, not hand it to the
    # sleeves that did evaluate.
    weights = compute_target_weights(
        states,
        universe_size=len(settings.symbols),
        max_position_pct=settings.max_position_pct,
    )
    validate_target_weights(weights, states, max_position_pct=settings.max_position_pct)

    current_notional = {s: v["notional"] for s, v in holdings.items()}
    orders = diff_to_orders(
        current_notional, weights, equity,
        min_order_notional=settings.min_order_notional,
        drift_tolerance=settings.drift_tolerance,
    )

    log_event(conn, "PORTFOLIO", "plan", {
        "equity": equity,
        "weights": weights,
        "orders": [vars(o) for o in orders],
    })

    send_rebalance_plan(orders, states, equity)

    if not orders:
        logger.info("No orders required — rebalance complete")
        logger.info("=== Rebalance end ===")
        return

    approved = _await_approval(orders)
    results = _execute_plan(conn, orders, holdings, approved)

    if any(r["status"] == "failed" for r in results):
        send_error_alert("One or more rebalance orders FAILED — see logs")

    send_rebalance_result(results)
    logger.info("=== Rebalance end ===")


def _await_approval(orders: list[RebalanceOrder]) -> bool:
    """
    Wait for the weekly YES, but only when there is something to approve.

    A timeout or a NO means increases are skipped; reductions run regardless,
    so the safe default needs no reply at all.
    """
    if not any(o.increases_exposure for o in orders):
        return False

    reply = listen_for_reply(settings.reply_timeout_secs)
    if reply is True:
        logger.info("Increases approved")
        return True

    logger.info("Increases not approved (%s)", "rejected" if reply is False else "timeout")
    return False


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

def _send_weekly_heartbeat(conn: sqlite3.Connection) -> None:
    """
    Independent liveness signal, separate from the rebalance job.

    Runs unconditionally so a rebalance that crashed, or a week with no orders,
    still produces evidence the process is alive. Equity is best-effort.
    """
    since = date.today() - timedelta(days=_SUMMARY_WINDOW_DAYS)
    summary = get_weekly_summary(conn, since)

    try:
        equity: float | None = get_account_equity()
    except Exception:
        logger.exception("Heartbeat: equity fetch failed — reporting unavailable")
        equity = None

    send_weekly_summary(summary, equity)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

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
    """Initialise state, then start the blocking weekly scheduler."""
    _configure_logging()

    conn = init_db("trades.db")
    scheduler = BlockingScheduler(timezone="America/New_York")

    scheduler.add_job(
        lambda: _run_rebalance(conn),
        trigger=CronTrigger(
            day_of_week=settings.rebalance_day,
            hour=settings.rebalance_hour,
            minute=settings.rebalance_minute,
            timezone="America/New_York",
        ),
        id="weekly_rebalance",
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        lambda: _send_weekly_heartbeat(conn),
        trigger=CronTrigger(
            day_of_week=_HEARTBEAT_DAY,
            hour=_HEARTBEAT_HOUR,
            minute=_HEARTBEAT_MINUTE,
            timezone="America/New_York",
        ),
        id="weekly_heartbeat",
        misfire_grace_time=3600,
    )

    logger.info(
        "Scheduler starting — paper=%s  symbols=%s  rebalance=%s %02d:%02d ET  band=%.1f%%",
        settings.alpaca_paper, list(settings.symbols), settings.rebalance_day,
        settings.rebalance_hour, settings.rebalance_minute, settings.sma_band * 100,
    )

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped by user")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
