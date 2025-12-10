
import os
import re
import json
import logging
from typing import Dict, Any, Optional

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ------------- Config -------------

TP_WEBHOOK_URL = os.getenv("TP_WEBHOOK_URL", "")   # TradersPost webhook URL
TP_DEFAULT_QTY = int(os.getenv("TP_DEFAULT_QTY", "1"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Latest EMA state per ticker
EMA_STATE: Dict[str, Dict[str, Any]] = {}

# Last trade per ticker
LAST_TRADES: Dict[str, Dict[str, Any]] = {}

# Simple position state per ticker
# { "MNQZ2025": {"open": True, "direction": "buy", "opened_time": "...", "price": 12345.0}, ... }
POSITION_STATE: Dict[str, Dict[str, Any]] = {}


# ------------- Helpers -------------

def send_to_traderspost(payload: dict) -> dict:
    """Send payload to TradersPost webhook."""
    if not TP_WEBHOOK_URL:
        logger.error("TP_WEBHOOK_URL not set")
        return {"ok": False, "error": "TP_WEBHOOK_URL not set"}

    try:
        logger.info("Sending to TradersPost: %s", payload)
        resp = requests.post(TP_WEBHOOK_URL, json=payload, timeout=5)
        return {
            "ok": resp.ok,
            "status_code": resp.status_code,
            "body": resp.text,
        }
    except Exception as e:
        logger.exception("Error sending to TradersPost: %s", e)
        return {"ok": False, "error": str(e)}


def update_ema_state_from_json(data: dict) -> None:
    """Handle ema_update JSON from EMA Broadcaster."""
    ticker = data.get("ticker")
    if not ticker:
        logger.warning("ema_update without ticker: %s", data)
        return

    above13_raw = str(data.get("above13", "")).lower()
    above13 = True if above13_raw == "true" else False

    try:
        ema13 = float(data.get("ema13", 0.0))
    except Exception:
        ema13 = 0.0

    try:
        close = float(data.get("close", 0.0))
    except Exception:
        close = 0.0

    time_ = data.get("time", "")

    EMA_STATE[ticker] = {
        "above13": above13,
        "ema13": ema13,
        "close": close,
        "time": time_,
    }

    logger.info("Updated EMA state for %s: %s", ticker, EMA_STATE[ticker])


# Titan GT Ultra "New Trade Design"
#   MNQZ2025 New Trade Design , Price = 25787.50
TITAN_RE = re.compile(
    r"(?P<ticker>[A-Z0-9_]+)\s+New Trade Design\s*,\s*Price\s*=\s*(?P<price>[0-9.]+)"
)

# Exit Signal
#   MNQZ2025 Exit Signal,  Price = 25787.00
EXIT_RE = re.compile(
    r"(?P<ticker>[A-Z0-9_]+)\s+Exit Signal\s*,?\s*Price\s*=\s*(?P<price>[0-9.]+)"
)


def parse_titan_new_trade(text: str) -> Dict[str, Any]:
    m = TITAN_RE.search(text)
    if not m:
        return {}
    out: Dict[str, Any] = {"ticker": m.group("ticker")}
    try:
        out["price"] = float(m.group("price"))
    except Exception:
        pass
    return out


def parse_exit_signal(text: str) -> Dict[str, Any]:
    m = EXIT_RE.search(text)
    if not m:
        return {}
    out: Dict[str, Any] = {"ticker": m.group("ticker")}
    try:
        out["price"] = float(m.group("price"))
    except Exception:
        pass
    return out


# ------------------ NEW TRADE HANDLER (EXIT + NEW ENTRY ON REDUNDANT) ------------------

def handle_new_trade_for_ticker(ticker: str, price: Optional[float], time_str: Optional[str]) -> dict:
    """
    Direction logic (no 200 EMA) + redundant trade behavior:

    - If NO open position:
        - Long  if above13 == True
        - Short if above13 == False

    - If position IS already open:
        - Send an 'exit' for the current position
        - Then send a NEW entry in the opposite direction
        - Update POSITION_STATE to the new direction
    """

    pos = POSITION_STATE.get(ticker, {})

    # --- CASE 1: position already open -> EXIT + NEW ENTRY (reverse) ---
    if pos.get("open"):
        current_dir = pos.get("direction")  # 'buy' or 'sell'
        logger.info("New trade on %s while position open (%s). Exit + new entry (reverse).", ticker, current_dir)

        # Exit current position
        exit_payload: Dict[str, Any] = {
            "ticker": ticker,
            "action": "exit",
        }
        if TP_DEFAULT_QTY > 0:
            exit_payload["quantity"] = TP_DEFAULT_QTY
        if price is not None:
            exit_payload["price"] = price

        exit_result = send_to_traderspost(exit_payload)

        # Opposite direction for new entry
        if current_dir == "buy":
            new_dir = "sell"
        elif current_dir == "sell":
            new_dir = "buy"
        else:
            new_dir = None

        entry_result = None
        if new_dir:
            entry_payload: Dict[str, Any] = {
                "ticker": ticker,
                "action": new_dir,
            }
            if TP_DEFAULT_QTY > 0:
                entry_payload["quantity"] = TP_DEFAULT_QTY
            if price is not None:
                entry_payload["price"] = price

            entry_result = send_to_traderspost(entry_payload)

            POSITION_STATE[ticker] = {
                "open": True,
                "direction": new_dir,
                "opened_time": time_str,
                "price": price,
            }
        else:
            logger.warning("Unknown current direction '%s' for %s; cannot determine reverse direction.", current_dir, ticker)
            POSITION_STATE[ticker] = {
                "open": False,
                "direction": None,
                "closed_time": time_str,
                "price": price,
            }

        LAST_TRADES[ticker] = {
            "last_event": "exit_and_new_entry",
            "from_direction": current_dir,
            "to_direction": new_dir,
            "price": price,
            "time": time_str,
            "tp_exit_result": exit_result,
            "tp_entry_result": entry_result,
        }

        ok = exit_result.get("ok", False) and (entry_result.get("ok", False) if entry_result else True)
        return {"ok": ok, "event": "exit_and_new_entry", "exit": exit_result, "entry": entry_result}

    # --- CASE 2: no open position -> normal 13 EMA entry ---
    ema_info = EMA_STATE.get(ticker)
    if not ema_info:
        logger.warning("No EMA state for %s, skipping trade", ticker)
        return {"ok": True, "skipped": "no_ema_state"}

    above13 = ema_info.get("above13", None)

    if above13 is None:
        logger.warning("Missing above13 for %s", ticker)
        return {"ok": True, "skipped": "missing_ema_13"}

    direction = "buy" if above13 else "sell"

    tp_payload: Dict[str, Any] = {
        "ticker": ticker,
        "action": direction,
    }

    if TP_DEFAULT_QTY > 0:
        tp_payload["quantity"] = TP_DEFAULT_QTY

    if price is not None:
        tp_payload["price"] = price

    result = send_to_traderspost(tp_payload)

    POSITION_STATE[ticker] = {
        "open": True,
        "direction": direction,
        "opened_time": time_str,
        "price": price,
    }

    LAST_TRADES[ticker] = {
        "last_event": "new_trade",
        "direction": direction,
        "price": price,
        "above13": above13,
        "ema_snapshot": ema_info,
        "time": time_str,
        "tp_result": result,
    }

    return {"ok": result.get("ok", False), "detail": result, "event": "new_trade"}


# ------------------ EXIT HANDLER ------------------

def handle_exit_for_ticker(ticker: str, price: Optional[float], time_str: Optional[str]) -> dict:
    tp_payload: Dict[str, Any] = {
        "ticker": ticker,
        "action": "exit",
    }
    if price is not None:
        tp_payload["price"] = price

    result = send_to_traderspost(tp_payload)

    POSITION_STATE[ticker] = {
        "open": False,
        "direction": None,
        "closed_time": time_str,
        "price": price,
    }

    LAST_TRADES[ticker] = {
        "last_event": "exit",
        "price": price,
        "time": time_str,
        "tp_result": result,
        "event": "exit",
    }

    return {"ok": result.get("ok", False), "detail": result, "event": "exit"}


# ------------- Routes -------------

@app.route("/webhook", methods=["POST"])
def webhook():

    raw_body = request.get_data(as_text=True) or ""
    logger.info("Incoming body: %r", raw_body)

    # Try JSON → EMA update
    data = None
    try:
        data = json.loads(raw_body)
    except Exception:
        data = None

    if isinstance(data, dict) and data:
        if data.get("type") == "ema_update":
            update_ema_state_from_json(data)
            return jsonify({"ok": True})

        return jsonify({"ok": False, "error": f"unknown json type {data.get('type')}"}), 400

    # Plain text TITAN entry
    titan_info = parse_titan_new_trade(raw_body)
    if titan_info:
        ticker = titan_info["ticker"]
        price = titan_info.get("price")
        result = handle_new_trade_for_ticker(ticker, price, time_str=None)
        return jsonify(result), (200 if result.get("ok") else 500)

    # Exit indicator
    exit_info = parse_exit_signal(raw_body)
    if exit_info:
        ticker = exit_info["ticker"]
        price = exit_info.get("price")
        result = handle_exit_for_ticker(ticker, price, time_str=None)
        return jsonify(result), (200 if result.get("ok") else 500)

    return jsonify({"ok": False, "error": "unrecognized payload"}), 400


@app.route("/", methods=["GET"])
def health():
    return jsonify({"ok": True, "message": "Titan Bot webhook running (13 EMA only, exit+new on redundant signal)"})


@app.route("/dashboard", methods=["GET"])
def dashboard():
    return jsonify({
        "ema_state": EMA_STATE,
        "last_trades": LAST_TRADES,
        "positions": POSITION_STATE,
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
