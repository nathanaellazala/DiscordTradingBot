#!/usr/bin/env python3
"""Desktop Discord-screen OCR -> Webull US options 0DTE trading.

No Discord user token, automation bot, login cookie, or Discord API access.
Live order execution uses official Webull OpenAPI SDK, NEVER unofficial endpoints.
"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Queue, Empty
import threading
from collections import deque
import fcntl
import json
import logging
import math
import os
import re
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, time as clocktime
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
ET = ZoneInfo("America/New_York")
LOG = logging.getLogger("discord_webull")

# Accept Discord text such as 'SPY 757P @ .34' / '$SPY 757 P @ $0.34'.
# Deliberately require a decimal price and the @ symbol to reduce OCR false positives.
ALERT_RE = re.compile(
    r"(?<![A-Z0-9])\$?(?P<symbol>[A-Z]{1,6})\s+"
    r"(?P<strike>\d{1,5}(?:\.\d{1,2})?)\s*"
    r"(?P<right>[CP])\s*@\s*\$?(?P<price>(?:\d+)?\.\d{1,3})(?!\d)",
    re.IGNORECASE,
)

DEFAULT_CONFIG = {
    "allowed_tickers": ["SPY"],
    "quantity": 1,
    "take_profit_percent": 20,
    "entry_type": "LIMIT",            # hard requirement: entry orders are ALWAYS limit orders
    "max_entry_slippage_percent": 10,  # upper bound above Discord alert price; never exceed 10%
    "entry_timeout_seconds": 300,    # rest buy limit up to 5 minutes, then cancel
    "entry_policy_version": 3,        # automatically migrate old 15%/12s settings
    "entry_cutoff_et": "15:45",        # no new 0DTE entries after this time
    "ocr_interval_seconds": 0.8,
    "baseline_seconds": 6.0,  # collect pre-existing on-screen alerts repeatedly
    "confirmation_scans": 2,  # require the same new alert on 2 sequential OCR scans
    "visual_gate_change_percent": 0.12,  # min changed screenshot pixels to arm a new OCR alert
    "order_poll_seconds": 3.0,
    "screen_region": None,
    "webull_environment": "production",  # live mode only; sandbox for test account
    "allowed_channel_ids": [],  # REQUIRED in live mode: Discord alert channel IDs
    "max_message_age_seconds": 8, # message Snowflake creation-time cutoff
    "bridge_port": 8765
}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG.copy())
        LOG.warning("Created config.json. Run --select-region before monitoring Discord.")
    cfg = DEFAULT_CONFIG.copy()
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        saved = json.load(handle)
    cfg.update(saved)
    # v2 allowed 15% slippage / MARKET buys and cancelled unfilled orders after
    # just 12 seconds. Upgrade safely while preserving the user's selected
    # Discord screen region and all unrelated configuration/credentials.
    if int(saved.get("entry_policy_version", 0)) < 3:
        cfg["entry_type"] = "LIMIT"
        cfg["max_entry_slippage_percent"] = 10
        if float(cfg.get("entry_timeout_seconds", 12)) == 12:
            cfg["entry_timeout_seconds"] = 300
        cfg["entry_policy_version"] = 3
        save_config(cfg)
        LOG.warning("Updated entry policy to 10%% maximum slippage, LIMIT-only, %ss entry timeout; preserved screen region", cfg["entry_timeout_seconds"])
    if cfg["entry_type"] != "LIMIT":
        raise ValueError("Only LIMIT entries are permitted: MARKET could exceed the 10% price ceiling")
    if not isinstance(cfg["quantity"], int) or not (1 <= cfg["quantity"] <= 10):
        raise ValueError("quantity must be a whole number from 1 to 10")
    if not (0 < float(cfg["take_profit_percent"]) <= 1000):
        raise ValueError("take_profit_percent must be greater than 0")
    if not (0 <= float(cfg["max_entry_slippage_percent"]) <= 10):
        raise ValueError("max_entry_slippage_percent must be between 0 and 10")
    if not (1 <= float(cfg["entry_timeout_seconds"]) <= 3600):
        raise ValueError("entry_timeout_seconds must be between 1 and 3600")
    if not (3.0 <= float(cfg["baseline_seconds"]) <= 30.0):
        raise ValueError("baseline_seconds must be between 3 and 30")
    if not (2 <= int(cfg["confirmation_scans"]) <= 6):
        raise ValueError("confirmation_scans must be between 2 and 6")
    if not (0.02 <= float(cfg["visual_gate_change_percent"]) <= 25):
        raise ValueError("visual_gate_change_percent must be between 0.02 and 25")
    if not isinstance(cfg["allowed_channel_ids"], list) or any(not re.fullmatch(r"[0-9]{16,21}", str(cid)) for cid in cfg["allowed_channel_ids"]):
        raise ValueError("allowed_channel_ids must be a list of Discord channel IDs")
    if not (2 <= float(cfg["max_message_age_seconds"]) <= 10):
        raise ValueError("max_message_age_seconds must be between 2 and 10")
    if not (1024 <= int(cfg["bridge_port"]) <= 65535):
        raise ValueError("bridge_port must be 1024..65535")
    if float(cfg["order_poll_seconds"]) < 2:
        raise ValueError("order_poll_seconds must be at least 2 seconds (Webull rate limits)")
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


@dataclass(frozen=True)
class Alert:
    symbol: str
    strike: Decimal
    right: str
    alert_price: Decimal

    def key(self, today: str) -> str:
        return f"{today}|{self.symbol}|{self.strike.normalize()}|{self.right}|{self.alert_price.normalize()}"

    def description(self) -> str:
        return f"{self.symbol} {format(self.strike.normalize(), 'f')}{self.right} @ ${self.alert_price}"


def parse_alerts(text: str, allowed_tickers: list[str]) -> list[Alert]:
    allowed = {symbol.upper() for symbol in allowed_tickers}
    output = []
    for match in ALERT_RE.finditer(text):
        try:
            symbol = match.group("symbol").upper()
            strike = Decimal(match.group("strike"))
            price = Decimal(match.group("price"))
            right = match.group("right").upper()
            if symbol not in allowed or strike <= 0 or price < Decimal("0.01") or price > 200:
                continue
            alert = Alert(symbol, strike, right, price)
            if alert not in output:
                output.append(alert)
        except InvalidOperation:
            continue
    return output


def ceil_cent(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_CEILING)


def floor_cent(value: Decimal) -> Decimal:
    # Always round DOWN for a maximum allowed purchase price. E.g. a 34c
    # alert +10% = 37.4c -> 37c, never 38c (which would exceed 10%).
    return value.quantize(Decimal("0.01"), rounding=ROUND_FLOOR)


def calculate_tp(fill: Decimal, tp_percent: float) -> Decimal:
    return ceil_cent(fill * (Decimal("1") + Decimal(str(tp_percent)) / 100))


def calculate_entry_limit(alert: Alert, max_slippage_percent: float) -> Decimal:
    requested = Decimal(str(max_slippage_percent))
    if requested < 0:
        raise ValueError("Entry slippage cannot be negative")
    # Independent guard, even if some caller bypasses load_config().
    pct = min(requested, Decimal("10"))
    return floor_cent(alert.alert_price * (Decimal("1") + pct / Decimal("100")))


def today_et() -> str:
    return datetime.now(ET).date().isoformat()


def is_entry_window(cutoff: str) -> bool:
    now = datetime.now(ET)
    hh, mm = (int(p) for p in cutoff.split(":"))
    return now.weekday() < 5 and clocktime(9, 30) <= now.time() < clocktime(hh, mm)


def atomic_write(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".state_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class State:
    def __init__(self, live: bool):
        self.path = ROOT / ("live_state.json" if live else "dry_run_state.json")
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.data = {"day": today_et(), "seen": [], "active": None}
        if self.data.get("day") != today_et():
            # Never erase an unresolved live order on a new trading day.
            self.data["day"] = today_et()
            self.data["seen"] = []
        self.save()

    def save(self):
        atomic_write(self.path, self.data)

    def seen(self, key: str) -> bool:
        return key in self.data["seen"]

    def mark(self, key: str) -> None:
        if key not in self.data["seen"]:
            self.data["seen"].append(key)
            self.data["seen"] = self.data["seen"][-2000:]
            self.save()

    def set_active(self, position: Optional[dict]):
        self.data["active"] = position
        self.save()


def option_order(alert: Alert, side: str, qty: int, order_id: str, *,
                 order_type: str, limit_price: Optional[Decimal] = None, expiry: str) -> dict:
    """Official Webull US single-leg option order request fields."""
    is_buy = side == "BUY"
    order = {
        "client_order_id": order_id,
        "combo_type": "NORMAL",
        "symbol": alert.symbol,
        "instrument_type": "OPTION",
        "market": "US",
        "order_type": order_type,
        "quantity": str(qty),
        "option_strategy": "SINGLE",
        "side": side,
        "time_in_force": "DAY",
        "entrust_type": "QTY",
        "position_intent": "BUY_TO_OPEN" if is_buy else "SELL_TO_CLOSE",
        "legs": [{
            "side": side,
            "quantity": str(qty),
            "symbol": alert.symbol,
            "strike_price": str(alert.strike),
            "option_expire_date": expiry,
            "instrument_type": "OPTION",
            "option_type": "CALL" if alert.right == "C" else "PUT",
            "market": "US"
        }],
    }
    if order_type == "MARKET":
        order["support_trading_session"] = "CORE"
    if limit_price is not None:
        order["limit_price"] = f"{limit_price:.2f}"
    return order


class WebullBroker:
    """Official SDK interface. No real calls unless program launched with --live."""
    def __init__(self, cfg: dict):
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
        key, secret, account = (
            os.getenv("WEBULL_APP_KEY", ""),
            os.getenv("WEBULL_APP_SECRET", ""),
            os.getenv("WEBULL_ACCOUNT_ID", ""),
        )
        if not all((key, secret, account)):
            raise RuntimeError("Live mode requires WEBULL_APP_KEY, WEBULL_APP_SECRET and WEBULL_ACCOUNT_ID in .env")
        from webull.core.client import ApiClient
        from webull.trade.trade_client import TradeClient
        client = ApiClient(key, secret, "us")
        endpoint = ("api.sandbox.webull.com" if cfg["webull_environment"] == "sandbox"
                    else "api.webull.com")
        client.add_endpoint("us", endpoint)
        self.api = TradeClient(client).order_v3
        self.account_id = account
        LOG.warning("Webull connected to %s (account: ...%s)", endpoint, account[-4:])

    @staticmethod
    def checked(response, label: str) -> dict:
        status = getattr(response, "status_code", None)
        try:
            payload = response.json()
        except Exception:
            payload = {"unparsed": str(getattr(response, "text", ""))[:300]}
        if status != 200:
            raise RuntimeError(f"{label} status={status}: {str(payload)[:400]}")
        return payload or {}

    def place(self, order: dict) -> dict:
        # A failure/timeout can have an UNKNOWN outcome. Caller must reconcile
        # by querying the persisted client_order_id, never blindly resubmit.
        return self.checked(self.api.place_order(self.account_id, [order]), "place_order")

    def order_detail(self, client_order_id: str) -> dict:
        data = self.checked(self.api.get_order_detail(self.account_id, client_order_id), "get_order_detail")
        items = data.get("orders") or data.get("data", {}).get("orders") or []
        for item in items:
            if item.get("client_order_id") == client_order_id:
                return item
        if len(items) == 1:
            return items[0]
        raise RuntimeError(f"Could not match order {client_order_id} in order detail response")

    def cancel(self, client_order_id: str) -> dict:
        return self.checked(self.api.cancel_order(self.account_id, client_order_id), "cancel_order")


def current_order_status(order: dict):
    try:
        qty = int(Decimal(str(order.get("filled_quantity") or "0")))
        price = Decimal(str(order.get("filled_price") or "0"))
    except (ValueError, InvalidOperation) as exc:
        raise RuntimeError(f"Invalid Webull fill fields: {order}") from exc
    return str(order.get("status", "")).upper(), qty, price


class TradingEngine:
    def __init__(self, cfg: dict, live: bool, state: State, broker: Optional[WebullBroker] = None):
        self.cfg, self.live, self.state, self.broker = cfg, live, state, broker
        self.last_poll = 0.0

    @staticmethod
    def deserialize_alert(rec: dict) -> Alert:
        return Alert(rec["symbol"], Decimal(rec["strike"]), rec["right"], Decimal(rec["alert_price"]))

    def accept_alert(self, alert: Alert):
        day = today_et()
        key = alert.key(day)
        if self.state.seen(key):
            return
        # Write dedupe flag BEFORE any live network request. Duplicate orders are more
        # dangerous than skipped messages when there is an uncertain connection.
        self.state.mark(key)
        LOG.info("NEW ALERT: %s", alert.description())
        if not is_entry_window(self.cfg["entry_cutoff_et"]):
            LOG.warning("Skipping new alert outside 09:30-%s ET trading window", self.cfg["entry_cutoff_et"])
            return
        if not self.live:
            limit = calculate_entry_limit(alert, self.cfg["max_entry_slippage_percent"])
            estimate = calculate_tp(alert.alert_price, self.cfg["take_profit_percent"])
            LOG.info("DRY RUN ONLY: BUY %s contracts %s via DAY LIMIT at $%.2f; "
                     "if ask > $%.2f, rest buy for up to %ss (or until cutoff); "
                     "if filled at alert $%s, TP LIMIT $%s (actual fill sets TP)",
                     self.cfg["quantity"], alert.description(), limit, limit,
                     self.cfg["entry_timeout_seconds"], alert.alert_price, estimate)
            return
        if self.state.data["active"] is not None:
            LOG.warning("Skipping %s; another trade is active or unresolved", alert.description())
            return
        entry_id = uuid.uuid4().hex
        record = {
            "symbol": alert.symbol, "strike": str(alert.strike),
            "right": alert.right, "alert_price": str(alert.alert_price),
            "expiry": day, "quantity_requested": self.cfg["quantity"],
            "entry_client_order_id": entry_id, "tp_client_order_id": None,
            "phase": "ENTRY_UNKNOWN", "created_at": datetime.now(ET).isoformat(),
            "filled_qty": 0, "fill_price": None, "target_price": None,
            "last_error": None
        }
        self.state.set_active(record)
        # ALWAYS place a buy LIMIT at the 10%-capped ceiling. If the market ask
        # is at/below the cap it may fill at that price or better. Otherwise
        # it rests at the limit; no need to rely on a possibly delayed quote.
        price = calculate_entry_limit(alert, self.cfg["max_entry_slippage_percent"])
        order = option_order(alert, "BUY", self.cfg["quantity"], entry_id,
                             order_type="LIMIT", limit_price=price, expiry=day)
        LOG.warning("LIVE BUY LIMIT: %s qty=%s max price=$%s, rests up to %ss; never MARKET",
                    alert.description(), self.cfg["quantity"], price,
                    self.cfg["entry_timeout_seconds"])
        self.broker.place(order)
        record["phase"] = "ENTRY_OPEN"
        self.state.set_active(record)
        LOG.info("Entry acknowledged: %s. Await actual fill.", entry_id)

    def manage_active(self):
        position = self.state.data["active"]
        if not self.live or not position:
            return
        if time.monotonic() - self.last_poll < float(self.cfg["order_poll_seconds"]):
            return
        self.last_poll = time.monotonic()
        phase = position["phase"]
        if phase.startswith("ENTRY"):
            self.manage_entry(position)
        elif phase.startswith("TP"):
            self.manage_tp(position)
        else:
            raise RuntimeError(f"Position phase {phase} requires manual inspection")

    def manage_entry(self, pos: dict):
        oid = pos["entry_client_order_id"]
        detail = self.broker.order_detail(oid)
        status, qty, price = current_order_status(detail)
        LOG.info("BUY order=%s status=%s filled=%s avg=%s", oid[:8], status, qty, price)
        pos["filled_qty"] = qty
        if qty and price > 0:
            pos["fill_price"] = str(price)
        self.state.set_active(pos)
        if status == "FILLED":
            if qty != pos["quantity_requested"]:
                raise RuntimeError("Inconsistent FILLED quantity; inspect order manually")
            self.submit_take_profit(pos)
            return
        if status in ("CANCELLED", "CANCELED", "FAILED", "REJECTED", "EXPIRED"):
            if qty > 0 and price > 0:
                self.submit_take_profit(pos)
            elif qty == 0:
                LOG.warning("Buy %s never filled; nothing to sell", oid[:8])
                self.state.set_active(None)
            else:
                raise RuntimeError("Cancelled entry with missing execution price")
            return
        if status == "PARTIAL_FILLED":
            if pos["phase"] != "ENTRY_CANCELLING":
                pos["phase"] = "ENTRY_CANCELLING"
                self.state.set_active(pos)
                LOG.warning("Partial entry; cancelling remaining order before setting TP")
                self.broker.cancel(oid)
            return
        if status not in ("PENDING", "SUBMITTED", "PENDING_CANCEL", "CANCEL_REQUESTED"):
            raise RuntimeError(f"Unknown entry status: {status}")
        elapsed = (datetime.now(ET) - datetime.fromisoformat(pos["created_at"])).total_seconds()
        expired = elapsed >= float(self.cfg["entry_timeout_seconds"])
        cutoff_passed = not is_entry_window(self.cfg["entry_cutoff_et"])
        if (expired or cutoff_passed) and pos["phase"] != "ENTRY_CANCELLING":
            pos["phase"] = "ENTRY_CANCELLING"
            self.state.set_active(pos)
            LOG.warning("Unfilled entry cancel requested: %s (elapsed=%.0fs, cutoff=%s)",
                        oid[:8], elapsed, cutoff_passed)
            self.broker.cancel(oid)

    def submit_take_profit(self, pos: dict):
        qty = int(pos["filled_qty"])
        fill = Decimal(str(pos["fill_price"] or 0))
        if qty <= 0 or fill <= 0:
            raise RuntimeError("Missing confirmed filled quantity/price; cannot safely create take profit")
        target = calculate_tp(fill, self.cfg["take_profit_percent"])
        alert = self.deserialize_alert(pos)
        tp_id = uuid.uuid4().hex
        pos["tp_client_order_id"] = tp_id
        pos["target_price"] = str(target)
        pos["phase"] = "TP_UNKNOWN"  # persist FIRST; never resend ambiguous order
        self.state.set_active(pos)
        sell = option_order(alert, "SELL", qty, tp_id, order_type="LIMIT",
                            limit_price=target, expiry=pos["expiry"])
        LOG.warning("LIVE TP SELL: %s qty=%s at >= $%s (+%s%% from fill $%s); NO STOP LOSS",
                    alert.description(), qty, target, self.cfg["take_profit_percent"], fill)
        self.broker.place(sell)
        pos["phase"] = "TP_OPEN"
        self.state.set_active(pos)
        LOG.info("Resting take-profit LIMIT order acknowledged: %s", tp_id)

    def manage_tp(self, pos: dict):
        oid = pos["tp_client_order_id"]
        if not oid:
            raise RuntimeError("No take-profit client order ID; manual intervention required")
        detail = self.broker.order_detail(oid)
        status, filled_qty, price = current_order_status(detail)
        LOG.info("TP order=%s status=%s sold=%s/%s @ $%s; target=$%s",
                 oid[:8], status, filled_qty, pos["filled_qty"], price, pos["target_price"])
        if status == "FILLED":
            if filled_qty != pos["filled_qty"]:
                raise RuntimeError("TP filled qty mismatch; manual reconciliation required")
            LOG.warning("TRADE COMPLETE: %s %s%s, buy $%s, TP filled at $%s",
                        pos["symbol"], pos["strike"], pos["right"], pos["fill_price"], price)
            self.state.set_active(None)
        elif status in ("CANCELLED", "FAILED"):
            raise RuntimeError("Take-profit order cancelled/failed; position may remain open. Check Webull now.")
        elif status not in ("PENDING", "SUBMITTED", "PARTIAL_FILLED"):
            raise RuntimeError(f"Unknown take profit order status {status}")


def screenshot_frame(region: dict):
    """Capture selected region. Requires macOS Screen Recording permission."""
    import mss
    from PIL import Image
    bounds = {k: int(region[k]) for k in ("left", "top", "width", "height")}
    if bounds["width"] < 50 or bounds["height"] < 30:
        raise RuntimeError("Screen region is too small")
    with mss.MSS() as capture:
        frame = capture.grab(bounds)
    return Image.frombytes("RGB", (frame.width, frame.height), frame.rgb)


def ocr_frame(image) -> str:
    import pytesseract
    from PIL import ImageEnhance, ImageOps
    enhanced = ImageOps.grayscale(image)
    enhanced = ImageEnhance.Contrast(enhanced).enhance(1.7)
    enhanced = enhanced.resize((enhanced.width * 2, enhanced.height * 2))
    return pytesseract.image_to_string(enhanced, config="--psm 6")


def screenshot_text(region: dict) -> str:
    return ocr_frame(screenshot_frame(region))


def image_change_percent(before, after) -> float:
    """Conservative percent of clearly changed thumbnail pixels; 0 means identical."""
    from PIL import ImageChops, ImageOps
    if before.size != after.size:
        return 100.0
    size = (min(320, before.width), min(240, before.height))
    a = ImageOps.grayscale(before.resize(size))
    b = ImageOps.grayscale(after.resize(size))
    diff = ImageChops.difference(a, b)
    return 100.0 * sum(px > 30 for px in diff.tobytes()) / (size[0] * size[1])


class FreshAlertGate:
    """Suppress OCR flicker: require visual activity + repeated identical recognition.

    Not an authoritative timestamp or Discord-message identity. Scrolling or a
    changed viewport can still cause OLD alerts to appear fresh. Stay in dry run
    until actual new-message scenarios have been verified.
    """
    def __init__(self, cfg: dict):
        self.confirmations = int(cfg["confirmation_scans"])
        self.visual_threshold = float(cfg["visual_gate_change_percent"])
        self.previous_frame = None
        self.last_visual_change = 0.0
        self.pending: dict[str, tuple[int, float]] = {}
        self.blocked_without_visual_change: set[str] = set()

    def baseline(self, frame):
        self.previous_frame = frame
        self.pending.clear()
        self.blocked_without_visual_change.clear()
        self.last_visual_change = 0.0

    def observe(self, frame, alerts: list[Alert], day: str, now: float) -> list[Alert]:
        """Return only candidates which passed the additional visual/temporal gate."""
        if self.previous_frame is None:
            self.baseline(frame)
            return []
        changed = image_change_percent(self.previous_frame, frame)
        if changed >= self.visual_threshold:
            self.last_visual_change = now
        self.previous_frame = frame
        current = {a.key(day): a for a in alerts}
        for key in list(self.pending):
            if key not in current or now - self.pending[key][1] > 3.0:
                self.pending.pop(key, None)
        out = []
        for key, alert in current.items():
            if key in self.blocked_without_visual_change:
                continue
            if key not in self.pending:
                # A previously unrecognized OLD alert should not become eligible
                # just because some unrelated region changes several seconds later.
                if not self.last_visual_change or now - self.last_visual_change > 1.5:
                    # Never allow an old OCR flicker to become eligible after an
                    # unrelated visual change much later in the same session.
                    self.blocked_without_visual_change.add(key)
                    continue
                self.pending[key] = (1, now)
                continue
            count, first_seen = self.pending[key]
            if now - first_seen > 3.0:
                self.pending.pop(key, None)
                continue
            count += 1
            if count >= self.confirmations:
                out.append(alert)
                self.pending.pop(key, None)
            else:
                self.pending[key] = (count, first_seen)
        return out


def select_region(cfg: dict) -> None:
    """Select chat message area on PRIMARY display using drag rectangle."""
    import tkinter as tk
    import mss
    from PIL import Image, ImageTk
    with mss.MSS() as capture:
        mon = capture.monitors[1]
        frame = capture.grab(mon)
        img = Image.frombytes("RGB", (frame.width, frame.height), frame.rgb)
    root = tk.Tk()
    root.title("Select Discord messages area: drag a rectangle, Enter = save, Esc = cancel")
    root.attributes("-topmost", True)
    root.geometry(f"{root.winfo_screenwidth()}x{root.winfo_screenheight()}+0+0")
    root.update()
    screenw, screenh = root.winfo_width(), root.winfo_height()
    scaled = img.resize((screenw, screenh))
    picture = ImageTk.PhotoImage(scaled)
    canvas = tk.Canvas(root, width=screenw, height=screenh, cursor="cross", highlightthickness=0)
    canvas.pack()
    canvas.create_image(0, 0, image=picture, anchor="nw")
    canvas.create_text(20, 25, text="Drag around the VISIBLE Discord message text. ENTER saves; ESC cancels.",
                       fill="yellow", font=("Arial", 18, "bold"), anchor="w")
    points = {"start": None, "end": None, "rect": None}

    def down(event):
        points["start"] = (event.x, event.y)
        if points["rect"]:
            canvas.delete(points["rect"])

    def move(event):
        if points["start"] is None:
            return
        x, y = points["start"]
        if points["rect"]:
            canvas.delete(points["rect"])
        points["rect"] = canvas.create_rectangle(x, y, event.x, event.y, outline="yellow", width=3)
        points["end"] = (event.x, event.y)

    def save(_event=None):
        if points["start"] is None or points["end"] is None:
            print("No region selected.")
            root.destroy()
            return
        ax, ay = points["start"]
        bx, by = points["end"]
        left = min(ax, bx) * frame.width / screenw
        top = min(ay, by) * frame.height / screenh
        width = abs(ax - bx) * frame.width / screenw
        height = abs(ay - by) * frame.height / screenh
        cfg["screen_region"] = {
            "left": round(mon["left"] + left), "top": round(mon["top"] + top),
            "width": round(width), "height": round(height)
        }
        save_config(cfg)
        print("Saved Discord screenshot region:", cfg["screen_region"])
        root.destroy()

    root.bind("<Return>", save)
    root.bind("<Escape>", lambda _event: root.destroy())
    canvas.bind("<ButtonPress-1>", down)
    canvas.bind("<B1-Motion>", move)
    canvas.bind("<ButtonRelease-1>", move)
    root.mainloop()


def lock_process():
    """Prevent two bot instances from simultaneously creating duplicate orders (macOS/Linux)."""
    lock_file = (ROOT / "bot.lock").open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        raise RuntimeError("Another bot instance is already running. Stop it before starting a second one.")
    return lock_file  # Keep descriptor open for entire process lifetime.


def monitor(cfg: dict, live: bool):
    if live:
        raise RuntimeError("UNSAFE: OCR screenshot changes cannot verify message freshness. Use --web-monitor --live")
    lock = lock_process()
    region = cfg.get("screen_region")
    if not region:
        raise RuntimeError("No screen_region in config.json; run --select-region first")
    state = State(live)
    broker = WebullBroker(cfg) if live else None
    engine = TradingEngine(cfg, live, state, broker)
    gate = FreshAlertGate(cfg)
    LOG.warning("Monitoring Discord screenshot region: %s", region)
    LOG.warning("Stale-alert protection: %.1fs baseline, %d OCR confirmations, %.2f%% screenshot change threshold",
                cfg["baseline_seconds"], cfg["confirmation_scans"], cfg["visual_gate_change_percent"])
    # Baseline multiple images: OCR may fail to recognize a visible alert on a
    # single screenshot. Any alerts captured during this period are historical.
    baseline_ends = time.monotonic() + float(cfg["baseline_seconds"])
    last_frame = None
    sampled = 0
    while time.monotonic() < baseline_ends or sampled == 0:
        engine.manage_active()
        last_frame = screenshot_frame(region)
        for alert in parse_alerts(ocr_frame(last_frame), cfg["allowed_tickers"]):
            state.mark(alert.key(today_et()))
        sampled += 1
        time.sleep(min(0.6, float(cfg["ocr_interval_seconds"])))
    gate.baseline(last_frame)
    LOG.info("Startup baseline complete (%d scans). Existing on-screen alerts are ignored when recognized.", sampled)
    if live and state.data["active"]:
        LOG.warning("Recovering saved order state: %s", state.data["active"]["phase"])
    tick = 0
    while True:
        try:
            engine.manage_active()
            frame = screenshot_frame(region)
            candidates = parse_alerts(ocr_frame(frame), cfg["allowed_tickers"])
            validated = gate.observe(frame, candidates, today_et(), time.monotonic())
            for alert in validated:
                engine.accept_alert(alert)
            tick += 1
            if tick % 45 == 0:
                LOG.info("Running (%s) | OCR scanning | active=%s", "LIVE" if live else "DRY RUN",
                         bool(state.data["active"]))
            time.sleep(float(cfg["ocr_interval_seconds"]))
        except KeyboardInterrupt:
            LOG.warning("Stopped by user; existing LIVE resting TP orders are not cancelled.")
            break
        except Exception as exc:
            LOG.exception("HALTED: %s", exc)
            if live:
                LOG.critical("LIVE automation halted. Check ALL open Webull orders and positions manually immediately.")
                break
            time.sleep(5)


# ===== V4 browser-local, message-ID validated signal source =====
# The previous screenshot OCR detects visual changes, not message creation;
# revealing an older alert via scrolling was observed to create a false signal.
# For this reason OCR monitoring is now ALWAYS read-only, and live requires
# locally observed, recent Discord message IDs from the bundled Chrome extension.
DISCORD_EPOCH_MS = 1420070400000
# A new message may contain a trade alert after introductory prose, as in:
# "gonna enter light size ...: SPY 750C @ .55 @unknown-role".
# Permit a single structured option alert only when the rest is an explicit
# entry cue and optional role mention; reject recaps, examples, and exit chatter.
WEB_STANDALONE_RE = re.compile(
    r"^\s*(?:[0-9]{1,2}[.)]\s*)?\$?[A-Z]{1,6}\s+"
    r"\d{1,5}(?:\.\d{1,2})?\s*[CP]\s*@\s*\$?(?:\d+)?\.\d{1,3}"
    r"(?:\s*(?:<@&?[0-9]{16,21}>|@[a-z0-9_-]+))*\s*[.!]?\s*$",
    re.IGNORECASE,
)
WEB_ROLE_SUFFIX_RE = re.compile(
    r"^\s*(?:(?:<@&?[0-9]{16,21}>|@[a-z0-9_-]+)\s*)*[.!]?\s*$",
    re.IGNORECASE,
)
WEB_ENTRY_CUE_RE = re.compile(
    r"\b(?:gonna|going\s+to|about\s+to|let['’]?s|i(?:['’]m|\s+am)?)"
    r"\s+(?:enter|buy|open|take|grab|start)\b|"
    r"\b(?:entering|buying|opening|taking|adding)\b|"
    r"\b(?:buy|enter|open|add|take)\s+(?:now|here|some|small|light|a|at)\b",
    re.IGNORECASE,
)
WEB_BLOCKED_CONTEXT_RE = re.compile(
    r"\b(?:sold|selling|sell|trim|trimmed|closing|closed|exit|exited|"
    r"recap|results|review|earlier|yesterday|last\s+(?:week|trade)|"
    r"example|hypothetical|backtest|watching|considering|maybe|might|could|would|"
    r"cancel|canceled|cancelled|avoid|skip|never|not|no\s+entry)\b|"
    r"don['’]?t|do\s+not",
    re.IGNORECASE,
)


def extract_browser_entry(text: str, allowed_tickers: list[str]) -> tuple[Optional[Alert], str]:
    """Only accept a standalone trade or one unambiguous *new entry* instruction."""
    if not isinstance(text, str) or not (1 <= len(text) <= 400):
        return None, "Message empty or too long"
    body = text.strip()
    if "\n" in body or "\r" in body:
        return None, "Multi-line message requires manual review"
    matches = list(ALERT_RE.finditer(body))
    if len(matches) != 1:
        return None, "Message must contain exactly one option alert pattern"
    match = matches[0]
    alerts = parse_alerts(match.group(0), allowed_tickers)
    if len(alerts) != 1:
        return None, "Not exactly one permitted ticker alert"
    if WEB_STANDALONE_RE.fullmatch(body):
        return alerts[0], "Standalone trade alert"
    prefix, suffix = body[:match.start()], body[match.end():]
    if len(prefix) > 220 or not WEB_ROLE_SUFFIX_RE.fullmatch(suffix):
        return None, "Unrecognized surrounding text; manual review required"
    if WEB_BLOCKED_CONTEXT_RE.search(prefix):
        return None, "Historical/uncertain/exit wording; not a fresh entry instruction"
    if not WEB_ENTRY_CUE_RE.search(prefix):
        return None, "No affirmative entry cue in message"
    return alerts[0], "Entry instruction with embedded alert"


def discord_timestamp_ms(snowflake: str) -> int:
    if not isinstance(snowflake, str) or not re.fullmatch(r"[0-9]{16,21}", snowflake):
        raise ValueError("Missing or invalid Discord snowflake ID")
    return (int(snowflake) >> 22) + DISCORD_EPOCH_MS


def validate_browser_signal(payload: dict, cfg: dict, session_start_ms: int,
                            now_ms: int, seen_ids: set[str], live: bool) -> tuple[bool, str, Optional[Alert]]:
    """Never infer freshness from page movement, OCR, or arrival time alone."""
    if not isinstance(payload, dict) or payload.get("type") != "alert":
        return False, "Not an alert", None
    msgid = payload.get("message_id")
    cid = payload.get("channel_id")
    if not isinstance(cid, str) or not re.fullmatch(r"[0-9]{16,21}", cid):
        return False, "Missing valid Discord channel ID", None
    if cid not in set(map(str, cfg.get("allowed_channel_ids", []))):
        if live or cfg.get("allowed_channel_ids"):
            return False, f"Channel {cid} not allowlisted", None
        # Dry-run may accept any channel while showing its ID for configuration.
    try:
        sent_ms = discord_timestamp_ms(msgid)
    except ValueError:
        return False, "No verifiable Discord message ID", None
    if msgid in seen_ids:
        return False, "Duplicate message ID", None
    # Messages that existed before monitoring started are NEVER actionable;
    # also deny delayed render, history scroll, cached/loaded messages, or clock drift.
    if sent_ms < session_start_ms:
        return False, "Message predates monitor session", None
    age_ms = now_ms - sent_ms
    if age_ms < -2000:
        return False, "Message timestamp is in the future", None
    if age_ms > int(float(cfg["max_message_age_seconds"]) * 1000):
        return False, f"Message too old ({age_ms / 1000:.1f}s)", None
    alert, reason = extract_browser_entry(payload.get("text"), cfg["allowed_tickers"])
    if alert is None:
        return False, reason, None
    return True, "Verified recent message: " + reason, alert


class LocalBridgeHandler(BaseHTTPRequestHandler):
    """Read-only browser transport on loopback; does not use Discord API."""
    queue: Queue

    def log_message(self, fmt, *args):
        return  # do not log raw Discord message content to HTTP logs

    def _reply(self, status: int, data: bytes = b"{}"):
        self.send_response(status)
        origin = self.headers.get("Origin", "")
        if origin.startswith("chrome-extension://"):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        # Diagnostics only: no account data or private messages.
        if self.path != "/health":
            self._reply(404)
            return
        self._reply(200, b'{"ok":true,"service":"discord-webull-local-bridge","version":6}')

    def do_OPTIONS(self):
        if not self.headers.get("Origin", "").startswith("chrome-extension://"):
            self._reply(403)
            return
        self._reply(204, b"")

    def do_POST(self):
        # An ordinary website cannot submit a request; only a Chrome extension
        # origin is accepted. This is not a defense against malware on the Mac.
        origin = self.headers.get("Origin", "")
        if self.path not in ("/alert", "/status") or not origin.startswith("chrome-extension://"):
            LOG.warning("Local bridge rejected POST %s (origin=%s)", self.path, origin[:70])
            self._reply(403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not (1 <= length <= 4096):
                self._reply(413)
                return
            event = json.loads(self.rfile.read(length))
            if not isinstance(event, dict) or len(str(event.get("text", ""))) > 400:
                self._reply(400)
                return
            if self.path == "/status":
                if event.get("type") != "status" or event.get("state") not in ("waiting-dom", "armed", "scanning") or \
                   not isinstance(event.get("channel_id"), str) or not re.fullmatch(r"[0-9]{16,21}", event["channel_id"]):
                    self._reply(400)
                    return
                for field in ("visible", "exactMatches", "newMessages"):
                    if type(event.get(field)) is not int or not (0 <= event[field] <= 50000):
                        self._reply(400)
                        return
                if "embeddedMatches" in event and (type(event["embeddedMatches"]) is not int
                                                   or not (0 <= event["embeddedMatches"] <= 50000)):
                    self._reply(400)
                    return
            else:
                if event.get("type") != "alert":
                    self._reply(400)
                    return
            try:
                self.queue.put_nowait(event)
            except Exception:
                self._reply(503)
                return
            self._reply(202)
        except (ValueError, UnicodeError):
            self._reply(400)


def monitor_web(cfg: dict, live: bool):
    lock = lock_process()
    if live and not cfg["allowed_channel_ids"]:
        raise RuntimeError("Live mode requires an exact allowed_channel_ids value in config.json")
    events: Queue = Queue(maxsize=200)
    class Handler(LocalBridgeHandler):
        queue = events
    server = ThreadingHTTPServer(("127.0.0.1", int(cfg["bridge_port"])), Handler)
    server.daemon_threads = True
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    # Fail closed on stale messages even if the extension reconnects.
    session_start_ms = int(time.time() * 1000)
    state = State(live)
    broker = WebullBroker(cfg) if live else None
    engine = TradingEngine(cfg, live, state, broker)
    seen_ids: set[str] = set()
    LOG.warning("BROWSER MESSAGE-ID MONITOR (%s) listening on 127.0.0.1:%s", "LIVE" if live else "DRY RUN", cfg["bridge_port"])
    LOG.warning("Chrome: visit discord.com/channels, enable unpacked chrome_extension, keep alerts channel open")
    LOG.warning("DIAGNOSTICS: open http://127.0.0.1:%s/health in Chrome to test that Python is listening", cfg["bridge_port"])
    LOG.warning("Within ~5s of opening the Discord channel, expect EXTENSION CONNECTED or EXTENSION WAITING. If none, reload Chrome extension and Discord tab.")
    LOG.warning("Trusted channel IDs: %s; messages must be younger than %ss and newer than monitor startup",
                cfg["allowed_channel_ids"] or "ANY (DRY RUN ONLY)", cfg["max_message_age_seconds"])
    last_status = time.monotonic()
    try:
        while True:
            engine.manage_active()
            try:
                event = events.get(timeout=0.5)
            except Empty:
                if time.monotonic() - last_status > 60:
                    LOG.info("Browser monitor active (%s) | current order=%s", "LIVE" if live else "DRY RUN", bool(state.data["active"]))
                    last_status = time.monotonic()
                continue
            if event.get("type") == "status":
                LOG.info("EXTENSION %s | channel=%s | visible_messages=%s | exact_alerts_on_screen=%s | embedded_candidates_on_screen=%s | new_messages_seen=%s", 
                         "WAITING FOR DOM" if event["state"] == "waiting-dom" else ("CONNECTED & ARMED" if event["state"] == "armed" else "CONNECTED"),
                         event["channel_id"], event["visible"], event["exactMatches"], event.get("embeddedMatches", 0), event["newMessages"])
                continue
            if event.get("type") == "heartbeat":
                continue
            ok, reason, alert = validate_browser_signal(event, cfg, session_start_ms,
                                                          int(time.time()*1000), seen_ids, live)
            if not ok:
                LOG.info("IGNORED browser candidate: %s (channel=%s)", reason, event.get("channel_id"))
                continue
            seen_ids.add(event["message_id"])
            LOG.info("VERIFIED FRESH DISCORD MESSAGE: id=%s channel=%s %s", event["message_id"], event["channel_id"], alert.description())
            engine.accept_alert(alert)
    except KeyboardInterrupt:
        LOG.warning("Stopped; existing resting live orders are not cancelled, check Webull")
    except Exception:
        LOG.exception("HALTED. If live: manually inspect Webull open orders/positions immediately")
        raise
    finally:
        server.shutdown()
        server.server_close()


def setup_logging():
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    LOG.setLevel(logging.INFO)
    if not LOG.handlers:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(formatter)
        LOG.addHandler(stream)
        fh = logging.FileHandler(ROOT / "bot.log", encoding="utf-8")
        fh.setFormatter(formatter)
        LOG.addHandler(fh)


def main():
    parser = argparse.ArgumentParser(description="Read-only Discord screen OCR or Chrome message-ID bridge -> Webull 0DTE")
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--select-region", action="store_true", help="Drag around the Discord chat messages")
    g.add_argument("--demo-alert", type=str, metavar="TEXT", help="Parse an alert offline, with no trading")
    g.add_argument("--show-state", action="store_true", help="Print persisted LIVE order state")
    g.add_argument("--check-screen", action="store_true", help="Print OCR result and alerts without trading")
    g.add_argument("--web-monitor", action="store_true", help="Chrome extension local message-ID verified monitor")
    parser.add_argument("--live", action="store_true", help="Enable REAL Webull API orders (otherwise dry run)")
    args = parser.parse_args()
    setup_logging()
    cfg = load_config()
    if args.select_region:
        select_region(cfg)
        return
    if args.demo_alert is not None:
        for a in parse_alerts(args.demo_alert, cfg["allowed_tickers"]):
            print(f"Parsed: {a.description()} | 0DTE today ET: {today_et()}")
            print(f"Estimated TP IF actual fill equals alert: ${calculate_tp(a.alert_price, cfg['take_profit_percent'])}")
            print(f"Entry LIMIT price cap: ${calculate_entry_limit(a, cfg['max_entry_slippage_percent'])}")
        if not parse_alerts(args.demo_alert, cfg["allowed_tickers"]):
            print("No valid alert matched.")
        return
    if args.show_state:
        p = ROOT / "live_state.json"
        print(p.read_text() if p.exists() else "No live_state.json yet")
        return
    if args.check_screen:
        if not cfg["screen_region"]:
            parser.error("Run --select-region first")
        txt = screenshot_text(cfg["screen_region"])
        print("========== OCR READ ==========")
        print(txt)
        print("========== PARSED ALERTS ==========")
        for a in parse_alerts(txt, cfg["allowed_tickers"]):
            print(a.description())
        return
    if args.live and not args.web_monitor:
        parser.error("SCREEN OCR LIVE TRADING DISABLED: use --web-monitor with bundled Chrome extension")
    if args.live:
        print("\nWARNING: LIVE option trading with NO STOP LOSS. An 0DTE option can lose 100%.")
        print("Entry orders may partially fill or fail. Take-profit LIMIT orders may not fill.")
        print("Check the expiry, contract, Webull permissions, account and market hours.")
        if input("Type exactly ENABLE LIVE ORDERS to continue: ").strip() != "ENABLE LIVE ORDERS":
            raise SystemExit("Live activation denied. No orders submitted.")
    if args.web_monitor:
        monitor_web(cfg, live=args.live)
    else:
        monitor(cfg, live=False)


if __name__ == "__main__":
    main()
