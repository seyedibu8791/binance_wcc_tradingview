# ==============================

# trade_notifier.py (Updated for latest app.py)

# ==============================

import requests
import threading
import time
import datetime
from config import (
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
TRADE_AMOUNT, LEVERAGE, BASE_URL,
BINANCE_API_KEY, BINANCE_SECRET_KEY,
EXIT_MARKET_DELAY_ENABLED, EXIT_MARKET_DELAY
)

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
url = f"[https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage](https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage)"
payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
r = requests.post(url, data=payload, timeout=10)
if r.status_code != 200:
print("❌ Telegram error:", r.text)
except Exception as e:
print("❌ Telegram send error:", e)

# ==============================

# 🟩 ENTRY LOGGER

# ==============================

def log_trade_entry(symbol, side, order_id, filled_price, interval):
if order_id in notified_orders:
return
notified_orders.add(order_id)

```
with trades_lock:  
    trades[symbol] = {  
        "symbol": symbol,  
        "side": side.upper(),  
        "entry_price": filled_price,  
        "interval": interval.lower(),  
        "order_id": order_id,  
        "entry_time": time.time(),  
        "closed": False  
    }  

arrow = "⬆️" if side.upper() == "BUY" else "⬇️"  
trade_type = "Long Trade" if side.upper() == "BUY" else "Short Trade"  

msg = f"""{arrow} <b>{trade_type}</b>  
```

Symbol: <b>#{symbol}</b>
Side: <b>{side.upper()}</b>
Interval: <b>{interval}</b>
--- ⌁ ---
Leverage: {LEVERAGE}x
Trade Amount: {TRADE_AMOUNT}$
--- ⌁ ---
Entry Price: <b>{filled_price}</b>
--- ⌁ ---
🕐 Wait for Exit Signal.."""
send_telegram_message(msg)

# ==============================

# 🟥 EXIT LOGGER

# ==============================

def log_trade_exit(symbol, order_id, filled_price, reason, interval):
with trades_lock:
trade = trades.get(symbol)
if not trade or trade.get("closed"):
return
side = trade["side"]
entry_price = trade["entry_price"]

```
    pnl_dollar = filled_price - entry_price  
    if side.upper() == "SELL":  
        pnl_dollar = entry_price - filled_price  
    pnl_dollar *= LEVERAGE  
    pnl_percent = (pnl_dollar / TRADE_AMOUNT) * 100 if TRADE_AMOUNT else 0  

    trade["exit_price"] = filled_price  
    trade["pnl"] = pnl_dollar  
    trade["pnl_percent"] = round(pnl_percent, 2)  
    trade["closed"] = True  

status = "✅ Ended in Profit!" if pnl_dollar > 0 else "⛔️ Ended in Loss!"  
msg = f"""{status}  
```

Reason: <b>{reason}</b>
PnL: <b>{pnl_dollar:.2f}$ | {pnl_percent:.2f}%</b>
--- ⌁ ---
Symbol: <b>#{symbol}</b>
Interval: {interval}
--- ⌁ ---
Entry: {entry_price}
Exit: {filled_price}"""
send_telegram_message(msg)

# ==============================

# ⚙️ PERFORM EXIT

# ==============================

def perform_exit(symbol, interval, reason="Auto Exit"):
from app import execute_market_exit, wait_and_finalize_exit  # lazy import

```
with trades_lock:  
    trade = trades.get(symbol)  
    if not trade or trade.get("closed"):  
        return  
    side = trade["side"]  

if EXIT_MARKET_DELAY_ENABLED:  
    print(f"⏳ Exit delay {EXIT_MARKET_DELAY}s for {symbol}")  
    time.sleep(EXIT_MARKET_DELAY)  

print(f"[EXIT] {symbol} ({interval}) reason={reason}")  
response = execute_market_exit(symbol, side)  
order_id = response.get("orderId") if response else None  
if order_id:  
    threading.Thread(target=wait_and_finalize_exit, args=(symbol, order_id, reason), daemon=True).start()  
```

# ==============================

# ⏱ INTERVAL SECONDS

# ==============================

def interval_to_seconds(interval: str) -> int:
mapping = {
"1m": 60, "3m": 180, "5m": 300, "15m": 900,
"30m": 1800, "1h": 3600, "2h": 7200, "4h": 14400, "1d": 86400
}
return mapping.get(interval.lower(), 60)

# ==============================

# 📅 DAILY SUMMARY

# ==============================

def send_daily_summary():
while True:
now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5.5)))
next_run = now.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)
time.sleep((next_run - now).total_seconds())

```
    with trades_lock:  
        closed = [t for t in trades.values() if t.get("closed")]  
        total = len(trades)  
        profitable = sum(1 for t in closed if t["pnl"] > 0)  
        lost = sum(1 for t in closed if t["pnl"] < 0)  
        open_trades = sum(1 for t in trades.values() if not t["closed"])  
        net_pnl = round(sum(t["pnl_percent"] for t in closed), 2)  

        details = "\n".join(  
            f"#{t['symbol']} {t['side']} {'✅' if t['pnl'] > 0 else '⛔️'} "  
            f"| Entry: {t['entry_price']} | Exit: {t.get('exit_price', '-')} "  
            f"| PnL%: {t.get('pnl_percent', 0)} | PnL$: {t.get('pnl', 0)}"  
            for t in closed  
        )  

        msg = f"""{details}  
```

👇🏻 <b>Daily Signals Summary</b>
➕ Total: {total}
✔️ Profitable: {profitable}
✖️ Lost: {lost}
◼️ Open: {open_trades}
💰 Net PnL %: {net_pnl}%"""
send_telegram_message(msg)
trades.clear()

threading.Thread(target=send_daily_summary, daemon=True).start()
