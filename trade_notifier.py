# ==============================
# trade_notifier.py (FINAL MINIMAL MODE)
# ==============================

import threading
import time
import requests
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TRADE_AMOUNT, LEVERAGE

# ==============================
# 🧾 SHARED STORAGE
# ==============================
trades = {}
trades_lock = threading.Lock()


# ==============================
# 📢 TELEGRAM HELPER
# ==============================
def send_telegram_message(message: str):
    """Send Telegram message via bot token"""
    try:
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            print("⚠️ Missing Telegram credentials, skipping message.")
            return
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
        response = requests.post(url, data=payload, timeout=10)
        if response.status_code != 200:
            print(f"⚠️ Telegram error: {response.text}")
    except Exception as e:
        print(f"❌ Telegram exception: {e}")


# ==============================
# 🟩 LOG TRADE ENTRY
# ==============================
def log_trade_exit(symbol: str, order_id: str = None, filled_price: float = 0.0, reason="Normal Exit", interval: str = "1m"):
    """Store entry data + send Telegram alert"""
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
Side: <b>{side.upper()}</b>
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
    print(f"[ENTRY] {symbol} {side.upper()} @ {filled_price} ({interval})")


# ==============================
# 🟥 LOG TRADE EXIT
# ==============================
def log_trade_exit(symbol: str, filled_price: float, pnl: float, pnl_percent: float, reason="Exit", interval: str = "1m"):
    """Store exit + send Telegram alert"""
    key = f"{symbol}_{interval.lower()}"

    with trades_lock:
        trade = trades.get(key)
        if not trade or trade.get("closed"):
            return
        trade["exit_price"] = filled_price
        trade["pnl"] = pnl
        trade["pnl_percent"] = pnl_percent
        trade["closed"] = True

    header = "✅ Profit Achieved!" if pnl >= 0 else "⛔️ Ended in Loss!"

    msg = f"""{header}
Reason: <b>{reason}</b>
PnL: <b>{pnl}$</b> | <b>{pnl_percent}%</b>
--- ⌁ ---
Symbol: <b>#{symbol}</b>
Interval: <b>{interval}</b>
--- ⌁ ---
Entry: {trade.get('entry_price', '?')}
Exit: {filled_price}
"""
    send_telegram_message(msg)
    print(f"[EXIT] {symbol} closed @ {filled_price} | PnL: {pnl}$ ({pnl_percent}%) | Reason: {reason}")


# ==============================
# ⏱️ INTERVAL TO SECONDS
# ==============================
def interval_to_seconds(interval: str) -> int:
    mapping = {
        "1m": 60, "3m": 180, "5m": 300, "15m": 900,
        "30m": 1800, "1h": 3600, "2h": 7200,
        "4h": 14400, "1d": 86400
    }
    return mapping.get(interval.lower(), 60)
