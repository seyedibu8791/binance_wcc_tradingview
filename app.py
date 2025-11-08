# ===============================
# app.py (Updated - Unified exit + entry + 2-bar logic)
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
    send_telegram_message,
)

app = Flask(__name__)

# -------------------------
# Interval normalization map
# -------------------------
INTERVAL_MAP = {
    "1": "1m", "3": "3m", "5": "5m", "15": "15m", "30": "30m",
    "60": "1h", "120": "2h", "240": "4h",
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1h", "2h": "2h", "4h": "4h", "1d": "1d", "D": "1d", "1D": "1d"
}

def normalize_interval(raw):
    if raw is None:
        return "1m"
    key = str(raw).strip()
    return INTERVAL_MAP.get(key, key).lower()

def trade_key(symbol: str, interval: str) -> str:
    return f"{symbol}_{interval.lower()}"


# ===============================
# Binance Signed Request Helper
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
# Leverage & Margin Setup
# ===============================
def set_leverage_and_margin(symbol):
    try:
        binance_signed_request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEVERAGE})
        binance_signed_request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": MARGIN_TYPE})
    except Exception as e:
        print("❌ Failed to set leverage/margin:", e)


# ===============================
# Quantity & Position Helpers
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
    step_size = float([f["stepSize"] for f in info["filters"] if f["filterType"]=="LOT_SIZE"][0])
    min_qty = float([f["minQty"] for f in info["filters"] if f["filterType"]=="LOT_SIZE"][0])
    try:
        multiples = int(qty / step_size)
        qty = float(multiples * step_size)
    except:
        qty = float(step_size)
    if qty < min_qty:
        qty = min_qty
    return round(qty, 8)

def count_active_trades():
    try:
        positions = binance_signed_request("GET", "/fapi/v2/positionRisk")
        if isinstance(positions, dict) and positions.get("error"):
            return 0
        active_positions = [p for p in positions if abs(float(p.get("positionAmt", 0)))>0]
        return len(active_positions)
    except Exception as e:
        print("❌ Failed to fetch active trades:", e)
        return 0

def calculate_quantity(symbol):
    try:
        price_data = requests.get(f"{BASE_URL}/fapi/v1/ticker/price", params={"symbol": symbol}).json()
        price = float(price_data["price"])
        qty = (TRADE_AMOUNT * LEVERAGE) / price
        return round_quantity(symbol, qty)
    except Exception as e:
        print("❌ Failed to calculate quantity:", e)
        return 0.001

def get_position_info(symbol):
    timestamp = int(time.time() * 1000)
    query_string = f"timestamp={timestamp}&symbol={symbol}"
    signature = hmac.new(BINANCE_SECRET_KEY.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    url = f"{BASE_URL}/fapi/v2/positionRisk?{query_string}&signature={signature}"
    resp = requests.get(url, headers=headers)
    if resp.status_code == 200 and isinstance(resp.json(), list) and len(resp.json())>0:
        return resp.json()[0]
    return None

def compute_implied_pnl_dollar(entry_price, exit_price, position_qty, side):
    if position_qty==0:
        return 0.0
    if side.upper()=="BUY":
        return (exit_price-entry_price)*position_qty
    else:
        return (entry_price-exit_price)*position_qty


# ===============================
# Unified Trade Exit Finalizer
# ===============================
def finalize_trade(symbol, reason):
    try:
        found_interval = "1m"
        with trades_lock:
            for k in list(trades.keys()):
                if k.startswith(f"{symbol}_"):
                    found_interval = k.split("_",1)[1]
                    break
        interval = found_interval

        timestamp = int(time.time() * 1000)
        query_string = f"symbol={symbol}&timestamp={timestamp}"
        signature = hmac.new(BINANCE_SECRET_KEY.encode(), query_string.encode(), hashlib.sha256).hexdigest()
        headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
        url = f"{BASE_URL}/fapi/v1/userTrades?{query_string}&signature={signature}"
        resp = requests.get(url, headers=headers)
        if resp.status_code!=200:
            print(f"⚠️ Binance trade fetch failed for {symbol}: {resp.text}")
            return
        trade_data = resp.json()
        if not trade_data:
            print(f"⚠️ No recent trade data for {symbol}")
            return
        last_trade = trade_data[-1]
        filled_price = float(last_trade.get("price",0.0))
        realized_pnl = float(last_trade.get("realizedPnl",0.0))
        qty = float(last_trade.get("qty",0.0))

        with trades_lock:
            local_trade = trades.get(trade_key(symbol, interval),{}) or trades.get(symbol,{})
            entry_price = local_trade.get("entry_price", filled_price)
            order_id = local_trade.get("order_id", last_trade.get("orderId") or f"auto_{int(time.time())}")

        pnl = round(realized_pnl,2)
        pnl_percent = 0.0
        try:
            if qty>0 and entry_price>0:
                pnl_percent = round((realized_pnl/(qty*entry_price))*100,2)
        except:
            pnl_percent=0.0

        log_trade_exit(symbol, filled_price, pnl, pnl_percent, reason=reason, interval=interval, order_id=order_id)
        print(f"[EXIT] {symbol} closed | {reason} | Exit: {filled_price} | PnL: {pnl} ({pnl_percent}%)")

        with trades_lock:
            keys_to_remove = [k for k in trades.keys() if k.startswith(f"{symbol}_")]
            for k in keys_to_remove:
                trades.pop(k,None)

    except Exception as e:
        print(f"❌ finalize_trade() error for {symbol}: {e}")


# ===============================
# Exit Helpers
# ===============================
def get_exit_qty(symbol):
    pos_data = binance_signed_request("GET","/fapi/v2/positionRisk",{"symbol":symbol})
    if not pos_data:
        return 0
    try:
        qty = abs(float(pos_data[0].get("positionAmt",0)))
    except:
        qty=0
    return round_quantity(symbol, qty)

def execute_exit(symbol, side, interval="1m", bar_high=None, bar_low=None, reason="Manual Exit"):
    try:
        key = trade_key(symbol,interval)
        with trades_lock:
            t = trades.get(key) or trades.get(symbol)
            if not t or t.get("entry_filled") is not True:
                print(f"⚠️ {symbol} exit ignored — entry not yet confirmed filled.")
                return {"status":"entry_not_filled"}

        if EXIT_MARKET_DELAY_ENABLED:
            print(f"[{symbol}] Exit delay active → waiting {EXIT_MARKET_DELAY}s...")
            time.sleep(EXIT_MARKET_DELAY)

        if USE_BAR_HIGH_LOW_FOR_EXIT and bar_high and bar_low:
            limit_price = float(bar_high) if side.upper()=="BUY" else float(bar_low)
            print(f"[{symbol}] Attempting limit exit @ {limit_price} ({side})")
            limit_order = binance_signed_request("POST","/fapi/v1/order",{
                "symbol":symbol,
                "side":"SELL" if side.upper()=="BUY" else "BUY",
                "type":"LIMIT",
                "timeInForce":"GTC",
                "quantity":get_exit_qty(symbol),
                "price":limit_price
            })
            order_id = limit_order.get("orderId")
            start_time=time.time()
            while time.time()-start_time<BAR_EXIT_TIMEOUT_SEC:
                order_status = binance_signed_request("GET","/fapi/v1/order",{"symbol":symbol,"orderId":order_id})
                if order_status.get("status")=="FILLED":
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
    pos_data = binance_signed_request("GET","/fapi/v2/positionRisk",{"symbol":symbol})
    if not pos_data or abs(float(pos_data[0].get("positionAmt",0)))==0:
        print(f"⚠️ No active position for {symbol}")
        with trades_lock:
            keys_to_remove = [k for k in trades.keys() if k.startswith(f"{symbol}_")]
            for k in keys_to_remove:
                trades.pop(k,None)
        return {"status":"no_position"}

    qty = abs(float(pos_data[0].get("positionAmt",0)))
    qty = round_quantity(symbol, qty)
    close_side = "SELL" if side.upper()=="BUY" else "BUY"
    response = binance_signed_request("POST","/fapi/v1/order",{"symbol":symbol,"side":close_side,"type":"MARKET","quantity":qty})
    if isinstance(response, dict) and "orderId" in response:
        threading.Thread(target=wait_and_finalize_exit,args=(symbol,response["orderId"],reason),daemon=True).start()
    return response

def wait_and_finalize_exit(symbol, order_id, reason):
    while True:
        order_status = binance_signed_request("GET","/fapi/v1/order",{"symbol":symbol,"orderId":order_id})
        if isinstance(order_status, dict) and order_status.get("status")=="FILLED":
            finalize_trade(symbol, reason)
            break
        time.sleep(1)


# ===============================
# 2-Bar Force Exit Worker
# ===============================
def two_bar_force_exit_worker(symbol, interval_str):
    try:
        interval_seconds = interval_to_seconds(interval_str)
        key = trade_key(symbol,interval_str)
        with trades_lock:
            trade = trades.get(key) or trades.get(symbol)
            if not trade:
                return
            entry_time = trade.get("entry_time", time.time())

        target = entry_time+(2*interval_seconds)
        remaining = target - time.time()
        if remaining>0:
            time.sleep(remaining)

        pos = get_position_info(symbol)
        if not pos or abs(float(pos.get("positionAmt",0)))==0:
            with trades_lock:
                keys_to_remove = [k for k in trades.keys() if k.startswith(f"{symbol}_")]
                for k in keys_to_remove:
                    trades.pop(k,None)
            return

        side = "BUY" if float(pos.get("positionAmt"))>0 else "SELL"
        print(f"[2-BAR FORCE] {symbol} → forcing market exit after 2 bars.")
        execute_market_exit(symbol, side, reason="2-Bar Force Exit")

    except Exception as e:
        print(f"❌ two_bar_force_exit_worker error for {symbol}: {e}")


# ===============================
# Open Position Logic
# ===============================
def open_position(symbol, side, limit_price, interval="1m"):
    pos_info = get_position_info(symbol)
    if pos_info and abs(float(pos_info.get("positionAmt",0)))>0:
        existing_side = "BUY" if float(pos_info.get("positionAmt"))>0 else "SELL"
        print(f"⚠️ Existing Binance position detected for {symbol} (side={existing_side}). Closing it to replace with new entry.")
        execute_market_exit(symbol, existing_side, reason="Replacing Entry: Opposite direction signal")
        wait_start=time.time()
        wait_timeout=20
        cleared=False
        while time.time()-wait_start<wait_timeout:
            time.sleep(1)
            cur = get_position_info(symbol)
            if not cur or abs(float(cur.get("positionAmt",0)))==0:
                cleared=True
                break
        if not cleared:
            print(f"⚠️ Position for {symbol} did not clear within {wait_timeout}s. Attempting new entry anyway.")

    if count_active_trades()>=MAX_ACTIVE_TRADES:
        print(f"🚫 Max active trades reached")
        return {"status":"max_trades_reached"}

    set_leverage_and_margin(symbol)
    qty = calculate_quantity(symbol)
    key = trade_key(symbol,interval)
    with trades_lock:
        trades[key] = {
            "symbol":symbol,
            "side":side.upper(),
            "interval":interval,
            "entry_time":time.time(),
            "entry_filled":False,
            "order_id":None,
            "closed":False,
            "two_bar_thread_started":False,
        }

    response = binance_signed_request("POST","/fapi/v1/order",{
        "symbol":symbol,
        "side":side,
        "type":"LIMIT",
        "timeInForce":"GTC",
        "quantity":qty,
        "price":limit_price
    })

    if isinstance(response, dict) and "orderId" in response:
        order_id=response["orderId"]
        with trades_lock:
            if key in trades:
                trades[key]["order_id"]=order_id
        threading.Thread(target=wait_and_notify_filled_entry,args=(symbol,side,order_id,interval),daemon=True).start()
    return response

def wait_and_notify_filled_entry(symbol, side, order_id, interval="1m"):
    notified=False
    try:
        key=trade_key(symbol,interval)
        while True:
            order_status=binance_signed_request("GET","/fapi/v1/order",{"symbol":symbol,"orderId":order_id})
            status=order_status.get("status")
            executed_qty=float(order_status.get("executedQty",0) or 0)
            avg_price=float(order_status.get("avgPrice") or order_status.get("price") or 0)
            if not notified and status in ("PARTIALLY_FILLED","FILLED") and executed_qty>0:
                with trades_lock:
                    trade = trades.get(key,{})
                    trade["entry_filled"]=True
                    trade["entry_price"]=avg_price
                    trade["entry_time"]=time.time()
                    trade["order_id"]=order_id
                    trade["position_qty"]=executed_qty
                log_trade_entry(symbol, side, avg_price, order_id=order_id, interval=interval)
                print(f"[ENTRY FILLED] {symbol} {side} @ {avg_price} ({interval})")
                notified=True
                with trades_lock:
                    if not trades.get(key,{}).get("two_bar_thread_started"):
                        trades[key]["two_bar_thread_started"]=True
                        threading.Thread(target=two_bar_force_exit_worker,args=(symbol,interval),daemon=True).start()
            if status in ("FILLED","CANCELED","REJECTED","EXPIRED"):
                break
            time.sleep(1)
    except Exception as e:
        print(f"❌ wait_and_notify_filled_entry error for {symbol}: {e}")


# ===============================
# Evaluate Exit Signal
# ===============================
def evaluate_exit_signal(symbol, alert_close_price, alert_side, bar_high=None, bar_low=None, interval_hint="1m"):
    try:
        pos=get_position_info(symbol)
        if not pos or abs(float(pos.get("positionAmt",0)))==0:
            print(f"⚠️ evaluate_exit_signal: no active position for {symbol}")
            with trades_lock:
                keys_to_remove=[k for k in trades.keys() if k.startswith(f"{symbol}_")]
                for k in keys_to_remove: trades.pop(k,None)
            return {"status":"no_position"}

        position_amt=abs(float(pos.get("positionAmt",0)))
        side="BUY" if float(pos.get("positionAmt"))>0 else "SELL"

        interval=interval_hint
        with trades_lock:
            for k in trades.keys():
                if k.startswith(f"{symbol}_"):
                    interval=k.split("_",1)[1]
                    break
            trade=trades.get(trade_key(symbol,interval),{})
            entry_price=trade.get("entry_price",float(pos.get("entryPrice",0.0)))
            entry_time=trade.get("entry_time",time.time())
            entry_filled=trade.get("entry_filled",False)

        alert_close_price=float(alert_close_price)
        implied_pnl_from_alert=compute_implied_pnl_dollar(entry_price,alert_close_price,position_amt,side)

        interval_seconds=interval_to_seconds(interval)
        two_bar_end=entry_time+(2*interval_seconds)
        now=time.time()

        print(f"[EXIT EVAL] {symbol} | now={now}, two_bar_end={two_bar_end} | current_unrealized={float(pos.get('unRealizedProfit',0.0)):.4f} | alert_implied={implied_pnl_from_alert:.4f}")

        if now<two_bar_end:
            if implied_pnl_from_alert<float(pos.get("unRealizedProfit",0.0)):
                print(f"[EXIT EVAL] {symbol} → alert_implied_pnl worse → closing now")
                if USE_BAR_HIGH_LOW_FOR_EXIT and bar_high and bar_low:
                    res=execute_exit(symbol,side,interval=interval,bar_high=bar_high,bar_low=bar_low,reason="Exit Signal (early close)")
                    return {"status":"closed_early_attempted_limit","result":res}
                else:
                    execute_market_exit(symbol,side,reason="Exit Signal (early close)")
                    return {"status":"closed_early_market"}
            else:
                print(f"[EXIT EVAL] {symbol} → alert_implied_pnl not worse. Waiting 2-bar end")
                return {"status":"wait_2bar"}
        else:
            print(f"[EXIT EVAL] {symbol} → after 2-bar end → closing now")
            if USE_BAR_HIGH_LOW_FOR_EXIT and bar_high and bar_low:
                res=execute_exit(symbol,side,interval=interval,bar_high=bar_high,bar_low=bar_low,reason="Exit Signal (post 2-bar)")
                return {"status":"closed_post_2bar_attempted_limit","result":res}
            else:
                execute_market_exit(symbol,side,reason="Exit Signal (post 2-bar)")
                return {"status":"closed_post_2bar_market"}
    except Exception as e:
        print(f"❌ evaluate_exit_signal error for {symbol}: {e}")
        return {"error":str(e)}


# ===============================
# Webhook Endpoint
# ===============================
@app.route("/webhook",methods=["POST"])
def webhook():
    data=request.get_data(as_text=True)
    try:
        parts=[p.strip() for p in data.split("|")]
        if len(parts)>=6:
            ticker,comment,close_price,bar_high,bar_low,interval=parts[:6]
        else:
            ticker,comment,close_price,interval=parts[0],parts[1],parts[2],parts[-1]
            bar_high=bar_low=None

        interval=normalize_interval(interval)
        symbol=ticker.replace("USDT","")+"USDT"
        close_price=float(close_price)

        with trades_lock:
            k=trade_key(symbol,interval)
            trades[k]=trades.get(k,{})
            trades[k]["interval"]=interval

        if comment=="BUY_ENTRY":
            return jsonify(open_position(symbol,"BUY",close_price,interval=interval))
        elif comment=="SELL_ENTRY":
            return jsonify(open_position(symbol,"SELL",close_price,interval=interval))
        elif comment in ["CROSS_EXIT_SHORT","CROSS_EXIT_LONG","OPPOSITE_EXIT","SAME_SIDE_EXIT"]:
            pos=get_position_info(symbol)
            if pos and abs(float(pos.get("positionAmt",0)))>0:
                side="BUY" if float(pos.get("positionAmt"))>0 else "SELL"
                if USE_BAR_HIGH_LOW_FOR_EXIT and bar_high and bar_low:
                    execute_exit(symbol,side,interval=interval,bar_high=bar_high,bar_low=bar_low,reason=f"Signal: {comment}")
                else:
                    execute_market_exit(symbol,side,reason=f"Signal: {comment}")
                return jsonify({"status":"closed_by_opposite_same_cross"})
            else:
                with trades_lock:
                    keys_to_remove=[k for k in trades.keys() if k.startswith(f"{symbol}_")]
                    for k in keys_to_remove: trades.pop(k,None)
                return jsonify({"status":"no_position"})
        elif comment in ["EXIT_LONG","EXIT_SHORT","SIGNAL_EXIT"]:
            result=evaluate_exit_signal(symbol,close_price,comment,bar_high,bar_low,interval_hint=interval)
            return jsonify(result)
        else:
            return jsonify({"error":f"Unknown comment: {comment}"})
    except Exception as e:
        print("❌ Webhook Error:", e)
        return jsonify({"error":str(e)})


# ===============================
# Ping & Self-ping
# ===============================
@app.route("/ping",methods=["GET"])
def ping():
    return "pong",200

def self_ping():
    while True:
        try:
            requests.get(os.getenv("SELF_PING_URL","https://binance-wcc-tradingview.onrender.com/ping"))
        except:
            pass
        time.sleep(5*60)

threading.Thread(target=self_ping,daemon=True).start()

if __name__=="__main__":
    port=int(os.getenv("PORT",5000))
    app.run(host="0.0.0.0",port=port)
