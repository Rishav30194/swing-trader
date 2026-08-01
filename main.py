"""
main.py — APScheduler entry point for the regime-overlay rebalancer.

Two jobs:
  _run_rebalance        — weekly (default Fri 16:15 ET, after the close)
  _send_weekly_heartbeat — weekly (Sat 09:00 ET), independent liveness signal

Why after the close, and why weekly:
  The strategy reads completed daily bars and was validated executing at the
  NEXT session's open. Running after Friday's close and letting Alpaca queue
  market orders to Monday's open reproduces that exactly. Weekly rebalancing
  captures almost all of daily's benefit at a third of the turnover, which is
  why there is no intraday scanner.

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
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from src.config import settings
from src.data import get_historical_bars
from src.database import (
    get_regime_states,
    get_strategy_cash,
    get_weekly_summary,
    init_db,
    log_event,
    log_rebalance_order,
    set_regime_state,
    set_strategy_cash,
)
from src.executor import (
    get_account_cash,
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

        try:
            df = compute_indicators(df)
            state = compute_regime_state(
                df,
                band=settings.sma_band,
                currently_held=previous.get(symbol, False),
            )
            set_regime_state(
                conn, symbol, state.on,
                last_close=state.context.get("close"),
                last_sma_200=state.context.get("sma_200"),
            )
        except Exception:
            # Omit rather than default: an un-evaluable sleeve keeps whatever
            # position it has and receives no target weight.
            logger.exception("%s: regime evaluation failed — sleeve left untouched", symbol)
            continue

        states[symbol] = state

    return states


# ---------------------------------------------------------------------------
# Order execution
# ---------------------------------------------------------------------------

def _classify_fill(response: dict) -> str:
    """
    Map an Alpaca order status onto what actually happened to our money.

    A submission that raises no exception has NOT necessarily filled: the order
    can sit in "new"/"accepted", or come back "rejected" or "canceled", and
    _wait_for_fill returns the last known state once it times out. Recording
    those as filled would put a trade in the database that never happened.
    """
    status = str(response.get("status") or "").lower()
    if status == "filled":
        return "filled"
    if status == "partially_filled":
        return "partial"
    return "failed"


def _actual_notional(response: dict, requested: float) -> float:
    """Dollar value actually transacted, falling back to the requested amount."""
    qty = float(response.get("filled_qty") or 0.0)
    price = float(response.get("filled_avg_price") or 0.0)
    filled = qty * price
    return filled if filled > 0 else requested


def _execute_order(
    conn: sqlite3.Connection,
    order: RebalanceOrder,
    holdings: dict[str, dict],
) -> dict:
    """
    Place one rebalance order and record what actually happened.

    A full sleeve exit sells the exact share count so no fractional dust is
    left behind; a partial trim sells a dollar amount. Never raises — the
    result dict carries the status so one bad symbol cannot abort the rest
    of the rebalance (hard rule 7).
    """
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
        return {"symbol": order.symbol, "side": order.side,
                "notional": order.notional, "status": "failed"}

    status = _classify_fill(resp)
    notional = _actual_notional(resp, order.notional)

    if status != "filled":
        logger.error(
            "%s: %s order did not fill cleanly — Alpaca status=%s filled_qty=%s",
            order.symbol, order.side, resp.get("status"), resp.get("filled_qty"),
        )

    log_rebalance_order(
        conn, order.symbol, order.side, notional, order.reason,
        status, order_id=resp.get("id"), detail=resp,
    )
    return {"symbol": order.symbol, "side": order.side,
            "notional": notional, "status": status}


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

def compute_strategy_equity(
    holdings: dict[str, dict],
    strategy_cash: float,
    account_equity: float,
    account_cash: float,
) -> float:
    """
    Capital this strategy is allowed to deploy — NOT the account balance.

    A paper account funded with $100,000 must still trade the $1,000 allocated
    to it, so sizing uses:

        managed sleeve value  +  the strategy's own cash ledger

    Profits compound: once the sleeves are worth $1,100 the strategy sizes off
    $1,100. Money in the account that was never allocated is invisible to it.

    Both account figures act as ceilings. If the ledger ever claims more than
    the account actually holds — a manual withdrawal, a mis-recorded fill — the
    account wins, because we can never deploy money that is not there.
    """
    managed_value = sum(
        v["notional"] for s, v in holdings.items() if s in settings.symbols
    )
    equity = managed_value + min(strategy_cash, account_cash)

    if equity > account_equity:
        logger.warning(
            "Strategy ledger ($%.2f) exceeds account equity ($%.2f) — capping",
            equity, account_equity,
        )
        equity = account_equity

    logger.info(
        "Strategy equity $%.2f (managed $%.2f + cash $%.2f) vs account $%.2f",
        equity, managed_value, strategy_cash, account_equity,
    )
    return equity


def _warn_on_unmanaged_holdings(holdings: dict[str, dict]) -> None:
    """
    Flag positions in the account that this strategy does not manage.

    Sizing uses total account equity, so anything held outside SYMBOLS inflates
    every sleeve's target. The rebalancer will never sell such a position — it
    only ever acts on symbols in the configured universe — but the user needs to
    know the account is not dedicated, because the sizing assumption is wrong.
    """
    foreign = sorted(set(holdings) - set(settings.symbols))
    if not foreign:
        return

    value = sum(holdings[s]["notional"] for s in foreign)
    logger.warning(
        "Account holds %d unmanaged position(s) worth $%.2f: %s — sleeve targets "
        "are sized off total equity and will be too large",
        len(foreign), value, ", ".join(foreign),
    )
    send_error_alert(
        f"Unmanaged positions detected ({', '.join(foreign)}, ${value:,.2f}). "
        "Sleeve targets are sized off total equity, so they are inflated. "
        "This account should hold only the configured symbols."
    )


def _run_rebalance(conn: sqlite3.Connection) -> None:
    """One weekly cycle: evaluate regimes, plan, approve, execute, report."""
    logger.info("=== Rebalance start ===")

    try:
        account_equity = get_account_equity()
        account_cash = get_account_cash()
        holdings = get_current_holdings()
    except Exception:
        logger.exception("Account state fetch failed — aborting rebalance")
        send_error_alert("Rebalance aborted: could not fetch equity or holdings")
        return

    shorts = sorted(
        s for s in settings.symbols
        if holdings.get(s, {}).get("shares", 0.0) < 0
    )
    if shorts:
        # This strategy is long-only. A negative position would make
        # current_notional negative and inflate the resulting buy, so refuse to
        # act rather than compute an order against an impossible state.
        logger.error("Short position(s) detected in managed symbols: %s", shorts)
        send_error_alert(
            f"Rebalance aborted: short position(s) in {', '.join(shorts)}. "
            "This strategy is long-only — close them manually first."
        )
        return

    _warn_on_unmanaged_holdings(holdings)

    strategy_cash = get_strategy_cash(conn, settings.trading_capital)
    equity = compute_strategy_equity(
        holdings, strategy_cash, account_equity, account_cash)

    states = _evaluate_regimes(conn)
    if not states:
        logger.error("No symbols could be evaluated — aborting rebalance")
        send_error_alert("Rebalance aborted: no symbols could be evaluated")
        return

    # A raise anywhere in here means a hard-rule violation or a sizing bug.
    # Without the guard it would escape into APScheduler, which logs it and
    # carries on — the rebalance would fail silently every week with nobody told.
    try:
        # universe_size is the configured symbol count, not len(states): a symbol
        # that failed to evaluate must leave its weight in cash rather than hand
        # it to the sleeves that did evaluate.
        weights = compute_target_weights(
            states,
            universe_size=len(settings.symbols),
            max_position_pct=settings.max_position_pct,
        )
        validate_target_weights(
            weights, states, max_position_pct=settings.max_position_pct)

        current_notional = {s: v["notional"] for s, v in holdings.items()}
        orders = diff_to_orders(
            current_notional, weights, equity,
            min_order_notional=settings.min_order_notional,
            drift_tolerance=settings.drift_tolerance,
        )
    except Exception as exc:
        logger.exception("Target weight computation failed — no orders placed")
        send_error_alert(f"Rebalance aborted: {exc}")
        return

    affordable = sum(o.notional for o in orders if o.increases_exposure)
    if affordable > account_cash:
        logger.warning(
            "Planned buys $%.2f exceed account cash $%.2f — broker may reject some",
            affordable, account_cash,
        )

    log_event(conn, "PORTFOLIO", "plan", {
        "equity": equity,
        "weights": weights,
        "orders": [vars(o) for o in orders],
    })

    send_rebalance_plan(orders, states, equity, account_equity)

    if not orders:
        logger.info("No orders required — rebalance complete")
        logger.info("=== Rebalance end ===")
        return

    approved = _await_approval(orders)
    results = _execute_plan(conn, orders, holdings, approved)
    _update_strategy_cash(conn, strategy_cash, results)

    # A partial fill also needs attention: money moved but the portfolio is not
    # at target, so it must not pass silently.
    problems = [r for r in results if r["status"] in ("failed", "partial")]
    if problems:
        send_error_alert(
            "Rebalance did not complete cleanly: "
            + ", ".join(f"{r['symbol']} {r['side']} {r['status']}" for r in problems)
        )

    send_rebalance_result(results)
    logger.info("=== Rebalance end ===")


def _update_strategy_cash(
    conn: sqlite3.Connection,
    opening_cash: float,
    results: list[dict],
) -> float:
    """
    Move the strategy's cash ledger by what actually transacted.

    Only fills count. A skipped or failed order moved no money, so counting it
    would drift the ledger away from reality and mis-size every later rebalance.
    """
    spent = sum(r["notional"] for r in results
                if r["side"] == "buy" and r["status"] in ("filled", "partial"))
    received = sum(r["notional"] for r in results
                   if r["side"] == "sell" and r["status"] in ("filled", "partial"))

    closing = max(0.0, opening_cash - spent + received)
    set_strategy_cash(conn, closing)
    logger.info(
        "Strategy cash: $%.2f -> $%.2f (bought $%.2f, sold $%.2f)",
        opening_cash, closing, spent, received,
    )
    return closing


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

def _guarded(job: Callable[[], None], label: str) -> Callable[[], None]:
    """
    Wrap a scheduled job so nothing can fail silently.

    APScheduler catches job exceptions and writes them to its own logger, then
    carries on. For a system that moves money that is the worst outcome: the
    rebalance stops happening and nobody is told. This turns any unhandled
    exception into a Telegram alert.
    """
    def run() -> None:
        try:
            job()
        except Exception as exc:
            logger.exception("%s crashed", label)
            try:
                send_error_alert(f"{label} crashed: {exc}")
            except Exception:
                # Last line of defence: nothing here may reach the scheduler.
                logger.exception("%s: crash alert also failed", label)
    return run


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
        _guarded(lambda: _run_rebalance(conn), "Weekly rebalance"),
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
        _guarded(lambda: _send_weekly_heartbeat(conn), "Weekly heartbeat"),
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
