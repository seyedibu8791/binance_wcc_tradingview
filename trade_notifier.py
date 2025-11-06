# ==============================
# trade_notifier.py (FINAL - Binance-synced Exit + Unified Telegram)
# ==============================

import requests
import threading
import time
import datetime
from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    TRADE_AMOUNT, LEVERAGE, BASE_URL,
    BINANCE_API_KEY, BINANCE_SECRET_KEY,
    USE_BAR_HIGH_LOW_FOR_EXIT, BAR_EXIT_TIMEOUT_SEC,
    EXIT_MARKET_DELAY_ENABLED, EXIT_MARKET_DELAY,
)
from urllib.parse import urlencode
import hmac
import hashlib

# ==============================
# 🧾 STORAGE + LOCK
# ==============================
trades = {}
notified_orders = set()
trades_lock = threading.Lock()


# ==============================
# 📢 TELEGRAM HELPER
# ==============================
def send_telegram_message(message: str):
    try:
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            print("⚠️ Missing Telegram credentials. Skipping Telegram send.")
            return
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
        resp = requests.post(url, data=payload, timeout=10)
        if resp.status_code != 200:
            print("❌ Telegram error:", resp.text)
    except Exception as e:
        print("❌ Telegram send error:", e)


# ==============================
# 🟩 TRADE ENTRY
# ==============================
def log_trade_entry(symbol: str, side: str, order_id: str, filled_price: float, interval: str = "1m"):
    if order_id in notified_orders:
        return
    notified_orders.add(order_id)

    key = f"{symbol}_{interval.lower()}"

    with trades_lock:
        trades[key] = {
            "symbol": symbol,
            "side": side.upper(),
            "entry_price": filled_price,
            "order_id": order_id,
            "interval": interval.lower(),
            "closed": False,
            "entry_time": time.time(),
        }

    arrow = "⬆️" if side.upper() == "BUY" else "⬇️"
    trade_type = "Long Trade" if side.upper() == "BUY" else "Short Trade"

    msg = f"""{arrow} <b>{trade_type}</b>
Symbol: <b>#{symbol}</b>
Side: <b>{side}</b>
Interval: <b>{interval}</b>
--- ⌁ ---
Leverage: {LEVERAGE}x
Trade Amount: {TRADE_AMOUNT}$
--- ⌁ ---
Entry Price: <b>{filled_price}</b>
--- ⌁ ---
🕐 Wait for Exit Signal..
"""
    send_telegram_message(msg)


# ==============================
# 💹 BINANCE TRADE FETCH HELPERS
# ==============================
def get_binance_trade_details(symbol):
    """Fetch last closed trade info for symbol"""
    try:
        endpoint = "/fapi/v1/userTrades"
        timestamp = int(time.time() * 1000)
        params = {"symbol": symbol, "limit": 5, "timestamp": timestamp}
        query = urlencode(params)
        signature = hmac.new(BINANCE_SECRET_KEY.encode(), query.encode(), hashlib.sha256).hexdigest()
        url = f"{BASE_URL}{endpoint}?{query}&signature={signature}"
        headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            print("❌ Binance fetch failed:", resp.text)
            return None
        data = resp.json()
        if not data:
            return None
        last_trade = data[-1]
        return {
            "price": float(last_trade["price"]),
            "realizedPnl": float(last_trade.get("realizedPnl", 0)),
        }
    except Exception as e:
        print("⚠️ Binance trade fetch error:", e)
        return None


# ==============================
# 🟥 TRADE EXIT
# ==============================
def log_trade_exit(symbol: str, order_id: str, reason="Signal Exit", interval: str = "1m"):
    """Record exit & send unified Telegram message using Binance data"""
    key = f"{symbol}_{interval.lower()}"
    with trades_lock:
        trade = trades.get(key)
        if not trade or trade.get("closed"):
            return

        entry_price = trade["entry_price"]
        side = trade["side"]

    # Fetch latest Binance closed trade details
    trade_data = get_binance_trade_details(symbol)
    if trade_data:
        exit_price = trade_data["price"]
        pnl_dollar = trade_data["realizedPnl"]
        pnl_percent = round((pnl_dollar / TRADE_AMOUNT) * 100, 2) if TRADE_AMOUNT else 0
    else:
        exit_price = entry_price
        pnl_dollar = 0
        pnl_percent = 0

    with trades_lock:
        trade["exit_price"] = exit_price
        trade["pnl"] = pnl_dollar
        trade["pnl_percent"] = pnl_percent
        trade["closed"] = True

    header = "✅ Profit Achieved!" if pnl_dollar >= 0 else "⛔️ Ended in Loss!"
    msg = f"""{header}
Reason: <b>{reason}</b>
PnL: <b>{pnl_dollar}$ | {pnl_percent}%</b>
--- ⌁ ---
Symbol: <b>#{symbol}</b>
Interval: {interval}
--- ⌁ ---
Entry: {entry_price}
Exit: {exit_price}
"""
    send_telegram_message(msg)


# ==============================
# ⚙️ EXIT EXECUTION WRAPPER
# ==============================
def perform_exit(symbol, interval, reason="Auto Exit"):
    """Trigger exit in Binance, cleanup & log with Telegram"""
    from app import execute_market_exit  # local import

    key = f"{symbol}_{interval.lower()}"

    with trades_lock:
        trade = trades.get(key)
        if not trade or trade.get("closed"):
            return
        side = trade["side"]

    if EXIT_MARKET_DELAY_ENABLED:
        print(f"⏳ Exit delay enabled → waiting {EXIT_MARKET_DELAY}s for {symbol}")
        time.sleep(EXIT_MARKET_DELAY)

    print(f"[EXIT] Executing {reason} for {symbol} ({interval})...")
    execute_market_exit(symbol, side)

    # Fetch Binance confirmed data & send Telegram
    log_trade_exit(symbol, trade["order_id"], reason, interval)

    # Residual safety
    with trades_lock:
        if key in trades:
            trades[key]["closed"] = True


# ==============================
# ⏱ INTERVAL → SECONDS
# ==============================
def interval_to_seconds(interval: str) -> int:
    mapping = {
        "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
        "1h": 3600, "2h": 7200, "4h": 14400, "1d": 86400
    }
    return mapping.get(interval.lower(), 60)


# ==============================
# 📉 AUTO 2-BAR EXIT
# ==============================
def monitor_2bar_exit():
    while True:
        try:
            with trades_lock:
                active = [t for t in trades.values() if not t.get("closed")]

            for trade in active:
                symbol = trade["symbol"]
                interval = trade["interval"]
                elapsed = time.time() - trade["entry_time"]
                if elapsed < 2 * interval_to_seconds(interval):
                    continue

                # fetch live price
                r = requests.get(f"{BASE_URL}/fapi/v1/ticker/price?symbol={symbol}")
                if r.status_code != 200:
                    continue
                current_price = float(r.json()["price"])
                entry_price = trade["entry_price"]
                side = trade["side"].upper()
                pnl_percent = ((current_price - entry_price) / entry_price) * 100 * LEVERAGE if side == "BUY" else (
                    (entry_price - current_price) / entry_price) * 100 * LEVERAGE

                if pnl_percent < 0:
                    print(f"[2-Bar Exit] {symbol} {interval} → loss {pnl_percent:.2f}% Exiting...")
                    threading.Thread(
                        target=lambda: perform_exit(symbol, interval, reason="2-Bar Loss Exit"),
                        daemon=True
                    ).start()
        except Exception as e:
            print("⚠️ 2-Bar Monitor Error:", e)

        time.sleep(30)


threading.Thread(target=monitor_2bar_exit, daemon=True).start()


# ==============================
# 📅 DAILY SUMMARY
# ==============================
def send_daily_summary():
    while True:
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5.5)))  # IST
        next_run = now.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)
        time.sleep((next_run - now).total_seconds())

        with trades_lock:
            closed = [t for t in trades.values() if t.get("closed")]
            total = len(trades)
            profitable = sum(1 for t in closed if t["pnl"] > 0)
            lost = sum(1 for t in closed if t["pnl"] < 0)
            open_trades = sum(1 for t in trades.values() if not t["closed"])
            net_pnl = round(sum(t["pnl_percent"] for t in closed), 2)

            details = "\n".join(
                f"#{t['symbol']} {t['side']} {'✅' if t['pnl'] > 0 else '⛔️'} "
                f"| Entry: {t['entry_price']} | Exit: {t['exit_price']} | "
                f"PnL%: {t['pnl_percent']} | PnL$: {t['pnl']}" for t in closed
            )

            msg = f"""{details}
👇🏻 <b>Daily Signals Summary</b>
➕ Total: {total}
✔️ Profitable: {profitable}
✖️ Lost: {lost}
◼️ Open: {open_trades}
💰 Net PnL %: {net_pnl}%"""

            send_telegram_message(msg)
            trades.clear()


threading.Thread(target=send_daily_summary, daemon=True).start()
