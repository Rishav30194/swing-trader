"""
test_notifier.py — Unit tests for src/notifier.py.

All Telegram API calls are mocked. No real network calls are made.

Coverage:
  - Formatting helpers produce correct structure and content.
  - Each send_* function calls bot.send_message with HTML parse mode.
  - Every send_* function returns False instead of raising when the bot is
    unreachable — hard rule 3 requires reductions to proceed regardless.
  - listen_for_reply returns True (YES), False (NO), or None (timeout).
  - listen_for_reply drains the queue before listening to avoid stale replies.
"""

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.notifier as notifier
from src.notifier import (
    _fmt_error,
    _fmt_rebalance_plan,
    _fmt_rebalance_result,
    _fmt_weekly,
    listen_for_reply,
    send_error_alert,
    send_rebalance_plan,
    send_rebalance_result,
    send_weekly_summary,
)
from src.database import WeeklySummary
from src.portfolio import RebalanceOrder, RegimeState
from telegram.constants import ParseMode


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def _state(on: bool = True, close: float = 204.12, sma: float = 203.50) -> RegimeState:
    return RegimeState(on=on, context={
        "close": close, "sma_200": sma,
        "lower_band": sma * 0.98, "upper_band": sma * 1.02,
        "was_held": on, "reason": "held" if on else "flat",
    })


def _order(**overrides) -> RebalanceOrder:
    defaults = dict(
        symbol="NVDA", side="buy", notional=125.0,
        reason="regime_entry", increases_exposure=True,
    )
    return RebalanceOrder(**{**defaults, **overrides})


def _mock_bot() -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.get_updates = AsyncMock(return_value=[])
    return bot


def _summary(**overrides) -> WeeklySummary:
    base = dict(
        period_start=date(2026, 6, 2),
        period_end=date(2026, 6, 8),
        sleeves_on=[],
        sleeves_off=[],
        orders_filled=0,
        orders_failed=0,
        notional_bought=0.0,
        notional_sold=0.0,
    )
    return WeeklySummary(**{**base, **overrides})


def _mock_update(text: str, uid: int = 100) -> MagicMock:
    upd = MagicMock()
    upd.update_id = uid
    upd.message.text = text
    return upd


# ---------------------------------------------------------------------------
# _fmt_rebalance_plan
# ---------------------------------------------------------------------------

class TestFmtRebalancePlan:
    def test_lists_every_sleeve(self):
        states = {"NVDA": _state(True), "ASML": _state(False, 1629.0, 1705.0)}
        text = _fmt_rebalance_plan([], states, 1000.0)
        assert "NVDA" in text and "ASML" in text

    def test_marks_on_and_off_sleeves_differently(self):
        states = {"NVDA": _state(True), "ASML": _state(False)}
        text = _fmt_rebalance_plan([], states, 1000.0)
        assert "●" in text and "○" in text

    def test_shows_close_and_sma(self):
        text = _fmt_rebalance_plan([], {"NVDA": _state(True, 204.12, 203.50)}, 1000.0)
        assert "204.12" in text and "203.50" in text

    def test_shows_equity(self):
        text = _fmt_rebalance_plan([], {"NVDA": _state()}, 1234.56)
        assert "1,234.56" in text

    def test_no_orders_says_no_changes(self):
        text = _fmt_rebalance_plan([], {"NVDA": _state()}, 1000.0)
        assert "no changes needed" in text

    def test_asks_for_yes_when_increases_present(self):
        text = _fmt_rebalance_plan([_order()], {"NVDA": _state()}, 1000.0)
        assert "YES" in text and "1 increase" in text

    def test_no_ask_when_only_reductions(self):
        sell = _order(side="sell", reason="regime_exit", increases_exposure=False)
        text = _fmt_rebalance_plan([sell], {"NVDA": _state()}, 1000.0)
        assert "Nothing to reply to" in text or "nothing to reply to" in text.lower()

    def test_reductions_flagged_as_automatic(self):
        sell = _order(side="sell", reason="regime_exit", increases_exposure=False)
        text = _fmt_rebalance_plan([sell], {"NVDA": _state()}, 1000.0)
        assert "automatically" in text

    def test_totals_the_increases(self):
        orders = [_order(notional=100.0), _order(symbol="MSFT", notional=50.0)]
        text = _fmt_rebalance_plan(orders, {"NVDA": _state()}, 1000.0)
        assert "150.00" in text

    def test_handles_sleeve_with_missing_context(self):
        states = {"NVDA": RegimeState(on=False, context={"skip_reason": "sma_200_nan"})}
        text = _fmt_rebalance_plan([], states, 1000.0)
        assert "no data" in text


# ---------------------------------------------------------------------------
# _fmt_rebalance_result
# ---------------------------------------------------------------------------

class TestFmtRebalanceResult:
    def _result(self, status: str) -> dict:
        return {"symbol": "NVDA", "side": "buy", "notional": 125.0, "status": status}

    def test_empty_results_says_none_required(self):
        assert "No orders were required" in _fmt_rebalance_result([])

    def test_success_uses_check_icon(self):
        assert "✅" in _fmt_rebalance_result([self._result("filled")])

    def test_failure_uses_alarm_icon(self):
        assert "🚨" in _fmt_rebalance_result([self._result("failed")])

    def test_failure_is_called_out(self):
        assert "FAILED" in _fmt_rebalance_result([self._result("failed")])

    def test_counts_each_status(self):
        text = _fmt_rebalance_result([
            self._result("filled"), self._result("failed"), self._result("skipped"),
        ])
        assert "filled 1" in text and "failed 1" in text and "skipped 1" in text

    def test_lists_symbol_and_notional(self):
        text = _fmt_rebalance_result([self._result("filled")])
        assert "NVDA" in text and "125.00" in text


# ---------------------------------------------------------------------------
# _fmt_error
# ---------------------------------------------------------------------------

class TestFmtError:
    def test_contains_error_text(self):
        assert "boom" in _fmt_error(ValueError("boom"))

    def test_contains_error_header(self):
        assert "ERROR" in _fmt_error("x")

    def test_accepts_plain_string(self):
        assert "plain failure" in _fmt_error("plain failure")

    def test_escapes_html_special_chars(self):
        text = _fmt_error("a < b & c > d")
        assert "&lt;" in text and "&amp;" in text


# ---------------------------------------------------------------------------
# send_* functions
# ---------------------------------------------------------------------------

class TestSendFunctions:
    def test_send_rebalance_plan_calls_send_message(self):
        bot = _mock_bot()
        with patch.object(notifier, "_bot", bot):
            assert send_rebalance_plan([_order()], {"NVDA": _state()}, 1000.0) is True
        bot.send_message.assert_awaited_once()

    def test_send_rebalance_result_calls_send_message(self):
        bot = _mock_bot()
        with patch.object(notifier, "_bot", bot):
            assert send_rebalance_result([]) is True
        bot.send_message.assert_awaited_once()

    def test_send_error_alert_calls_send_message(self):
        bot = _mock_bot()
        with patch.object(notifier, "_bot", bot):
            assert send_error_alert("bad") is True
        bot.send_message.assert_awaited_once()

    def test_all_sends_use_html_parse_mode(self):
        bot = _mock_bot()
        with patch.object(notifier, "_bot", bot):
            send_rebalance_plan([], {"NVDA": _state()}, 1000.0)
            send_rebalance_result([])
            send_error_alert("x")
            send_weekly_summary(_summary(), 1000.0)
        for call in bot.send_message.await_args_list:
            assert call.kwargs["parse_mode"] == ParseMode.HTML


# ---------------------------------------------------------------------------
# Failure tolerance — hard rule 3
# ---------------------------------------------------------------------------

class TestSendsNeverRaise:
    @pytest.mark.parametrize("send", [
        lambda: send_rebalance_plan([_order()], {"NVDA": _state()}, 1000.0),
        lambda: send_rebalance_result([{"symbol": "NVDA", "side": "buy",
                                        "notional": 1.0, "status": "filled"}]),
        lambda: send_error_alert("x"),
        lambda: send_weekly_summary(_summary(), None),
    ])
    def test_returns_false_when_bot_unreachable(self, send):
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=RuntimeError("network down"))
        with patch.object(notifier, "_bot", bot):
            assert send() is False


# ---------------------------------------------------------------------------
# listen_for_reply
# ---------------------------------------------------------------------------

class TestListenForReply:
    def _bot_with_replies(self, *texts: str) -> MagicMock:
        """Bot that returns empty on drain then one batch of updates."""
        updates = [_mock_update(t, uid=i + 1) for i, t in enumerate(texts)]
        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.get_updates = AsyncMock(side_effect=[[], updates])
        return bot

    def test_returns_true_for_yes(self):
        with patch.object(notifier, "_bot", self._bot_with_replies("YES")):
            assert listen_for_reply(30) is True

    def test_returns_true_for_y_lowercase(self):
        with patch.object(notifier, "_bot", self._bot_with_replies("y")):
            assert listen_for_reply(30) is True

    def test_returns_false_for_no(self):
        with patch.object(notifier, "_bot", self._bot_with_replies("NO")):
            assert listen_for_reply(30) is False

    def test_returns_false_for_n_lowercase(self):
        with patch.object(notifier, "_bot", self._bot_with_replies("n")):
            assert listen_for_reply(30) is False

    def test_ignores_unrecognised_messages(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.get_updates = AsyncMock(side_effect=[
            [],
            [_mock_update("maybe", uid=1), _mock_update("NO", uid=2)],
        ])
        with patch.object(notifier, "_bot", bot):
            assert listen_for_reply(30) is False

    def test_returns_none_on_timeout(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.get_updates = AsyncMock(return_value=[])
        with patch.object(notifier, "_bot", bot):
            assert listen_for_reply(0) is None

    def test_drains_old_messages_before_listening(self):
        """A YES present before the plan was sent must not approve this week."""
        old_yes = _mock_update("YES", uid=50)
        new_no = _mock_update("NO", uid=51)
        bot = MagicMock()
        bot.send_message = AsyncMock()
        # drain returns old YES → offset advances to 51; poll returns new NO
        bot.get_updates = AsyncMock(side_effect=[[old_yes], [new_no]])
        with patch.object(notifier, "_bot", bot):
            assert listen_for_reply(30) is False

    def test_updates_without_message_are_skipped(self):
        """Non-message updates (e.g. edited messages) must not crash the loop."""
        no_msg = MagicMock()
        no_msg.update_id = 1
        no_msg.message = None
        real = _mock_update("YES", uid=2)
        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.get_updates = AsyncMock(side_effect=[[], [no_msg, real]])
        with patch.object(notifier, "_bot", bot):
            assert listen_for_reply(30) is True


# ---------------------------------------------------------------------------
# _fmt_weekly + send_weekly_summary
# ---------------------------------------------------------------------------

class TestWeeklySummary:
    def test_quiet_week_renders_running(self):
        text = _fmt_weekly(_summary(), equity=100_000.0)
        assert "✅ Running" in text
        assert "none" in text

    def test_lists_invested_and_cash_sleeves(self):
        text = _fmt_weekly(
            _summary(sleeves_on=["NVDA", "MSFT"], sleeves_off=["ASML"]), 1000.0)
        assert "NVDA, MSFT" in text and "ASML" in text

    def test_active_week_shows_order_counts_and_notional(self):
        text = _fmt_weekly(
            _summary(orders_filled=3, orders_failed=1,
                     notional_bought=250.0, notional_sold=125.0), 1000.0)
        assert "3 filled / 1 failed" in text
        assert "375.00" in text

    def test_unavailable_equity_is_flagged(self):
        assert "unavailable" in _fmt_weekly(_summary(), equity=None)

    def test_same_month_header_drops_repeated_month(self):
        assert "Jun 02–08" in _fmt_weekly(_summary(), 1.0)

    def test_cross_month_header_keeps_both_months(self):
        s = _summary(period_start=date(2026, 5, 28), period_end=date(2026, 6, 3))
        assert "May 28–Jun 03" in _fmt_weekly(s, 1.0)

    def test_send_weekly_summary_calls_send_message_html(self):
        bot = _mock_bot()
        with patch.object(notifier, "_bot", bot):
            assert send_weekly_summary(_summary(), 1000.0) is True
        assert bot.send_message.await_args.kwargs["parse_mode"] == ParseMode.HTML
