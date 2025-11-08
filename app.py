# ===============================
# app.py
# ===============================

from flask import Flask, request, jsonify
import requests
import hmac
import hashlib
import time
import threading
import os

from config import *
from trade_notifier import (
    log_trade_entry,
    log_trade_exit,
    trades,
    interval_to_seconds,
    trades_lock,
    send_telegram_message,
)

app = Flask(__name__)

# -------------------------
# Interval normalization map
# -------------------------
INTERVAL_MAP = {
    "1": "1m", "3": "3m", "5": "5m", "15": "15m", "30": "30m",
    "60": "1h", "120": "2h", "240": "4h", "1": "1m",
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1h", "2h": "2h", "4h": "4h", "1d": "1d", "D": "1d", "1D": "1d"
}


def normalize_interval(raw):
    if raw is None:
        return "1m"
    key = str(raw).strip()
    return INTERVAL_MAP.get(key, key).lower()


def trade_key(symbol: str, interval: str) -> str:
    """Canonical key used in shared trades dict (matches trade_notifier)."""
    return f"{symbol}_{interval.lower()}"


# ===============================
# 🔒 Binance Signed Request Helper
# ===============================
def binance_signed_request(http_method, path, params=None):
    if params is None:
        params = {}
    params["timestamp"] = int(time.time() * 1000)
    # maintain given order (config/environment must be deterministic)
    query = "&".join([f"{k}={v}" for k, v in params.items()])
    signature = hmac.new(BINANCE_SECRET_KEY.encode(), query.encode(), hashlib.sha256).hexdigest()
    query += f"&signature={signature}"
    url = f"{BASE_URL}{path}?{query}"
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    try:
        if http_method == "POST":
            return requests.post(url, headers=headers).json()
        elif http_method == "DELETE":
            return requests.delete(url, headers=headers).json()
        else:
            return requests.get(url, headers=headers).json()
    except Exception as e:
        print("❌ Binance request failed:", e)
        return {"error": str(e)}


# ===============================
# ⚙️ Leverage & Margin Setup
# ===============================
def set_leverage_and_margin(symbol):
    try:
        binance_signed_request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEVERAGE})
        binance_signed_request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": MARGIN_TYPE})
    except Exception as e:
        print("❌ Failed to set leverage/margin:", e)


# ===============================
# 📊 Quantity & Position Helpers
# ===============================
def get_symbol_info(symbol):
    info = requests.get(f"{BASE_URL}/fapi/v1/exchangeInfo").json()
    for s in info.get("symbols", []):
        if s["symbol"] == symbol:
            return s
    return None


def round_quantity(symbol, qty):
    info = get_symbol_info(symbol)
    if not info:
        return round(qty, 3)
    step_size = float([f["stepSize"] for f in info["filters"] if f["filterType"] == "LOT_SIZE"][0])
    min_qty = float([f["minQty"] for f in info["filters"] if f["filterType"] == "LOT_SIZE"][0])
    # Align to step_size (floor)
    try:
        multiples = int(qty / step_size)
        qty = float(multiples * step_size)
    except Exception:
        qty = float(step_size)
    if qty < min_qty:
        qty = min_qty
    return round(qty, 8)


def count_active_trades():
    try:
        positions = binance_signed_request("GET", "/fapi/v2/positionRisk")
        if isinstance(positions, dict) and positions.get("error"):
            return 0
        active_positions = [p for p in positions if abs(float(p.get("positionAmt", 0))) > 0]
        return len(active_positions)
    except Exception as e:
        print("❌ Failed to fetch active trades:", e)
        return 0


def calculate_quantity(symbol):
    try:
        price_data = requests.get(f"{BASE_URL}/fapi/v1/ticker/price", params={"symbol": symbol}).json()
        price = float(price_data["price"])
        position_value = TRADE_AMOUNT * LEVERAGE
        qty = position_value / price
        return round_quantity(symbol, qty)
    except Exception as e:
        print("❌ Failed to calculate quantity:", e)
        return 0.001


def get_position_info(symbol):
    # Signed endpoint
    timestamp = int(time.time() * 1000)
    query_string = f"timestamp={timestamp}&symbol={symbol}"
    signature = hmac.new(BINANCE_SECRET_KEY.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    url = f"{BASE_URL}/fapi/v2/positionRisk?{query_string}&signature={signature}"
    resp = requests.get(url, headers=headers)
    if resp.status_code == 200 and isinstance(resp.json(), list) and len(resp.json()) > 0:
        return resp.json()[0]
    return None


# ------------------------------
# Helper: compute implied PnL from given exit price (dollars)
# ------------------------------
def compute_implied_pnl_dollar(entry_price, exit_price, position_qty, side):
    # position_qty is absolute quantity (in base asset units)
    # For BUY side: profit = (exit - entry) * qty
    # For SELL side: profit = (entry - exit) * qty
    if position_qty == 0:
        return 0.0
    if side.upper() == "BUY":
        return (exit_price - entry_price) * position_qty
    else:
        return (entry_price - exit_price) * position_qty


# ===============================
# 🧠 Unified Exit Finalizer (Patched)
# ===============================
def finalize_trade(symbol, reason):
    """
    Fetch actual trade data from Binance (userTrades) and send unified Telegram exit
    using trade_notifier.log_trade_exit(...) signature:
        log_trade_exit(symbol, filled_price, pnl, pnl_percent, reason, interval, order_id)
    This function will:
      - fetch userTrades
      - derive pnl and pnl%
      - call log_trade_exit(...) which relies on local trades dict to include entry_price if present
      - cleanup any local trade keys for symbol
    """
    try:
        # determine interval from local state if any
        found_interval = "1m"
        with trades_lock:
            for k in list(trades.keys()):
                if k.startswith(f"{symbol}_"):
                    found_interval = k.split("_", 1)[1]
                    break

        interval = found_interval

        # Signed request to userTrades
        timestamp = int(time.time() * 1000)
        query_string = f"symbol={symbol}&timestamp={timestamp}"
        signature = hmac.new(BINANCE_SECRET_KEY.encode(), query_string.encode(), hashlib.sha256).hexdigest()
        headers = {"X-MBX-APIKEY": BINANCE_API_KEY}

        url = f"{BASE_URL}/fapi/v1/userTrades?{query_string}&signature={signature}"
        resp = requests.get(url, headers=headers)
        if resp.status_code != 200:
            print(f"⚠️ Binance trade fetch failed for {symbol}: {resp.text}")
            return

        trade_data = resp.json()
        if not trade_data:
            print(f"⚠️ No recent trade data for {symbol}")
            return

        last_trade = trade_data[-1]
        filled_price = float(last_trade.get("price", 0.0))
        realized_pnl = float(last_trade.get("realizedPnl", 0.0))
        qty = float(last_trade.get("qty", 0.0))

        # try to get local entry price to compute percent and to include in exit message
        with trades_lock:
            local_trade = trades.get(trade_key(symbol, interval), {}) or trades.get(symbol, {}) or {}
            entry_price = local_trade.get("entry_price", filled_price)
            order_id = local_trade.get("order_id", last_trade.get("orderId") or f"auto_{int(time.time())}")

        pnl = round(realized_pnl, 2)
        pnl_percent = 0.0
        try:
            if qty > 0 and entry_price > 0:
                pnl_percent = round((realized_pnl / (qty * entry_price)) * 100, 2)
        except Exception:
            pnl_percent = 0.0

        # call trade_notifier's log_trade_exit with the expected signature
        # log_trade_exit(symbol, filled_price, pnl, pnl_percent, reason=..., interval=..., order_id=...)
        log_trade_exit(symbol, filled_price, pnl, pnl_percent, reason=reason, interval=interval, order_id=order_id)

        print(f"[EXIT] {symbol} closed | {reason} | Exit: {filled_price} | PnL: {pnl} ({pnl_percent}%)")

        # cleanup local state entries that match symbol
        with trades_lock:
            keys_to_remove = [k for k in trades.keys() if k.startswith(f"{symbol}_")]
            for k in keys_to_remove:
                trades.pop(k, None)

    except Exception as e:
        print(f"❌ finalize_trade() error for {symbol}: {e}")


# ===============================
# 🧾 Exit Handlers
# ===============================
def get_exit_qty(symbol):
    pos_data = binance_signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    if not pos_data:
        return 0
    try:
        qty = abs(float(pos_data[0].get("positionAmt", 0)))
    except Exception:
        qty = 0
    return round_quantity(symbol, qty)


def execute_exit(symbol, side, interval="1m", bar_high=None, bar_low=None, reason="Manual Exit"):
    """
    High-level exit path:
    - Try limit exit using bar high/low if configured.
    - Fallback to market exit.
    - finalize_trade() will fetch real exit details and send telegram.
    NOTE: This function expects the trade entry to already be "filled" in local state.
    """
    try:
        key = trade_key(symbol, interval)
        with trades_lock:
            t = trades.get(key) or trades.get(symbol)
            # if no local filled entry exists, still allow closure (user may want forced market close)
            if not t:
                print(f"[WARN] execute_exit: no local trade state for {symbol} (interval={interval}); proceeding to exit anyway.")

        if EXIT_MARKET_DELAY_ENABLED:
            print(f"[{symbol}] Exit delay active → waiting {EXIT_MARKET_DELAY}s...")
            time.sleep(EXIT_MARKET_DELAY)

        # Attempt bar high/low limit exit if configured and provided
        if USE_BAR_HIGH_LOW_FOR_EXIT and bar_high and bar_low:
            limit_price = float(bar_high) if side.upper() == "BUY" else float(bar_low)
            print(f"[{symbol}] Attempting limit exit @ {limit_price} ({side})")

            limit_order = binance_signed_request("POST", "/fapi/v1/order", {
                "symbol": symbol,
                "side": "SELL" if side.upper() == "BUY" else "BUY",
                "type": "LIMIT",
                "timeInForce": "GTC",
                "quantity": get_exit_qty(symbol),
                "price": limit_price
            })

            order_id = limit_order.get("orderId")
            start_time = time.time()

            while time.time() - start_time < BAR_EXIT_TIMEOUT_SEC:
                order_status = binance_signed_request("GET", "/fapi/v1/order", {
                    "symbol": symbol,
                    "orderId": order_id
                })
                if order_status.get("status") == "FILLED":
                    print(f"[EXIT] {symbol} filled @ {limit_price}")
                    finalize_trade(symbol, reason=reason)
                    return True
                time.sleep(1)

            print(f"[{symbol}] Limit not filled in {BAR_EXIT_TIMEOUT_SEC}s → switching to MARKET exit")

        # Market fallback
        execute_market_exit(symbol, side, reason=reason)
        return True

    except Exception as e:
        print(f"❌ Exit error for {symbol}: {e}")
        return False


def execute_market_exit(symbol, side, reason="Market Exit"):
    pos_data = binance_signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    if not pos_data or abs(float(pos_data[0].get("positionAmt", 0))) == 0:
        print(f"⚠️ No active position for {symbol}")
        # Clear local state if mismatch
        with trades_lock:
            keys_to_remove = [k for k in trades.keys() if k.startswith(f"{symbol}_")]
            for k in keys_to_remove:
                trades.pop(k, None)
        return {"status": "no_position"}

    qty = abs(float(pos_data[0].get("positionAmt", 0)))
    qty = round_quantity(symbol, qty)
    close_side = "SELL" if side.upper() == "BUY" else "BUY"

    response = binance_signed_request("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": close_side,
        "type": "MARKET",
        "quantity": qty
    })

    if isinstance(response, dict) and "orderId" in response:
        threading.Thread(target=wait_and_finalize_exit, args=(symbol, response["orderId"], reason), daemon=True).start()

    return response


def wait_and_finalize_exit(symbol, order_id, reason):
    while True:
        order_status = binance_signed_request("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})
        if isinstance(order_status, dict) and order_status.get("status") == "FILLED":
            finalize_trade(symbol, reason)
            break
        time.sleep(1)


# ===============================
# ⏱️ 2-Bar Exit Logic (force market exit after 2 bars)
# ===============================
def two_bar_force_exit_worker(symbol, interval_str):
    """
    Worker that waits for 2 bars (2 * interval_seconds) after entry_filled,
    then force-closes at market (regardless of PnL) unless already closed or an exit signal arrived.
    """
    try:
        interval_seconds = interval_to_seconds(interval_str)
        key = trade_key(symbol, interval_str)

        with trades_lock:
            trade = trades.get(key) or trades.get(symbol)
            if not trade:
                return
            entry_time = trade.get("entry_time", time.time())

        # Sleep until 2 bars after entry_time
        target = entry_time + (2 * interval_seconds)
        now = time.time()
        remaining = target - now
        if remaining > 0:
            time.sleep(remaining)

        # Re-check: if exit_signal_received was set, skip 2-bar force close
        with trades_lock:
            trade = trades.get(key) or trades.get(symbol)
            if not trade:
                return
            if trade.get("exit_signal_received"):
                print(f"[2-BAR] {symbol} → exit signal already processed, skipping 2-bar force.")
                return

        # Before forcing exit, check if trade still exists and open
        pos = get_position_info(symbol)
        if not pos or abs(float(pos.get("positionAmt", 0))) == 0:
            # Nothing to close; cleanup local state
            with trades_lock:
                keys_to_remove = [k for k in trades.keys() if k.startswith(f"{symbol}_")]
                for k in keys_to_remove:
                    trades.pop(k, None)
            return

        # Force market exit after 2 bars regardless of PnL
        side = "BUY" if float(pos.get("positionAmt")) > 0 else "SELL"
        print(f"[2-BAR FORCE] {symbol} → forcing market exit after 2 bars.")
        # mark reason specifically
        execute_market_exit(symbol, side, reason="2-Bar Force Exit")

    except Exception as e:
        print(f"❌ two_bar_force_exit_worker error for {symbol}: {e}")


# ===============================
# 🚀 Open Position Logic
# ===============================
def open_position(symbol, side, limit_price, interval="1m"):
    """
    Places entry limit order.
    If Binance reports an active position for symbol:
      - Close that position first (market close) and wait for finalize_trade to fire
      - Then place the new entry
    Ensures exit message is delivered before sending new entry message (finalize_trade -> log_trade_exit does that).
    """
    # Check Binance for existing position
    pos_info = get_position_info(symbol)
    if pos_info and abs(float(pos_info.get("positionAmt", 0))) > 0:
        existing_side = "BUY" if float(pos_info.get("positionAmt")) > 0 else "SELL"
        # decide replace reason label
        replace_reason = "Replacing Entry: same direction signal" if existing_side == side.upper() else "Replacing Entry: opposite direction signal"
        print(f"⚠️ Existing Binance position detected for {symbol} (side={existing_side}). Will close it first. Reason: {replace_reason}")

        # Mark local trade (if exists) that we're replacing
        # Do NOT create new trade entry yet; ensure finalize_trade will use existing local state if present.
        execute_market_exit(symbol, existing_side, reason=replace_reason)

        # Wait for position to clear and for finalize_trade to run (poll local trades and position)
        wait_start = time.time()
        wait_timeout = 30  # seconds to wait for the previous position to clear; tweak if needed
        cleared = False
        while time.time() - wait_start < wait_timeout:
            time.sleep(1)
            cur = get_position_info(symbol)
            # check local trades cleared as well
            with trades_lock:
                any_local = any(k.startswith(f"{symbol}_") for k in trades.keys())
            if (not cur or abs(float(cur.get("positionAmt", 0))) == 0) and not any_local:
                cleared = True
                break

        if not cleared:
            print(f"⚠️ Position for {symbol} did not clear within {wait_timeout}s. Proceeding to attempt new entry anyway (risk of failure).")

    # Now we may place the new entry (either there was no pre-existing position, or it's closed)
    active_count = count_active_trades()
    if active_count >= MAX_ACTIVE_TRADES:
        print(f"🚫 Max active trades reached ({active_count}/{MAX_ACTIVE_TRADES})")
        return {"status": "max_trades_reached"}

    set_leverage_and_margin(symbol)
    qty = calculate_quantity(symbol)

    key = trade_key(symbol, interval)
    # Initialize local trade placeholder - entry_filled False until we get a fill
    with trades_lock:
        trades[key] = {
            "symbol": symbol,
            "side": side.upper(),
            "interval": interval,
            "entry_time": time.time(),   # will be updated to fill time when filled
            "entry_filled": False,
            "order_id": "PENDING",
            "closed": False,
            "two_bar_thread_started": False,
            "exit_signal_received": False,  # flag to avoid 2-bar forcing if exit came earlier
        }

    # Place the limit order (do NOT send pending entry telegram; entry telegram will be sent when filled)
    response = binance_signed_request("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": side,
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": qty,
        "price": limit_price
    })

    # If order placed, monitor fill
    if isinstance(response, dict) and "orderId" in response:
        order_id = response["orderId"]
        # update order id in local state
        with trades_lock:
            if key in trades:
                trades[key]["order_id"] = order_id
        threading.Thread(target=wait_and_notify_filled_entry, args=(symbol, side, order_id, interval), daemon=True).start()
    else:
        # If Binance rejected order or returned error, cleanup local placeholder
        with trades_lock:
            if key in trades:
                trades.pop(key, None)

    return response


def wait_and_notify_filled_entry(symbol, side, order_id, interval="1m"):
    """
    Monitors order until partially/fully filled. When fill starts, send a single entry Telegram
    with actual Binance fill price and start the 2-bar force-exit worker.
    """
    notified = False
    try:
        key = trade_key(symbol, interval)
        while True:
            order_status = binance_signed_request("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})
            status = order_status.get("status")
            executed_qty = float(order_status.get("executedQty", 0) or 0)
            # avgPrice may be string "0" for unfilled; fallback accordingly
            avg_price = float(order_status.get("avgPrice") or order_status.get("price") or 0)

            if not notified and status in ("PARTIALLY_FILLED", "FILLED") and executed_qty > 0:
                # Mark entry as filled and update entry price/time
                with trades_lock:
                    trade = trades.get(key, {})
                    trade["entry_filled"] = True
                    trade["entry_price"] = avg_price
                    trade["entry_time"] = time.time()
                    trade["order_id"] = order_id
                    trade["position_qty"] = executed_qty  # store base asset qty
                # Send single entry notification (actual Binance price)
                log_trade_entry(symbol, side, avg_price, order_id=order_id, interval=interval)
                print(f"[ENTRY FILLED] {symbol} {side} @ {avg_price} ({interval})")
                notified = True

                # Start 2-bar force exit thread once per trade
                with trades_lock:
                    if not trades.get(key, {}).get("two_bar_thread_started"):
                        trades[key]["two_bar_thread_started"] = True
                        threading.Thread(target=two_bar_force_exit_worker, args=(symbol, interval), daemon=True).start()

            if status in ("FILLED", "CANCELED", "REJECTED", "EXPIRED"):
                break
            time.sleep(1)
    except Exception as e:
        print(f"❌ wait_and_notify_filled_entry error for {symbol}: {e}")


# ===============================
# Evaluate exit-signal
# ===============================
def evaluate_exit_signal(symbol, alert_close_price, alert_side, bar_high=None, bar_low=None, interval_hint="1m"):
    """
    When an EXIT signal arrives it takes priority.
    Behavior:
      - If no open position -> ignore
      - Mark trade local flag 'exit_signal_received' so 2-bar worker skips force-exit.
      - Attempt limit exit using bar_high/bar_low if configured -> wait BAR_EXIT_TIMEOUT_SEC
      - If not filled in timeout, fall back to market close.
      - Return status dict
    Note: This version does NOT do any PnL comparison (removed per requirement).
    """
    try:
        pos = get_position_info(symbol)
        if not pos or abs(float(pos.get("positionAmt", 0))) == 0:
            print(f"⚠️ evaluate_exit_signal: no active position for {symbol}. Ignoring exit alert.")
            # cleanup local state
            with trades_lock:
                keys_to_remove = [k for k in trades.keys() if k.startswith(f"{symbol}_")]
                for k in keys_to_remove:
                    trades.pop(k, None)
            return {"status": "no_position"}

        # determine side
        side = "BUY" if float(pos.get("positionAmt", 0)) > 0 else "SELL"

        # choose interval from local state if available
        interval = interval_hint
        with trades_lock:
            for k in trades.keys():
                if k.startswith(f"{symbol}_"):
                    interval = k.split("_", 1)[1]
                    break
            # mark exit signal received so 2-bar worker will skip
            key = trade_key(symbol, interval)
            if key in trades:
                trades[key]["exit_signal_received"] = True
            else:
                # also mark any matching symbol key without interval
                for k in list(trades.keys()):
                    if k.startswith(f"{symbol}_"):
                        trades[k]["exit_signal_received"] = True

        # attempt exit (limit -> market)
        reason_label = "Exit Signal"
        if USE_BAR_HIGH_LOW_FOR_EXIT and bar_high and bar_low:
            res = execute_exit(symbol, side, interval=interval, bar_high=bar_high, bar_low=bar_low, reason=reason_label)
            return {"status": "exit_signal_attempted_limit", "result": res}
        else:
            execute_market_exit(symbol, side, reason=reason_label)
            return {"status": "exit_signal_market_called"}

    except Exception as e:
        print(f"❌ evaluate_exit_signal error for {symbol}: {e}")
        return {"error": str(e)}


# ===============================
# Webhook Endpoint
# ===============================
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_data(as_text=True)
    try:
        parts = [p.strip() for p in data.split("|")]
        if len(parts) >= 6:
            ticker, comment, close_price, bar_high, bar_low, interval = parts[:6]
        else:
            # fallback formats
            ticker, comment, close_price, interval = parts[0], parts[1], parts[2], parts[-1]
            bar_high = bar_low = None

        interval = normalize_interval(interval)
        symbol = ticker.replace("USDT", "") + "USDT"
        close_price = float(close_price)

        # initialize interval placeholder in trades state
        with trades_lock:
            k = trade_key(symbol, interval)
            trades.setdefault(k, {})
            trades[k]["interval"] = interval

        # ENTRY signals
        if comment == "BUY_ENTRY":
            return jsonify(open_position(symbol, "BUY", close_price, interval=interval))
        elif comment == "SELL_ENTRY":
            return jsonify(open_position(symbol, "SELL", close_price, interval=interval))

        # CROSS / OPPOSITE / SAME-SIDE signals => immediate close regardless of PnL
        elif comment in ["CROSS_EXIT_SHORT", "CROSS_EXIT_LONG", "OPPOSITE_EXIT", "SAME_SIDE_EXIT"]:
            pos = get_position_info(symbol)
            if pos and abs(float(pos.get("positionAmt", 0))) > 0:
                side = "BUY" if float(pos.get("positionAmt")) > 0 else "SELL"
                # Map reason label
                reason_label = "Cross Exit" if comment.startswith("CROSS") else ("Opposite Exit" if comment == "OPPOSITE_EXIT" else "Same Side Exit")
                # mark exit_signal_received for local trade if present
                with trades_lock:
                    for k in list(trades.keys()):
                        if k.startswith(f"{symbol}_"):
                            trades[k]["exit_signal_received"] = True
                # attempt limit->market if provided
                if USE_BAR_HIGH_LOW_FOR_EXIT and bar_high and bar_low:
                    execute_exit(symbol, side, interval=interval, bar_high=bar_high, bar_low=bar_low, reason=reason_label)
                else:
                    execute_market_exit(symbol, side, reason=reason_label)
                return jsonify({"status": "closed_by_opposite_same_cross"})
            else:
                # no position — ensure local state cleared
                with trades_lock:
                    keys_to_remove = [k for k in trades.keys() if k.startswith(f"{symbol}_")]
                    for k in keys_to_remove:
                        trades.pop(k, None)
                return jsonify({"status": "no_position"})

        # EXIT signals (informational; but now they always close immediately)
        elif comment in ["EXIT_LONG", "EXIT_SHORT", "SIGNAL_EXIT"]:
            result = evaluate_exit_signal(symbol, close_price, comment, bar_high, bar_low, interval_hint=interval)
            return jsonify(result)

        else:
            return jsonify({"error": f"Unknown comment: {comment}"})

    except Exception as e:
        print("❌ Webhook Error:", e)
        return jsonify({"error": str(e)})


# ===============================
# Ping & Self-ping
# ===============================
@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200


def self_ping():
    while True:
        try:
            requests.get(os.getenv("SELF_PING_URL", "https://binance-wcc-tradingview.onrender.com/ping"))
        except:
            pass
        time.sleep(5 * 60)


threading.Thread(target=self_ping, daemon=True).start()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
