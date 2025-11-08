# ===============================
# app.py (Full Async Version - Unified Entry, Exit, 2-Bar, Evaluate Exit Signal)
# ===============================

import os, hmac, hashlib, time, asyncio
from flask import Flask, request, jsonify
import aiohttp
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
# Async Binance Request Helper
# ===============================
async def binance_signed_request(http_method, path, params=None):
    if params is None:
        params = {}
    params["timestamp"] = int(time.time() * 1000)
    query = "&".join([f"{k}={v}" for k, v in params.items()])
    signature = hmac.new(BINANCE_SECRET_KEY.encode(), query.encode(), hashlib.sha256).hexdigest()
    query += f"&signature={signature}"
    url = f"{BASE_URL}{path}?{query}"
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    try:
        async with aiohttp.ClientSession() as session:
            if http_method.upper() == "POST":
                async with session.post(url, headers=headers) as r:
                    return await r.json()
            elif http_method.upper() == "DELETE":
                async with session.delete(url, headers=headers) as r:
                    return await r.json()
            else:
                async with session.get(url, headers=headers) as r:
                    return await r.json()
    except Exception as e:
        print("❌ Binance request failed:", e)
        return {"error": str(e)}

# ===============================
# Leverage / Margin / Qty Helpers
# ===============================
async def set_leverage_and_margin(symbol):
    try:
        await binance_signed_request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEVERAGE})
        await binance_signed_request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": MARGIN_TYPE})
    except Exception as e:
        print("❌ Failed to set leverage/margin:", e)

async def get_symbol_info(symbol):
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{BASE_URL}/fapi/v1/exchangeInfo") as r:
            info = await r.json()
            for s in info.get("symbols", []):
                if s["symbol"] == symbol:
                    return s
    return None

def round_quantity(symbol, qty):
    info = asyncio.run(get_symbol_info(symbol))
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

async def calculate_quantity(symbol):
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{BASE_URL}/fapi/v1/ticker/price", params={"symbol": symbol}) as r:
            price_data = await r.json()
            price = float(price_data["price"])
            qty = (TRADE_AMOUNT * LEVERAGE) / price
            return round_quantity(symbol, qty)

async def get_position_info(symbol):
    timestamp = int(time.time() * 1000)
    query_string = f"timestamp={timestamp}&symbol={symbol}"
    signature = hmac.new(BINANCE_SECRET_KEY.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    url = f"{BASE_URL}/fapi/v2/positionRisk?{query_string}&signature={signature}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as r:
            data = await r.json()
            if isinstance(data, list) and len(data) > 0:
                return data[0]
    return None

def compute_implied_pnl_dollar(entry_price, exit_price, position_qty, side):
    if position_qty==0:
        return 0.0
    if side.upper()=="BUY":
        return (exit_price-entry_price)*position_qty
    else:
        return (entry_price-exit_price)*position_qty

# ===============================
# Unified Exit & Finalize
# ===============================
async def finalize_trade(symbol, reason):
    try:
        found_interval = "1m"
        async with trades_lock:
            for k in list(trades.keys()):
                if k.startswith(f"{symbol}_"):
                    found_interval = k.split("_",1)[1]
                    break
        interval = found_interval

        async with trades_lock:
            keys_to_remove = [k for k in trades.keys() if k.startswith(f"{symbol}_")]
            for k in keys_to_remove: trades.pop(k,None)

        timestamp = int(time.time() * 1000)
        query_string = f"symbol={symbol}&timestamp={timestamp}"
        signature = hmac.new(BINANCE_SECRET_KEY.encode(), query_string.encode(), hashlib.sha256).hexdigest()
        headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
        url = f"{BASE_URL}/fapi/v1/userTrades?{query_string}&signature={signature}"

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    print(f"⚠️ Binance trade fetch failed for {symbol}: {resp.status}")
                    return
                trade_data = await resp.json()
        if not trade_data:
            print(f"⚠️ No recent trade data for {symbol}")
            return
        last_trade = trade_data[-1]
        filled_price = float(last_trade.get("price",0.0))
        realized_pnl = float(last_trade.get("realizedPnl",0.0))
        qty = float(last_trade.get("qty",0.0))

        pnl = round(realized_pnl,2)
        pnl_percent = round((realized_pnl/(qty*filled_price)*100),2) if qty>0 and filled_price>0 else 0.0

        log_trade_exit(symbol, filled_price, pnl, pnl_percent, reason=reason, interval=interval, order_id=last_trade.get("orderId"))
        print(f"[EXIT] {symbol} closed | {reason} | Exit: {filled_price} | PnL: {pnl} ({pnl_percent}%)")
    except Exception as e:
        print(f"❌ finalize_trade() error for {symbol}: {e}")

async def execute_market_exit(symbol, side, reason="Market Exit"):
    pos_data = await get_position_info(symbol)
    if not pos_data or abs(float(pos_data.get("positionAmt",0)))==0:
        print(f"⚠️ No active position for {symbol}")
        async with trades_lock:
            keys_to_remove = [k for k in trades.keys() if k.startswith(f"{symbol}_")]
            for k in keys_to_remove:
                trades.pop(k,None)
        return {"status":"no_position"}

    qty = abs(float(pos_data.get("positionAmt",0)))
    qty = round_quantity(symbol, qty)
    close_side = "SELL" if side.upper()=="BUY" else "BUY"
    response = await binance_signed_request("POST","/fapi/v1/order",{"symbol":symbol,"side":close_side,"type":"MARKET","quantity":qty})
    asyncio.create_task(wait_and_finalize_exit(symbol,response.get("orderId"),reason))
    return response

async def wait_and_finalize_exit(symbol, order_id, reason):
    while True:
        order_status = await binance_signed_request("GET","/fapi/v1/order",{"symbol":symbol,"orderId":order_id})
        if isinstance(order_status, dict) and order_status.get("status")=="FILLED":
            await finalize_trade(symbol, reason)
            break
        await asyncio.sleep(1)

# ===============================
# 2-Bar Force Exit
# ===============================
async def two_bar_force_exit_worker(symbol, interval_str):
    interval_seconds = interval_to_seconds(interval_str)
    async with trades_lock:
        trade = trades.get(trade_key(symbol, interval_str)) or trades.get(symbol)
        if not trade:
            return
        entry_time = trade.get("entry_time", time.time())

    remaining = (entry_time + 2*interval_seconds) - time.time()
    if remaining>0:
        await asyncio.sleep(remaining)

    pos = await get_position_info(symbol)
    if not pos or abs(float(pos.get("positionAmt",0)))==0:
        async with trades_lock:
            keys_to_remove = [k for k in trades.keys() if k.startswith(f"{symbol}_")]
            for k in keys_to_remove:
                trades.pop(k,None)
        return

    side = "BUY" if float(pos.get("positionAmt"))>0 else "SELL"
    print(f"[2-BAR FORCE] {symbol} → forcing market exit after 2 bars.")
    await execute_market_exit(symbol, side, reason="2-Bar Force Exit")

# ===============================
# Evaluate Exit Signal (Async)
# ===============================
async def evaluate_exit_signal(symbol, alert_close_price, alert_side, bar_high=None, bar_low=None, interval_hint="1m"):
    try:
        pos = await get_position_info(symbol)
        if not pos or abs(float(pos.get("positionAmt",0)))==0:
            print(f"⚠️ evaluate_exit_signal: no active position for {symbol}")
            async with trades_lock:
                keys_to_remove=[k for k in trades.keys() if k.startswith(f"{symbol}_")]
                for k in keys_to_remove: trades.pop(k,None)
            return {"status":"no_position"}

        position_amt=abs(float(pos.get("positionAmt",0)))
        side="BUY" if float(pos.get("positionAmt"))>0 else "SELL"

        interval=interval_hint
        async with trades_lock:
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
                await execute_market_exit(symbol, side, reason="Exit Signal (early close)")
                return {"status":"closed_early_market"}
            else:
                print(f"[EXIT EVAL] {symbol} → alert_implied_pnl not worse. Waiting 2-bar end")
                return {"status":"wait_2bar"}
        else:
            print(f"[EXIT EVAL] {symbol} → after 2-bar end → closing now")
            await execute_market_exit(symbol, side, reason="Exit Signal (post 2-bar)")
            return {"status":"closed_post_2bar_market"}
    except Exception as e:
        print(f"❌ evaluate_exit_signal error for {symbol}: {e}")
        return {"error":str(e)}

# ===============================
# Webhook
# ===============================
@app.route("/webhook",methods=["POST"])
def webhook():
    data=request.get_data(as_text=True)
    asyncio.create_task(handle_webhook(data))
    return jsonify({"status":"processing"})

async def handle_webhook(data):
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

        async with trades_lock:
            k=trade_key(symbol,interval)
            trades[k]=trades.get(k,{})
            trades[k]["interval"]=interval

        if comment=="BUY_ENTRY":
            await open_position(symbol,"BUY",close_price,interval=interval)
        elif comment=="SELL_ENTRY":
            await open_position(symbol,"SELL",close_price,interval=interval)
        elif comment in ["CROSS_EXIT_SHORT","CROSS_EXIT_LONG","OPPOSITE_EXIT","SAME_SIDE_EXIT"]:
            pos = await get_position_info(symbol)
            if pos and abs(float(pos.get("positionAmt",0)))>0:
                side="BUY" if float(pos.get("positionAmt"))>0 else "SELL"
                await execute_market_exit(symbol, side, reason=f"Signal: {comment}")
        elif comment in ["EXIT_LONG","EXIT_SHORT","SIGNAL_EXIT"]:
            await evaluate_exit_signal(symbol, close_price, comment, bar_high, bar_low, interval_hint=interval)
    except Exception as e:
        print("❌ handle_webhook error:", e)

# ===============================
# Ping & Self-Ping (Safe for Flask/Render)
# ===============================
@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200


def self_ping():
    """Periodically ping the app to prevent Render/Gunicorn sleep"""
    import time
    import requests
    url = os.getenv("SELF_PING_URL", "https://binance-wcc-tradingview.onrender.com/ping")
    while True:
        try:
            requests.get(url, timeout=10)
        except Exception as e:
            print(f"[PING ERROR] {e}")
        time.sleep(300)  # every 5 minutes


# Start the self-ping in a background thread
import threading
threading.Thread(target=self_ping, daemon=True).start()


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

