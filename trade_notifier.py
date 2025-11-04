# trade_notifier.py
import requests
import threading
import time
import datetime
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TRADE_AMOUNT, LEVERAGE, BASE_URL, BINANCE_API_KEY

# =======================
# 🧾 STORAGE
# =======================
trades = {}  # {symbol: {...}}
notified_orders = set()

# =======================
# 📢 TELEGRAM HELPER
# =======================
def send_telegram_message(message: str):
    try:
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            print("⚠️ Missing Telegram credentials. Skipping message.")
            return
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
        response = requests.post(url, data=payload, timeout=10)
        if response.status_code != 200:
            print("❌ Telegram Error:", response.status_code, response.text)
    except Exception as e:
        print("❌ Telegram Exception:", e)


# =======================
# 🟩 TRADE ENTRY
# =======================
def log_trade_entry(symbol: str, side: str, order_id: str, filled_price: float, interval: str = "1m"):
    """Record and notify trade entry when order is FILLED"""
    if order_id in notified_orders:
        return
    notified_orders.add(order_id)

    trades[symbol] = {
        "side": side,
        "entry_price": filled_price,
        "order_id": order_id,
        "interval": interval.lower(),
        "closed": False,
        "exit_price": None,
        "pnl": 0,
        "pnl_percent": 0,
        "entry_time": time.time(),
    }

    arrow = "⬆️" if side.upper() == "BUY" else "⬇️"
    trade_type = "Long Trade" if side.upper() == "BUY" else "Short Trade"

    message = f"""{arrow} <b>{trade_type}</b>
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
    send_telegram_message(message)


# =======================
# 🟥 TRADE EXIT
# =======================
def log_trade_exit(symbol: str, order_id: str, filled_price: float, reason="Normal Exit"):
    """Record and notify trade exit"""
    if symbol not in trades:
        trades[symbol] = {
            "side": "UNKNOWN",
            "entry_price": filled_price,
            "closed": True,
            "exit_price": filled_price,
            "pnl": 0,
            "pnl_percent": 0,
        }

    trade = trades[symbol]
    if trade["closed"]:
        return  # avoid duplicate exit

    trade["exit_price"] = filled_price
    trade["closed"] = True

    entry_price = trade["entry_price"]
    side = trade["side"].upper()
    qty = TRADE_AMOUNT

    if side == "BUY":
        pnl = (filled_price - entry_price) * qty * LEVERAGE / entry_price
        pnl_percent = ((filled_price - entry_price) / entry_price) * 100 * LEVERAGE
    elif side == "SELL":
        pnl = (entry_price - filled_price) * qty * LEVERAGE / entry_price
        pnl_percent = ((entry_price - filled_price) / entry_price) * 100 * LEVERAGE
    else:
        pnl = pnl_percent = 0

    trade["pnl"] = round(pnl, 2)
    trade["pnl_percent"] = round(pnl_percent, 2)

    header = "Profit Achieved! ✅" if pnl >= 0 else "Ended in Loss! ⛔️"

    message = f"""{header}
Reason: <b>{reason}</b>
PnL: {trade['pnl']}$ | {trade['pnl_percent']}%
--- ⌁ ---
Symbol: <b>#{symbol}</b>
Interval: {trade.get('interval', '-')}
--- ⌁ ---
Entry: {trade['entry_price']}
Exit: {trade['exit_price']}
"""
    send_telegram_message(message)


# =======================
# 🕒 Helper: Convert interval → seconds
# =======================
def interval_to_seconds(interval: str) -> int:
    mapping = {
        "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
        "1h": 3600, "2h": 7200, "4h": 14400, "1d": 86400
    }
    return mapping.get(interval, 60)


# =======================
# ⏱️ 2-BAR NEGATIVE CHECK
# =======================
def monitor_2bar_exit():
    """Continuously checks open trades and exits after 2 bars if PnL < 0"""
    while True:
        try:
            for symbol, trade in list(trades.items()):
                if trade.get("closed", True):
                    continue

                interval = trade.get("interval", "1m")
                elapsed = time.time() - trade["entry_time"]
                interval_sec = interval_to_seconds(interval)

                # Proceed only after 2 full bars
                if elapsed >= (2 * interval_sec):
                    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
                    resp = requests.get(f"{BASE_URL}/fapi/v1/ticker/price?symbol={symbol}", headers=headers)
                    if resp.status_code != 200:
                        continue

                    current_price = float(resp.json()["price"])
                    entry_price = trade["entry_price"]
                    side = trade["side"].upper()

                    if side == "BUY":
                        pnl_percent = ((current_price - entry_price) / entry_price) * 100 * LEVERAGE
                    else:
                        pnl_percent = ((entry_price - current_price) / entry_price) * 100 * LEVERAGE

                    # If PnL negative → mark as closed with reason
                    if pnl_percent < 0:
                        log_trade_exit(symbol, trade["order_id"], current_price, reason="2 bar close exit")
                        print(f"[2-Bar Auto Exit] {symbol} closed after 2 bars with {pnl_percent:.2f}% loss")
        except Exception as e:
            print("⚠️ 2-Bar Monitor Error:", e)

        time.sleep(30)  # check every 30 seconds


# Start background monitor thread
threading.Thread(target=monitor_2bar_exit, daemon=True).start()


# =======================
# 📅 DAILY SUMMARY
# =======================
def send_daily_summary():
    """Send daily trading performance summary"""
    while True:
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5.5)))  # IST
        next_run = now.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)
        time.sleep((next_run - now).total_seconds())

        closed_trades = [t for t in trades.values() if t["closed"]]
        total_signals = len(trades)
        profitable = sum(1 for t in closed_trades if t["pnl"] > 0)
        lost = sum(1 for t in closed_trades if t["pnl"] < 0)
        open_trades = sum(1 for t in trades.values() if not t["closed"])
        net_pnl_percent = round(sum(t["pnl_percent"] for t in closed_trades), 2)

        detailed_msg = ""
        for symbol, t in trades.items():
            if t["closed"]:
                icon = "✅" if t["pnl"] > 0 else "⛔️"
                detailed_msg += f"#{symbol} {t['side']} {icon} | Entry: {t['entry_price']} | Exit: {t['exit_price']} | PnL%: {t['pnl_percent']} | PnL$: {t['pnl']}\n"

        summary_msg = f"""{detailed_msg}
👇🏻 <b>Signals Summary</b>
➕ Total Signals: {total_signals}
✔️ Profitable: {profitable}
✖️ Lost: {lost}
◼️ Open Trades: {open_trades}
✅ Net PnL %: {net_pnl_percent}%"""
        send_telegram_message(summary_msg)
        trades.clear()


# Start daily summary in background
threading.Thread(target=send_daily_summary, daemon=True).start()
