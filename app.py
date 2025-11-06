# ===============================
# app.py (Final Integrated with Trade Notifier)
# ===============================

from flask import Flask, request, jsonify
import requests, hmac, hashlib, time, threading, os
from config import *
from trade_notifier import (
    log_trade_entry,
    log_trade_exit,
    trades,
    interval_to_seconds,
    trades_lock,
    send_telegram_message,  # ✅ Added import for unified Telegram sending
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


# ===============================
# 🔒 Binance Signed Request Helper
# ===============================
def binance_signed_request(http_method, path, params=None):
    if params is None:
        params = {}
    params["timestamp"] = int(time.time() * 1000)
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
    qty = (qty // step_size) * step_size
    if qty < min_qty:
        qty = min_qty
    return round(qty, 8)


def count_active_trades():
    try:
        positions = binance_signed_request("GET", "/fapi/v2/positionRisk")
        active_positions = [p for p in positions if abs(float(p["positionAmt"])) > 0]
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
    timestamp = int(time.time() * 1000)
    query_string = f"symbol={symbol}&timestamp={timestamp}"
    signature = hmac.new(BINANCE_SECRET_KEY.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    url = f"{BASE_URL}/fapi/v2/positionRisk?{query_string}&signature={signature}"
    resp = requests.get(url, headers=headers)
    if resp.status_code == 200 and len(resp.json()) > 0:
        return resp.json()[0]
    return None


# ===============================
# 🧠 Unified Exit Finalizer (Updated)
# ===============================
def finalize_trade(symbol, reason):
    """Fetch actual trade data from Binance and send unified Telegram exit."""
    try:
        headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
        resp = requests.get(f"{BASE_URL}/fapi/v1/userTrades?symbol={symbol}", headers=headers)
        if resp.status_code != 200:
            print(f"⚠️ Binance trade fetch failed for {symbol}: {resp.text}")
            return

        trade_data = resp.json()
        if not trade_data:
            print(f"⚠️ No recent trade data for {symbol}")
            return

        last_trade = trade_data[-1]
        filled_price = float(last_trade["price"])
        realized_pnl = float(last_trade.get("realizedPnl", 0.0))
        qty = float(last_trade["qty"])

        with trades_lock:
            trade = trades.get(symbol, {})
            entry_price = trade.get("entry_price", filled_price)
            interval = trade.get("interval", "1m")
            order_id = trade.get("order_id", f"auto_{int(time.time())}")

        pnl_percent = (realized_pnl / (qty * entry_price)) * 100 if entry_price > 0 else 0

        # ✅ Unified Telegram formatting
        log_trade_exit(
            symbol=symbol,
            order_id=order_id,
            filled_price=filled_price,
            reason=reason,
            interval=interval
        )

        print(f"[EXIT] {symbol} closed | {reason} | Exit: {filled_price} | PnL: {realized_pnl} ({pnl_percent:.2f}%)")

    except Exception as e:
        print(f"❌ finalize_trade() error for {symbol}: {e}")


# ===============================
# 🧾 Exit Handlers
# ===============================
def get_exit_qty(symbol):
    pos_data = binance_signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    if not pos_data:
        return 0
    qty = abs(float(pos_data[0].get("positionAmt", 0)))
    return round_quantity(symbol, qty)


def execute_exit(symbol, side, bar_high=None, bar_low=None, reason="Manual Exit"):
    try:
        if EXIT_MARKET_DELAY_ENABLED:
            print(f"[{symbol}] Exit delay active → waiting {EXIT_MARKET_DELAY}s...")
            time.sleep(EXIT_MARKET_DELAY)

        if USE_BAR_HIGH_LOW_FOR_EXIT and bar_high and bar_low:
            # ✅ Adjusted: BUY exits use HIGH (capture profit), SELL exits use LOW
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
                    finalize_trade(symbol, reason="Bar High/Low Exit")
                    return True
                time.sleep(1)

            print(f"[{symbol}] Limit not filled in {BAR_EXIT_TIMEOUT_SEC}s → switching to MARKET exit")

        execute_market_exit(symbol, side, reason)
        return True

    except Exception as e:
        print(f"❌ Exit error for {symbol}: {e}")
        return False


def execute_market_exit(symbol, side, reason="Market Exit"):
    pos_data = binance_signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    if not pos_data or abs(float(pos_data[0]["positionAmt"])) == 0:
        print(f"⚠️ No active position for {symbol}")
        return {"status": "no_position"}

    qty = abs(float(pos_data[0]["positionAmt"]))
    qty = round_quantity(symbol, qty)
    close_side = "SELL" if side.upper() == "BUY" else "BUY"

    response = binance_signed_request("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": close_side,
        "type": "MARKET",
        "quantity": qty
    })

    if "orderId" in response:
        threading.Thread(target=wait_and_finalize_exit, args=(symbol, response["orderId"], reason), daemon=True).start()

    return response


def wait_and_finalize_exit(symbol, order_id, reason):
    while True:
        order_status = binance_signed_request("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})
        if order_status.get("status") == "FILLED":
            finalize_trade(symbol, reason)
            break
        time.sleep(1)


# ===============================
# ⏱️ 2-Bar Exit Logic
# ===============================
def check_two_bar_exit(symbol):
    try:
        trade = trades.get(symbol)
        if not trade:
            return

        interval_str = normalize_interval(trade.get("interval", "1m"))
        interval_seconds = interval_to_seconds(interval_str)
        print(f"🕒 2-bar check for {symbol} ({interval_str})")
        time.sleep(interval_seconds * 2)

        position_info = get_position_info(symbol)
        if not position_info:
            return

        pnl = float(position_info.get("unRealizedProfit", 0))
        amt = abs(float(position_info.get("positionAmt", 0)))
        if amt == 0:
            return

        if pnl < 0:
            side = "BUY" if float(position_info["positionAmt"]) > 0 else "SELL"
            execute_exit(symbol, side, reason="2-bar Exit Triggered")
            print(f"[AUTO-EXIT] {symbol}: 2-bar close exit")
    except Exception as e:
        print(f"⚠️ 2-bar check error for {symbol}: {e}")


# ===============================
# 🚀 Open Position Logic
# ===============================
def open_position(symbol, side, limit_price):
    active_count = count_active_trades()
    if active_count >= MAX_ACTIVE_TRADES:
        print(f"🚫 Max active trades reached ({active_count}/{MAX_ACTIVE_TRADES})")
        return {"status": "max_trades_reached"}

    existing_pos = get_position_info(symbol)
    if existing_pos and abs(float(existing_pos["positionAmt"])) > 0:
        close_side = "BUY" if float(existing_pos["positionAmt"]) < 0 else "SELL"
        execute_exit(symbol, close_side, reason="Opposite Signal / New Entry")

    set_leverage_and_margin(symbol)
    qty = calculate_quantity(symbol)

    trades[symbol] = {
        "side": side,
        "interval": trades.get(symbol, {}).get("interval", "1m"),
        "entry_time": time.time(),
        "closed": False,
        "two_bar_thread": False,
    }

    log_trade_entry(symbol, side, "PENDING", limit_price, trades[symbol]["interval"])
    print(f"[ENTRY] {symbol} {side} pending @ {limit_price}")

    response = binance_signed_request("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": side,
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": qty,
        "price": limit_price
    })

    if "orderId" in response:
        threading.Thread(target=wait_and_notify_filled_entry, args=(symbol, side, response["orderId"]), daemon=True).start()

    return response


def wait_and_notify_filled_entry(symbol, side, order_id):
    notified = False
    while True:
        order_status = binance_signed_request("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})
        status = order_status.get("status")
        executed_qty = float(order_status.get("executedQty", 0))
        avg_price = float(order_status.get("avgPrice") or order_status.get("price") or 0)

        if not notified and status in ("PARTIALLY_FILLED", "FILLED") and executed_qty > 0:
            interval_for_symbol = trades.get(symbol, {}).get("interval", "1m")
            log_trade_entry(symbol, side, order_id, avg_price, interval_for_symbol)
            print(f"[ENTRY FILLED] {symbol} {side} @ {avg_price}")
            notified = True

            trade = trades.get(symbol)
            if trade and not trade.get("two_bar_thread"):
                trades[symbol]["two_bar_thread"] = True
                threading.Thread(target=check_two_bar_exit, args=(symbol,), daemon=True).start()

        if status in ("FILLED", "CANCELED", "REJECTED", "EXPIRED"):
            break
        time.sleep(1)


# ===============================
# 🌐 Webhook Endpoint
# ===============================
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_data(as_text=True)
    try:
        parts = [p.strip() for p in data.split("|")]
        if len(parts) >= 6:
            ticker, comment, close_price, bar_high, bar_low, interval = parts[:6]
        else:
            ticker, comment, close_price, interval = parts[0], parts[1], parts[2], parts[-1]
            bar_high = bar_low = None

        interval = normalize_interval(interval)
        symbol = ticker.replace("USDT", "") + "USDT"
        close_price = float(close_price)

        trades[symbol] = trades.get(symbol, {})
        trades[symbol]["interval"] = interval

        if comment == "BUY_ENTRY":
            open_position(symbol, "BUY", close_price)
        elif comment == "SELL_ENTRY":
            open_position(symbol, "SELL", close_price)
        elif comment in ["CROSS_EXIT_SHORT", "EXIT_LONG"]:
            execute_exit(symbol, "BUY", bar_high, bar_low, reason="Signal Exit")
        elif comment in ["CROSS_EXIT_LONG", "EXIT_SHORT"]:
            execute_exit(symbol, "SELL", bar_high, bar_low, reason="Signal Exit")
        else:
            return jsonify({"error": f"Unknown comment: {comment}"})

        return jsonify({"status": "ok"})
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
