"""Broker failures use mocks only: no SDK, credentials, or live orders."""
import json
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import discord_webull_bot as bot
import test_bot as helpers
from test_bot import MemoryState, MockBroker


class FailureTests(unittest.TestCase):
    def test_ambiguous_entry_stays_blocked_across_restart(self):
        with TemporaryDirectory() as directory, patch.object(bot, "ROOT", Path(directory)), patch.object(bot, "is_entry_window", return_value=True):
            state = bot.State(live=True)
            broker = MockBroker()
            broker.place = lambda order: (_ for _ in ()).throw(TimeoutError("synthetic acknowledgement loss"))
            engine = bot.TradingEngine(helpers.BotTests.cfg, True, state, broker)
            alert = bot.Alert("SPY", Decimal("750"), "C", Decimal(".55"))
            with self.assertRaises(TimeoutError):
                engine.accept_alert(alert)
            restarted = bot.State(live=True)
            self.assertEqual(restarted.data["active"]["phase"], "ENTRY_UNKNOWN")
            self.assertTrue(restarted.seen(alert.key(bot.today_et())))
            safe_broker = MockBroker()
            resumed = bot.TradingEngine(helpers.BotTests.cfg, True, restarted, safe_broker)
            resumed.accept_alert(alert)
            resumed.accept_alert(bot.Alert("SPY", Decimal("751"), "C", Decimal(".55")))
            self.assertEqual(safe_broker.orders, [])

    def test_ambiguous_take_profit_is_reconciled_without_resubmit(self):
        state = MemoryState()
        broker = MockBroker()
        engine = bot.TradingEngine(helpers.BotTests.cfg, True, state, broker)
        position = {"symbol": "SPY", "strike": "750", "right": "C", "alert_price": ".55", "expiry": bot.today_et(), "filled_qty": 1, "fill_price": ".55"}
        broker.place = lambda order: (_ for _ in ()).throw(TimeoutError("synthetic acknowledgement loss"))
        with self.assertRaises(TimeoutError):
            engine.submit_take_profit(position)
        self.assertEqual(state.data["active"]["phase"], "TP_UNKNOWN")
        order_id = position["tp_client_order_id"]
        broker.order_detail = lambda oid: (_ for _ in ()).throw(ConnectionError("synthetic broker outage"))
        with self.assertRaises(ConnectionError):
            engine.manage_active()
        self.assertEqual(state.data["active"]["tp_client_order_id"], order_id)
        self.assertEqual(state.data["active"]["phase"], "TP_UNKNOWN")

    def test_corrupt_state_refuses_startup(self):
        with TemporaryDirectory() as directory, patch.object(bot, "ROOT", Path(directory)):
            (Path(directory) / "live_state.json").write_text("broken json")
            with self.assertRaises(json.JSONDecodeError):
                bot.State(live=True)
