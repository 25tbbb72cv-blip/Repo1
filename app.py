import os
import re
import json
import logging
from typing import Dict, Any, Optional

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ------------- Config -------------

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")   # used in URL query ?secret=...
TP_WEBHOOK_URL = os.getenv("TP_WEBHOOK_URL", "")   # TradersPost webhook URL
TP_DEFAULT_QTY = int(os.getenv("TP_DEFAULT_QTY", "1"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Latest EMA state per ticker
# { "MNQZ2025": {"above13": True, "above200": True, "ema13": ..., "ema200": ..., "close": ..., "time": ...}, ... }
EMA_STATE: Dict[str, Dict[str, Any]] = {}

# Last trade per ticker (for simple dashboard / debugging)
LAST_TRADES: Dict[str, Dict[str, Any]] = {}


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

    # above13 / above200 come as "true"/"false" strings
    above13_raw = str(data.get("above13", "")).lower()
    above200_raw = str(data.get("above200", "")).lower()
    above13 = True if above13_raw == "true" else False
    above200 = True if above200_raw == "true" else False

    try:
        ema13 = float(data.get("ema13", 0.0))
    except Exception:
        ema13 = 0.0

    try:
        ema200 = float(data.get("ema200", 0.0))
    except Exception:
        ema200 = 0.0

    try:
        close = float(data.get("close", 0.0))
    except Exception:
        close = 0.0

    time_ = data.get("time", "")

    EMA_STATE[ticker] = {
        "above13": above13,
        "above200": above200,
        "ema13": ema13,
        "ema200": ema200,
        "close": close,
        "time": time_,
    }
    logger.info("Updated EMA state for %s: %s", ticker, EMA_STATE[ticker])


# Titan GT Ultra "New Trade Design" line:
#   MNQZ2025 New Trade Design , Price = 25787.50
TITAN_RE = re.compile(
    r"(?P<ticker>[A-Z0-9_]+)\s+New Trade Design\s*,\s*Price\s*=\s*(?P<price>[0-9.]+)"
)

# GT Ultra Exits v1.0 line:
#   MNQZ2025 Exit Signal,  Price = 25787.00
EXIT_RE = re.compile(
    r"(?P<ticker>[A-Z0-9_]+)\s+Exit Signal\s*,?\s*Price\s*=\s*(?P<price>[0-9.]+)"
)


def parse_titan_new_trade(text: str) -> Dict[str, Any]:
    """Parse Titan 'New Trade Design' text into {ticker, price}."""
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
    """Parse GT Ultra Exit text into {ticker, price}."""
    m = EXIT_RE.search(text)
    if not m:
        return {}
    out: Dict[str, Any] = {"ticker": m.group("ticker")}
    try:
        out["price"] = float(m.group("price"))
    except Exception:
        pass
    return out


def handle_new_trade_for_ticker(ticker: str, price: Optional[float], time_str: Optional[str]) -> dict:
    """
    Decide buy/sell using EMA_STATE[ticker] with 13 + 200 EMA alignment:

    - Long  only if above13 == True  AND above200 == True
    - Short only if above13 == False AND above200 == False
    - Otherwise -> skip trade
    """
    ema_info = EMA_STATE.get(ticker)
    if not ema_info:
        logger.warning("No EMA state for %s, skipping trade", ticker)
        return {"ok": True, "skipped": "no_ema_state"}

    above13 = ema_info.get("above13", None)
    above200 = ema_info.get("above200", None)

    if above13 is None or above200 is None:
        logger.warning("Missing EMA fields for %s, skipping trade", ticker)
        return {"ok": True, "skipped": "missing_ema_fields"}

    # Long only if above both EMAs
    if above13 and above200:
        direction = "buy"

    # Short only if below both EMAs
    elif (not above13) and (not above200):
        direction = "sell"

    # Mixed / not aligned -> skip
    else:
        logger.info(
            "Skipping %s new trade: EMA alignment failed (above13=%s, above200=%s)",
            ticker, above13, above200
        )
        return {"ok": True, "skipped": "ema_alignment_filter"}

    tp_payload: Dict[str, Any] = {
        "ticker": ticker,
        "action": direction,          # buy or sell
    }
    if TP_DEFAULT_QTY > 0:
        tp_payload["quantity"] = TP_DEFAULT_QTY
    if price is not None:
        tp_payload["price"] = price

    result = send_to_traderspost(tp_payload)

    LAST_TRADES[ticker] = {
        "last_event": "new_trade",
        "direction": direction,
        "price": price,
        "ema13_above": above13,
        "ema200_above": above200,
        "ema_snapshot": ema_info,
        "time": time_str,
        "tp_result": result,
    }

    return {"ok": result.get("ok", False), "detail": result}


def handle_exit_for_ticker(ticker: str, price: Optional[float], time_str: Optional[str]) -> dict:
    """Send an exit order for the given ticker, ignoring EMA state."""
    tp_payload: Dict[str, Any] = {
        "ticker": ticker,
        "action": "exit",   # if TradersPost prefers 'close', change this string
    }
    if price is not None:
        tp_payload["price"] = price

    result = send_to_traderspost(tp_payload)

    LAST_TRADES[ticker] = {
        "last_event": "exit",
        "price": price,
        "time": time_str,
        "tp_result": result,
    }

    return {"ok": result.get("ok", False), "detail": result}


# ------------- Routes -------------

@app.route("/webhook", methods=["POST"])
def webhook():
    # Secret check via querystring so it works for JSON + text
    if WEBHOOK_SECRET:
        if request.args.get("secret") != WEBHOOK_SECRET:
            logger.warning("Invalid secret")
            return jsonify({"ok": False, "error": "invalid secret"}), 403

    raw_body = request.get_data(as_text=True) or ""
    logger.info("Incoming body: %r", raw_body)

    # First, try JSON (EMA updates)
    data = None
    try:
        data = json.loads(raw_body)
    except Exception:
        data = None

    if isinstance(data, dict) and data:
        msg_type = data.get("type")
        if msg_type == "ema_update":
            update_ema_state_from_json(data)
            return jsonify({"ok": True})

        logger.warning("Unknown JSON type: %s", msg_type)
        return jsonify({"ok": False, "error": f"unknown json type {msg_type}"}), 400

    # Plain text – Titan entry or Exit indicator
    titan_info = parse_titan_new_trade(raw_body)
    if titan_info:
        ticker = titan_info.get("ticker")
        price = titan_info.get("price")
        result = handle_new_trade_for_ticker(ticker, price, time_str=None)
        status = 200 if result.get("ok", True) else 500
        return jsonify(result), status

    exit_info = parse_exit_signal(raw_body)
    if exit_info:
        ticker = exit_info.get("ticker")
        price = exit_info.get("price")
        result = handle_exit_for_ticker(ticker, price, time_str=None)
        status = 200 if result.get("ok", True) else 500
        return jsonify(result), status

    logger.warning("Unrecognized webhook payload")
    return jsonify({"ok": False, "error": "unrecognized payload"}), 400


@app.route("/", methods=["GET"])
def health():
    return jsonify({"ok": True, "message": "Titan Bot 1.0 webhook running"})


@app.route("/dashboard", methods=["GET"])
def dashboard():
    """View EMA state + last trades in a browser."""
    return jsonify({"ema_state": EMA_STATE, "last_trades": LAST_TRADES})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
