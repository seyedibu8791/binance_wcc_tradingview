from flask import Flask, request, jsonify
import requests, hmac, hashlib, time, threading, os
from config import *
from trade_notifier import log_trade_entry, log_trade_exit, trades, send_telegram_message

app = Flask(__name__)

# ===== Binance Helpers =====
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


def set_leverage_and_margin(symbol):
    try:
        binance_signed_request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEVERAGE})
        binance_signed_request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": MARGIN_TYPE})
    except Exception as e:
        print("❌ Failed to set leverage/margin:", e)


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


# ===== Active Trades =====
def count_active_trades():
    try:
        positions = binance_signed_request("GET", "/fapi/v2/positionRisk")
        active_positions = [p for p in positions if abs(float(p["positionAmt"])) > 0]
        return len(active_positions)
    except Exception as e:
        print("❌ Failed to fetch active trades:", e)
        return 0


# ===== Calculate Quantity =====
def calculate_quantity(symbol):
    try:
        price_data = requests.get(f"{BASE_URL}/fapi/v1/ticker/price", params={"symbol": symbol}).json()
        price = float(price_data["price"])
        position_value = TRADE_AMOUNT * LEVERAGE
        qty = position_value / price
        qty = round_quantity(symbol, qty)
        return qty
    except Exception as e:
        print("❌ Failed to calculate quantity:", e)
        return 0.001


# ===== Helper: Interval Conversion =====
def interval_to_seconds(interval):
    mapping = {
        "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
        "1h": 3600, "2h": 7200, "4h": 14400, "1d": 86400
    }
    return mapping.get(interval, 60)


# ===== Helper: Get Position Info =====
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


# ===== 2-Bar Exit Logic =====
def check_two_bar_exit(symbol):
    try:
        trade = trades.get(symbol)
        if not trade:
            return

        interval_str = trade.get("interval", "1m")
        interval_seconds = interval_to_seconds(interval_str)

        print(f"🕒 Starting 2-bar timer for {symbol} ({interval_str})")
        time.sleep(interval_seconds * 2)

        position_info = get_position_info(symbol)
        if not position_info:
            return

        pnl = float(position_info.get("unRealizedProfit", 0))
        amt = abs(float(position_info.get("positionAmt", 0)))
        if amt == 0:
            return  # position already closed

        if pnl < 0:
            side = "BUY" if float(position_info["positionAmt"]) > 0 else "SELL"
            execute_market_exit(symbol, side)
            send_telegram_message(f"2 bar close exit | {symbol} | PnL: {pnl:.4f}")
            print(f"[AUTO-EXIT] {symbol}: 2 bar close exit, PnL={pnl:.4f}")
    except Exception as e:
        print(f"⚠️ Error in 2-bar exit check for {symbol}: {e}")


# ===== Open Position =====
def open_position(symbol, side, limit_price):
    active_count = count_active_trades()
    if active_count >= MAX_ACTIVE_TRADES:
        print(f"🚫 Max active trades reached ({active_count}/{MAX_ACTIVE_TRADES})")
        return {"status": "max_trades_reached"}

    set_leverage_and_margin(symbol)
    qty = calculate_quantity(symbol)

    if symbol not in trades or trades[symbol].get("closed", True):
        trades[symbol] = {
            "side": side,
            "interval": trades.get(symbol, {}).get("interval", "1m"),
            "entry_time": time.time(),
            "closed": False,
            "two_bar_thread": False
        }
        log_trade_entry(symbol, side, "PENDING", limit_price)

    response = binance_signed_request("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": side,
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": qty,
        "price": limit_price
    })

    if "orderId" in response:
        order_id = response["orderId"]
        threading.Thread(target=wait_and_notify_filled_entry, args=(symbol, side, order_id), daemon=True).start()

    return response


def wait_and_notify_filled_entry(symbol, side, order_id):
    notified = False
    while True:
        order_status = binance_signed_request("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})
        status = order_status.get("status")
        executed_qty = float(order_status.get("executedQty", 0))
        avg_price = float(order_status.get("avgPrice") or order_status.get("price") or 0)

        if not notified and status in ("PARTIALLY_FILLED", "FILLED") and executed_qty > 0:
            log_trade_entry(symbol, side, order_id, avg_price)
            notified = True

            # ✅ Start 2-bar timer only after order fills and if not already started
            trade = trades.get(symbol)
            if trade and not trade.get("two_bar_thread"):
                trades[symbol]["two_bar_thread"] = True
                threading.Thread(target=check_two_bar_exit, args=(symbol,), daemon=True).start()

        if status in ("FILLED", "CANCELED", "REJECTED", "EXPIRED"):
            break

        time.sleep(1)


# ===== Market Exit =====
def execute_market_exit(symbol, side):
    pos_data = binance_signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    if not pos_data or abs(float(pos_data[0]["positionAmt"])) == 0:
        print(f"⚠️ No active position for {symbol} to close.")
        return {"status": "no_position"}

    qty = abs(float(pos_data[0]["positionAmt"]))
    qty = round_quantity(symbol, qty)
    close_side = "SELL" if side == "BUY" else "BUY"

    response = binance_signed_request("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": close_side,
        "type": "MARKET",
        "quantity": qty
    })

    if "orderId" in response:
        threading.Thread(target=wait_and_notify_filled_exit, args=(symbol, response["orderId"]), daemon=True).start()

    return response


def wait_and_notify_filled_exit(symbol, order_id):
    while True:
        order_status = binance_signed_request("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})
        if order_status.get("status") == "FILLED":
            filled_price = float(order_status.get("avgPrice") or order_status.get("price") or 0)
            log_trade_exit(symbol, order_id, filled_price)
            clean_residual_positions(symbol)
            break
        time.sleep(1)


# ===== Residual Clean-up =====
def clean_residual_positions(symbol):
    try:
        binance_signed_request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})
        pos_data = binance_signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
        if pos_data and abs(float(pos_data[0]["positionAmt"])) > 0.00001:
            amt = abs(float(pos_data[0]["positionAmt"]))
            side = "SELL" if float(pos_data[0]["positionAmt"]) > 0 else "BUY"
            binance_signed_request("POST", "/fapi/v1/order", {
                "symbol": symbol,
                "side": side,
                "type": "MARKET",
                "quantity": round_quantity(symbol, amt)
            })
            print(f"🧹 Residual position cleaned for {symbol}")
    except Exception as e:
        print("⚠️ Residual cleanup failed:", e)


# ===== Async Exit & Open =====
def async_exit_and_open(symbol, new_side, limit_price):
    def worker():
        pos_data = binance_signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
        amt = float(pos_data[0]["positionAmt"]) if pos_data else 0
        opposite_side = None

        if amt > 0 and new_side == "SELL":
            opposite_side = "BUY"
        elif amt < 0 and new_side == "BUY":
            opposite_side = "SELL"

        if opposite_side:
            execute_market_exit(symbol, opposite_side)
            time.sleep(OPPOSITE_CLOSE_DELAY)

        open_position(symbol, new_side, limit_price)

    threading.Thread(target=worker, daemon=True).start()


# ===== Webhook =====
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

        symbol = ticker.replace("USDT", "") + "USDT"
        close_price = float(close_price)

        if comment == "BUY_ENTRY":
            trades[symbol] = trades.get(symbol, {})
            trades[symbol]["interval"] = interval
            async_exit_and_open(symbol, "BUY", close_price)
        elif comment == "SELL_ENTRY":
            trades[symbol] = trades.get(symbol, {})
            trades[symbol]["interval"] = interval
            async_exit_and_open(symbol, "SELL", close_price)
        elif comment == "CROSS_EXIT_SHORT":
            execute_market_exit(symbol, "BUY")
        elif comment == "CROSS_EXIT_LONG":
            execute_market_exit(symbol, "SELL")
        elif comment == "EXIT_LONG":
            execute_market_exit(symbol, "BUY")
        elif comment == "EXIT_SHORT":
            execute_market_exit(symbol, "SELL")
        else:
            return jsonify({"error": f"Unknown comment: {comment}"})

        return jsonify({"status": "ok"})
    except Exception as e:
        print("❌ Webhook Error:", e)
        return jsonify({"error": str(e)})


# ===== Ping =====
@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200


# ===== Self Ping =====
def self_ping():
    while True:
        try:
            requests.get(f"https://tradingview-binance-2o1v.onrender.com/ping")
        except:
            pass
        time.sleep(5 * 60)


threading.Thread(target=self_ping, daemon=True).start()


# ===== Run Flask =====
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
