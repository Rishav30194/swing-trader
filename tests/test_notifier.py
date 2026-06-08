"""
test_notifier.py — Unit tests for src/notifier.py.

All Telegram API calls are mocked. No real network calls are made.

Coverage:
  - Formatting helpers produce correct structure and content.
  - Each send_* function calls bot.send_message with HTML parse mode.
  - send_error_alert never raises even when the bot is unreachable.
  - listen_for_reply returns True (YES), False (NO), or None (timeout).
  - listen_for_reply drains the queue before listening to avoid stale replies.
"""

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.notifier as notifier
from src.notifier import (
    _fmt_error,
    _fmt_execution,
    _fmt_exit,
    _fmt_signal,
    _fmt_weekly,
    listen_for_reply,
    send_error_alert,
    send_execution_alert,
    send_exit_alert,
    send_signal_alert,
    send_weekly_summary,
)
from src.database import WeeklySummary
from src.risk import Position
from telegram.constants import ParseMode


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def _position(**overrides) -> Position:
    defaults = dict(
        symbol="NVDA", entry_price=100.0, shares=10,
        stop_loss=95.0, trailing_stop=95.0, take_profit=110.0,
        entry_date=date(2025, 1, 15),
    )
    return Position(**{**defaults, **overrides})


def _signal_ctx(**overrides) -> dict:
    base = dict(
        close=134.21, ema_50=128.40, rsi=47.3, atr=4.41,
        rsi_lower_bound=40, rsi_upper_bound=55,
        cond_trend=True, cond_rsi=True, cond_macd=True,
    )
    return {**base, **overrides}


def _mock_bot() -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.get_updates  = AsyncMock(return_value=[])
    return bot


def _summary(**overrides) -> WeeklySummary:
    base = dict(
        period_start=date(2026, 6, 2),
        period_end=date(2026, 6, 8),
        trades_closed=0,
        wins=0,
        losses=0,
        net_pnl_dollars=0.0,
        open_symbols=[],
        signals=0,
    )
    return WeeklySummary(**{**base, **overrides})


def _mock_update(text: str, uid: int = 100) -> MagicMock:
    upd = MagicMock()
    upd.update_id    = uid
    upd.message.text = text
    return upd


# ---------------------------------------------------------------------------
# _fmt_signal
# ---------------------------------------------------------------------------

class TestFmtSignal:
    def test_contains_symbol(self):
        assert "NVDA" in _fmt_signal("NVDA", _signal_ctx())

    def test_contains_price_and_rsi(self):
        msg = _fmt_signal("NVDA", _signal_ctx(close=134.21, rsi=47.3))
        assert "134.21" in msg
        assert "47.3" in msg

    def test_contains_yes_no_prompt(self):
        msg = _fmt_signal("NVDA", _signal_ctx())
        assert "YES" in msg
        assert "NO" in msg

    def test_shows_sl_and_tp_when_present(self):
        msg = _fmt_signal("NVDA", _signal_ctx(stop_loss=128.0, take_profit=145.0))
        assert "128.00" in msg
        assert "145.00" in msg

    def test_shows_dash_when_sl_tp_absent(self):
        msg = _fmt_signal("NVDA", _signal_ctx())
        assert "—" in msg

    def test_uses_html_bold_for_symbol(self):
        msg = _fmt_signal("AAPL", _signal_ctx())
        assert "<b>" in msg and "AAPL" in msg

    def test_rsi_bounds_shown(self):
        msg = _fmt_signal("NVDA", _signal_ctx(rsi_lower_bound=40, rsi_upper_bound=55))
        assert "40" in msg
        assert "55" in msg


# ---------------------------------------------------------------------------
# _fmt_exit
# ---------------------------------------------------------------------------

class TestFmtExit:
    def test_profit_uses_green_icon(self):
        assert "✅" in _fmt_exit("NVDA", _position(entry_price=100.0), "tp", 110.0)

    def test_loss_uses_red_icon(self):
        assert "🔴" in _fmt_exit("NVDA", _position(entry_price=100.0), "sl", 94.0)

    def test_pnl_dollars_computed_correctly(self):
        # (110 - 100) * 10 shares = +$100 — format is $+100.00
        msg = _fmt_exit("NVDA", _position(entry_price=100.0, shares=10), "tp", 110.0)
        assert "$+100.00" in msg

    def test_exit_reason_hard_stop(self):
        assert "Hard stop-loss" in _fmt_exit("NVDA", _position(), "sl", 95.0)

    def test_exit_reason_trailing(self):
        assert "Trailing stop" in _fmt_exit("NVDA", _position(), "trailing", 97.0)

    def test_exit_reason_tp(self):
        assert "Take-profit" in _fmt_exit("NVDA", _position(), "tp", 110.0)

    def test_exit_reason_day5(self):
        assert "Day-5" in _fmt_exit("NVDA", _position(), "day5", 102.0)

    def test_unknown_reason_falls_back_to_raw(self):
        assert "Manual close" in _fmt_exit("NVDA", _position(), "manual", 100.0)

    def test_contains_entry_and_exit_price(self):
        msg = _fmt_exit("NVDA", _position(entry_price=100.0), "tp", 110.0)
        assert "100.00" in msg
        assert "110.00" in msg


# ---------------------------------------------------------------------------
# _fmt_error
# ---------------------------------------------------------------------------

class TestFmtError:
    def test_contains_error_text(self):
        msg = _fmt_error(ValueError("something broke"))
        assert "something broke" in msg

    def test_contains_error_header(self):
        assert "ERROR" in _fmt_error("timeout")

    def test_accepts_plain_string(self):
        assert "network timeout" in _fmt_error("network timeout")

    def test_escapes_html_special_chars(self):
        msg = _fmt_error("value < 0 & flag > limit")
        assert "<" not in msg.split("<pre>")[1].split("</pre>")[0]
        assert "&lt;" in msg or "value" in msg  # escaped


# ---------------------------------------------------------------------------
# _fmt_execution
# ---------------------------------------------------------------------------

class TestFmtExecution:
    def test_contains_symbol(self):
        order = {"filled_avg_price": 134.0, "filled_qty": 10}
        assert "NVDA" in _fmt_execution("NVDA", order, _position())

    def test_shows_fill_price(self):
        order = {"filled_avg_price": 134.50, "filled_qty": 10}
        assert "134.50" in _fmt_execution("NVDA", order, _position())

    def test_falls_back_to_position_when_order_empty(self):
        pos = _position(entry_price=100.0, shares=5)
        msg = _fmt_execution("NVDA", {}, pos)
        assert "100.00" in msg
        assert "5" in msg


# ---------------------------------------------------------------------------
# send_* functions — verify bot.send_message is called with HTML mode
# ---------------------------------------------------------------------------

class TestSendFunctions:
    def test_send_signal_alert_calls_send_message(self):
        bot = _mock_bot()
        with patch.object(notifier, "_bot", bot):
            send_signal_alert("NVDA", _signal_ctx())
        bot.send_message.assert_called_once()
        assert "NVDA" in bot.send_message.call_args.kwargs["text"]

    def test_send_execution_alert_calls_send_message(self):
        bot = _mock_bot()
        with patch.object(notifier, "_bot", bot):
            send_execution_alert("NVDA", {"filled_avg_price": 134.0, "filled_qty": 10}, _position())
        bot.send_message.assert_called_once()

    def test_send_exit_alert_calls_send_message(self):
        bot = _mock_bot()
        with patch.object(notifier, "_bot", bot):
            send_exit_alert("NVDA", _position(), "tp", 110.0)
        bot.send_message.assert_called_once()

    def test_send_error_alert_calls_send_message(self):
        bot = _mock_bot()
        with patch.object(notifier, "_bot", bot):
            send_error_alert("crash")
        bot.send_message.assert_called_once()

    def test_all_sends_use_html_parse_mode(self):
        bot = _mock_bot()
        with patch.object(notifier, "_bot", bot):
            send_signal_alert("NVDA", _signal_ctx())
        assert bot.send_message.call_args.kwargs.get("parse_mode") == ParseMode.HTML


# ---------------------------------------------------------------------------
# send_error_alert — must never raise
# ---------------------------------------------------------------------------

class TestSendErrorAlertNeverRaises:
    def test_does_not_raise_when_bot_unreachable(self):
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=Exception("Telegram down"))
        bot.get_updates  = AsyncMock(return_value=[])
        with patch.object(notifier, "_bot", bot):
            send_error_alert(RuntimeError("something crashed"))   # must not raise

    def test_does_not_raise_for_plain_string(self):
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=ConnectionError("no network"))
        bot.get_updates  = AsyncMock(return_value=[])
        with patch.object(notifier, "_bot", bot):
            send_error_alert("plain error string")                # must not raise


# ---------------------------------------------------------------------------
# listen_for_reply
# ---------------------------------------------------------------------------

class TestListenForReply:
    def _bot_with_replies(self, *texts: str) -> MagicMock:
        """Bot that returns empty on drain then one batch of updates."""
        updates = [_mock_update(t, uid=i + 1) for i, t in enumerate(texts)]
        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.get_updates  = AsyncMock(side_effect=[[], updates])
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
        bot.get_updates  = AsyncMock(side_effect=[
            [],
            [_mock_update("maybe", uid=1), _mock_update("NO", uid=2)],
        ])
        with patch.object(notifier, "_bot", bot):
            assert listen_for_reply(30) is False

    def test_returns_none_on_timeout(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.get_updates  = AsyncMock(return_value=[])
        with patch.object(notifier, "_bot", bot):
            assert listen_for_reply(0) is None

    def test_drains_old_messages_before_listening(self):
        """A YES present before the alert was sent must not approve the trade."""
        old_yes = _mock_update("YES", uid=50)
        new_no  = _mock_update("NO",  uid=51)
        bot = MagicMock()
        bot.send_message = AsyncMock()
        # drain returns old YES → offset advances to 51; poll returns new NO
        bot.get_updates  = AsyncMock(side_effect=[[old_yes], [new_no]])
        with patch.object(notifier, "_bot", bot):
            assert listen_for_reply(30) is False

    def test_updates_without_message_are_skipped(self):
        """Non-message updates (e.g. edited messages) must not crash the loop."""
        no_msg = MagicMock()
        no_msg.update_id = 1
        no_msg.message   = None
        real   = _mock_update("YES", uid=2)
        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.get_updates  = AsyncMock(side_effect=[[], [no_msg, real]])
        with patch.object(notifier, "_bot", bot):
            assert listen_for_reply(30) is True


# ---------------------------------------------------------------------------
# _fmt_weekly + send_weekly_summary
# ---------------------------------------------------------------------------

class TestWeeklySummary:
    def test_quiet_week_renders_running_and_zeros(self):
        text = _fmt_weekly(_summary(), equity=100_000.0)
        assert "✅ Running" in text
        assert "$100,000.00" in text
        assert "Open     : none" in text
        assert "Closed   : 0 trades" in text
        assert "Signals  : 0" in text

    def test_active_week_shows_win_loss_and_net(self):
        text = _fmt_weekly(
            _summary(trades_closed=3, wins=2, losses=1, net_pnl_dollars=240.0,
                     open_symbols=["NVDA", "AMD"], signals=4),
            equity=101_240.0,
        )
        assert "NVDA, AMD" in text
        assert "3 trades  (2W / 1L)  $+240.00" in text
        assert "Signals  : 4" in text

    def test_negative_net_pnl_keeps_sign(self):
        text = _fmt_weekly(
            _summary(trades_closed=1, wins=0, losses=1, net_pnl_dollars=-55.5),
            equity=99_944.5,
        )
        assert "$-55.50" in text

    def test_unavailable_equity_is_flagged(self):
        text = _fmt_weekly(_summary(), equity=None)
        assert "unavailable" in text

    def test_same_month_header_drops_repeated_month(self):
        text = _fmt_weekly(_summary(), equity=100_000.0)
        assert "Jun 02–08" in text

    def test_cross_month_header_keeps_both_months(self):
        text = _fmt_weekly(
            _summary(period_start=date(2026, 5, 28), period_end=date(2026, 6, 3)),
            equity=100_000.0,
        )
        assert "May 28–Jun 03" in text

    def test_send_weekly_summary_calls_send_message_html(self):
        bot = _mock_bot()
        with patch.object(notifier, "_bot", bot):
            send_weekly_summary(_summary(), 100_000.0)
        bot.send_message.assert_called_once()
        assert bot.send_message.call_args.kwargs.get("parse_mode") == ParseMode.HTML

    def test_send_weekly_summary_never_raises(self):
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=Exception("Telegram down"))
        bot.get_updates  = AsyncMock(return_value=[])
        with patch.object(notifier, "_bot", bot):
            send_weekly_summary(_summary(), None)   # must not raise
