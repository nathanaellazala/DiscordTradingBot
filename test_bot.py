import unittest
from PIL import Image, ImageDraw
from decimal import Decimal
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import json
from zoneinfo import ZoneInfo
from unittest.mock import patch
from discord_webull_bot import (parse_alerts, calculate_tp, calculate_entry_limit, option_order,
                                Alert, TradingEngine, current_order_status, FreshAlertGate, image_change_percent)

class MemoryState:
    def __init__(self):
        self.data = {"seen": [], "active": None}
    def seen(self, x):
        return x in self.data["seen"]
    def mark(self, x):
        self.data["seen"].append(x)
    def set_active(self, p):
        self.data["active"] = p

class MockBroker:
    def __init__(self):
        self.orders = []
        self.details = {}
        self.cancelled = []
    def place(self, order):
        self.orders.append(order)
        return {"accepted": True}
    def order_detail(self, oid):
        return self.details[oid]
    def cancel(self, oid):
        self.cancelled.append(oid)
        return {}

class BotTests(unittest.TestCase):
    cfg = {"allowed_tickers": ["SPY"], "quantity": 1, "take_profit_percent": 20,
           "entry_type": "LIMIT", "max_entry_slippage_percent": 10,
           "entry_cutoff_et": "15:45", "entry_timeout_seconds": 300,
           "order_poll_seconds": 3}

    def test_parser(self):
        a = parse_alerts("hello SPY 757P @ .34 and SPY 770 C @ 0.55", ["SPY"])
        self.assertEqual(len(a), 2)
        self.assertEqual(a[0], Alert("SPY", Decimal("757"), "P", Decimal(".34")))
        self.assertEqual(a[1].right, "C")
        self.assertEqual(parse_alerts("SPY 757P .34", ["SPY"]), [])
        self.assertEqual(parse_alerts("QQQ 500P @ .34", ["SPY"]), [])

    def test_exact_tp_rounding(self):
        self.assertEqual(calculate_tp(Decimal(".34"), 20), Decimal(".41"))
        self.assertEqual(calculate_tp(Decimal(".40"), 20), Decimal(".48"))
        self.assertEqual(calculate_entry_limit(Alert("SPY", Decimal("757"), "P", Decimal(".34")), 10), Decimal(".37"))
        self.assertEqual(calculate_entry_limit(Alert("SPY", Decimal("750"), "C", Decimal(".60")), 10), Decimal(".66"))
        self.assertEqual(calculate_entry_limit(Alert("SPY", Decimal("757"), "P", Decimal(".39")), 10), Decimal(".42"))
        # Hard cap still applies if an old config mistakenly specifies 15%.
        self.assertEqual(calculate_entry_limit(Alert("SPY", Decimal("757"), "P", Decimal(".34")), 15), Decimal(".37"))

    def test_order_schema(self):
        a = Alert("SPY", Decimal("757"), "P", Decimal(".34"))
        o = option_order(a, "BUY", 1, "abc", order_type="LIMIT", limit_price=Decimal(".37"), expiry="2026-11-12")
        self.assertEqual(o["legs"][0]["option_type"], "PUT")
        self.assertEqual(o["legs"][0]["option_expire_date"], "2026-11-12")
        self.assertEqual(o["position_intent"], "BUY_TO_OPEN")
        self.assertEqual(o["limit_price"], "0.37")
        self.assertEqual(o["order_type"], "LIMIT")
        self.assertEqual(o["time_in_force"], "DAY")

    @patch("discord_webull_bot.is_entry_window", return_value=True)
    def test_lifecycle_buy_to_tp(self, _):
        state, broker = MemoryState(), MockBroker()
        engine = TradingEngine(self.cfg, True, state, broker)
        a = Alert("SPY", Decimal("757"), "P", Decimal(".34"))
        engine.accept_alert(a)
        self.assertEqual(len(broker.orders), 1)
        self.assertEqual(state.data["active"]["phase"], "ENTRY_OPEN")
        self.assertEqual(broker.orders[0]["order_type"], "LIMIT")
        self.assertEqual(broker.orders[0]["limit_price"], "0.37")
        buy_id = state.data["active"]["entry_client_order_id"]
        broker.details[buy_id] = {"status": "FILLED", "filled_quantity": "1", "filled_price": "0.36"}
        engine.manage_entry(state.data["active"])
        self.assertEqual(len(broker.orders), 2)
        tp_order = broker.orders[1]
        self.assertEqual(tp_order["limit_price"], "0.44")  # Actual 0.36 * 1.2 rounded up
        self.assertEqual(tp_order["position_intent"], "SELL_TO_CLOSE")
        self.assertEqual(state.data["active"]["phase"], "TP_OPEN")
        engine.accept_alert(a)
        self.assertEqual(len(broker.orders), 2)
        tp_id = state.data["active"]["tp_client_order_id"]
        broker.details[tp_id] = {"status": "FILLED", "filled_quantity": "1", "filled_price": "0.44"}
        engine.manage_tp(state.data["active"])
        self.assertIsNone(state.data["active"])

    @patch("discord_webull_bot.is_entry_window", return_value=True)
    def test_uncertain_entry_not_resubmitted(self, _):
        state, broker = MemoryState(), MockBroker()
        class UncertainBroker(MockBroker):
            def place(self, order):
                self.orders.append(order)
                raise TimeoutError("timeout after request sent")
        broker = UncertainBroker()
        engine = TradingEngine(self.cfg, True, state, broker)
        alert = Alert("SPY", Decimal("757"), "P", Decimal(".34"))
        with self.assertRaises(TimeoutError):
            engine.accept_alert(alert)
        self.assertEqual(state.data["active"]["phase"], "ENTRY_UNKNOWN")
        engine.accept_alert(alert)  # duplicate never sends a second order
        self.assertEqual(len(broker.orders), 1)

    @patch("discord_webull_bot.is_entry_window", return_value=True)
    def test_entry_resting_limit_not_cancelled_after_12_seconds(self, _):
        state, broker = MemoryState(), MockBroker()
        engine = TradingEngine(self.cfg, True, state, broker)
        alert = Alert("SPY", Decimal("750"), "C", Decimal(".60"))
        engine.accept_alert(alert)
        pos = state.data["active"]
        oid = pos["entry_client_order_id"]
        pos["created_at"] = (datetime.now(ZoneInfo("America/New_York")) - timedelta(seconds=30)).isoformat()
        state.set_active(pos)
        broker.details[oid] = {"status": "SUBMITTED", "filled_quantity": "0", "filled_price": "0"}
        engine.manage_entry(pos)
        self.assertEqual(broker.cancelled, [])
        self.assertEqual(pos["phase"], "ENTRY_OPEN")
        # Still has a limit order resting; it must never chase the price.
        self.assertEqual(broker.orders[0]["limit_price"], "0.66")
        self.assertEqual(len(broker.orders), 1)
        pos["created_at"] = (datetime.now(ZoneInfo("America/New_York")) - timedelta(seconds=301)).isoformat()
        engine.manage_entry(pos)
        self.assertEqual(broker.cancelled, [oid])
        self.assertEqual(pos["phase"], "ENTRY_CANCELLING")

    @patch("discord_webull_bot.is_entry_window", return_value=True)
    def test_resting_order_later_fill_creates_tp_based_on_actual_price(self, _):
        state, broker = MemoryState(), MockBroker()
        engine = TradingEngine(self.cfg, True, state, broker)
        alert = Alert("SPY", Decimal("750"), "C", Decimal(".60"))
        engine.accept_alert(alert)
        pos = state.data["active"]
        pos["created_at"] = (datetime.now(ZoneInfo("America/New_York")) - timedelta(seconds=48)).isoformat()
        broker.details[pos["entry_client_order_id"]] = {
            "status": "FILLED", "filled_quantity": "1", "filled_price": "0.65"}
        engine.manage_entry(pos)
        self.assertEqual(len(broker.orders), 2)
        self.assertEqual(broker.orders[1]["side"], "SELL")
        self.assertEqual(broker.orders[1]["limit_price"], "0.78")

    @patch("discord_webull_bot.is_entry_window", side_effect=[True, False])
    def test_entry_cutoff_cancels_pending_limit(self, _):
        state, broker = MemoryState(), MockBroker()
        engine = TradingEngine(self.cfg, True, state, broker)
        alert = Alert("SPY", Decimal("750"), "C", Decimal(".60"))
        engine.accept_alert(alert)
        pos = state.data["active"]
        oid = pos["entry_client_order_id"]
        broker.details[oid] = {"status": "SUBMITTED", "filled_quantity": "0", "filled_price": "0"}
        engine.manage_entry(pos)
        self.assertEqual(broker.cancelled, [oid])

    @patch("discord_webull_bot.is_entry_window", return_value=True)
    def test_even_legacy_market_config_only_places_limit_order(self, _):
        cfg = dict(self.cfg, entry_type="MARKET", max_entry_slippage_percent=15)
        state, broker = MemoryState(), MockBroker()
        TradingEngine(cfg, True, state, broker).accept_alert(
            Alert("SPY", Decimal("750"), "C", Decimal(".60")))
        self.assertEqual(len(broker.orders), 1)
        self.assertEqual(broker.orders[0]["order_type"], "LIMIT")
        self.assertEqual(broker.orders[0]["limit_price"], "0.66")

    def test_migration_preserves_existing_screen_region(self):
        from discord_webull_bot import load_config
        with TemporaryDirectory() as temp:
            location = Path(temp) / "config.json"
            location.write_text(json.dumps({"screen_region": {"left": 4, "top": 5, "width": 700, "height": 300},
                                            "entry_type": "MARKET", "max_entry_slippage_percent": 15,
                                            "entry_timeout_seconds": 12}), encoding="utf-8")
            with patch("discord_webull_bot.CONFIG_PATH", location):
                cfg = load_config()
                self.assertEqual(cfg["entry_type"], "LIMIT")
                self.assertEqual(cfg["max_entry_slippage_percent"], 10)
                self.assertEqual(cfg["entry_timeout_seconds"], 300)
                self.assertEqual(cfg["screen_region"], {"left": 4, "top": 5, "width": 700, "height": 300})
                self.assertEqual(json.loads(location.read_text())["entry_policy_version"], 3)

    def test_migration_rejects_future_market_override(self):
        from discord_webull_bot import load_config
        with TemporaryDirectory() as temp:
            location = Path(temp) / "config.json"
            location.write_text(json.dumps({"entry_policy_version": 3, "entry_type": "MARKET"}), encoding="utf-8")
            with patch("discord_webull_bot.CONFIG_PATH", location):
                with self.assertRaisesRegex(ValueError, "LIMIT entries"):
                    load_config()

    def test_fresh_gate_rejects_ocr_flicker_without_visual_change(self):
        cfg = {"confirmation_scans": 2, "visual_gate_change_percent": 0.12}
        gate = FreshAlertGate(cfg)
        before = Image.new("RGB", (300, 200), "black")
        gate.baseline(before)
        a = Alert("SPY", Decimal("750"), "P", Decimal(".42"))
        self.assertEqual(gate.observe(before, [a], "2026-11-16", 100.0), [])
        self.assertEqual(gate.observe(before, [a], "2026-11-16", 101.0), [])
        # A late unrelated screen change must not release a pre-existing pending alert.
        other = before.copy()
        ImageDraw.Draw(other).rectangle((0, 0, 150, 35), fill="white")
        self.assertEqual(gate.observe(other, [a], "2026-11-16", 106.0), [])
        self.assertEqual(gate.observe(other, [a], "2026-11-16", 107.0), [])

    def test_fresh_gate_needs_second_identical_ocr_scan(self):
        cfg = {"confirmation_scans": 2, "visual_gate_change_percent": 0.12}
        gate = FreshAlertGate(cfg)
        before = Image.new("RGB", (300, 200), "black")
        after = before.copy()
        ImageDraw.Draw(after).rectangle((40, 90, 250, 125), fill="white")
        self.assertGreater(image_change_percent(before, after), 0.12)
        gate.baseline(before)
        a = Alert("SPY", Decimal("757"), "P", Decimal(".34"))
        self.assertEqual(gate.observe(after, [a], "2026-11-16", 100.0), [])
        self.assertEqual(gate.observe(after, [a], "2026-11-16", 100.8), [a])

    def test_strike_display_normal_decimal(self):
        a = Alert("SPY", Decimal("750"), "P", Decimal(".42"))
        self.assertIn("750P", a.description())
        self.assertNotIn("E+", a.description())

    def test_status(self):
        self.assertEqual(current_order_status({"status": "FILLED", "filled_quantity": "1.000", "filled_price": "0.37"}),
                         ("FILLED", 1, Decimal("0.37")))

if __name__ == '__main__':
    unittest.main(verbosity=2)
