# ==============================
# trade_notifier.py (FINAL INTEGRATED + FIXED finalize_trade)
# ==============================

import requests
import threading
import time
import datetime
import hmac
import hashlib
from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    TRADE_AMOUNT, LEVERAGE, BASE_URL, BINANCE_API_KEY, BINANCE_SECRET_KEY,
    USE_BAR_HIGH_LOW_FOR_EXIT, BAR_EXIT_TIMEOUT_SEC,
    EXIT_MARKET_DELAY_ENABLED, EXIT_MARKET_DELAY,
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
    """Send a Telegram message using bot token and chat ID"""
    try:
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            print("⚠️ Missing Telegram credentials. Skipping message.")
            return
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
        resp = requests.post(url, data=payload, timeout=10)
        if resp.status_code != 200:
            print("❌ Telegram Error:", resp.status_code, resp.text)
    except Exception as e:
        print("❌ Telegram Exception:", e)


# ==============================
# 🟩 TRADE ENTRY
# ==============================
def log_trade_entry(symbol: str, side: str, order_id: str, filled_price: float, interval: str = "1m"):
    """Store and announce a new trade entry"""
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
            "exit_price": None,
            "pnl": 0,
            "pnl_percent": 0,
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
# 🟥 TRADE EXIT
# ==============================
def log_trade_exit(symbol: str, order_id: str, filled_price: float, reason="Normal Exit", interval: str = "1m"):
    """Log and announce a trade exit"""
    key = f"{symbol}_{interval.lower()}"

    with trades_lock:
        trade = trades.get(key)
        if not trade or trade.get("closed"):
            return

        trade["exit_price"] = filled_price
        trade["closed"] = True

        entry_price = trade["entry_price"]
        side = trade["side"].upper()
        qty = TRADE_AMOUNT

        if side == "BUY":
            pnl = (filled_price - entry_price) * qty * LEVERAGE / entry_price
            pnl_percent = ((filled_price - entry_price) / entry_price) * 100 * LEVERAGE
        else:
            pnl = (entry_price - filled_price) * qty * LEVERAGE / entry_price
            pnl_percent = ((entry_price - filled_price) / entry_price) * 100 * LEVERAGE

        trade["pnl"] = round(pnl, 2)
        trade["pnl_percent"] = round(pnl_percent, 2)

    header = "✅ Profit Achieved!" if pnl >= 0 else "⛔️ Ended in Loss!"

    msg = f"""{header}
Reason: <b>{reason}</b>
PnL: {trade['pnl']}$ | {trade['pnl_percent']}%
--- ⌁ ---
Symbol: <b>#{symbol}</b>
Interval: {interval}
--- ⌁ ---
Entry: {trade['entry_price']}
Exit: {trade['exit_price']}
"""
    send_telegram_message(msg)


# ==============================
# ⚖️ FINALIZE TRADE (with timestamp + signature fix)
# ==============================
def finalize_trade(symbol, reason):
    """Fetch actual trade data from Binance and send unified Telegram exit."""
    try:
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
        filled_price = float(last_trade["price"])
        realized_pnl = float(last_trade.get("realizedPnl", 0.0))
        qty = float(last_trade["qty"])

        with trades_lock:
            trade = trades.get(symbol, {})
            entry_price = trade.get("entry_price", filled_price)
            interval = trade.get("interval", "1m")
            order_id = trade.get("order_id", f"auto_{int(time.time())}")

        pnl_percent = (realized_pnl / (qty * entry_price)) * 100 if entry_price > 0 else 0

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


# ==============================
# ⏱️ INTERVAL → SECONDS
# ==============================
def interval_to_seconds(interval: str) -> int:
    mapping = {
        "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
        "1h": 3600, "2h": 7200, "4h": 14400, "1d": 86400
    }
    return mapping.get(interval.lower(), 60)


# ==============================
# ⚙️ EXIT EXECUTION HELPER
# ==============================
def perform_exit(symbol, interval, reason="Auto Exit"):
    """Handles delayed or immediate market exit and residual cleanup"""
    from app import execute_market_exit  # avoid circular import

    key = f"{symbol}_{interval.lower()}"
    with trades_lock:
        trade = trades.get(key)
        if not trade or trade.get("closed"):
            return
        side = trade["side"]

    if EXIT_MARKET_DELAY_ENABLED:
        print(f"⏳ Exit delay enabled → waiting {EXIT_MARKET_DELAY}s for {symbol}")
        time.sleep(EXIT_MARKET_DELAY)

    execute_market_exit(symbol, side)
    print(f"[EXIT] Market exit executed for {symbol} ({interval}) → {reason}")

    with trades_lock:
        if key in trades:
            trades[key]["closed"] = True


# ==============================
# 📉 AUTO 2-BAR NEGATIVE EXIT
# ==============================
def monitor_2bar_exit():
    """Automatically checks and exits positions after 2 bars if loss"""
    while True:
        try:
            with trades_lock:
                active_trades = [t for t in trades.values() if not t.get("closed")]

            for trade in active_trades:
                symbol = trade["symbol"]
                interval = trade.get("interval", "1m")
                elapsed = time.time() - trade["entry_time"]
                interval_sec = interval_to_seconds(interval)

                if elapsed < 2 * interval_sec:
                    continue

                resp = requests.get(f"{BASE_URL}/fapi/v1/ticker/price?symbol={symbol}")
                if resp.status_code != 200:
                    continue

                current_price = float(resp.json()["price"])
                entry_price = trade["entry_price"]
                side = trade["side"].upper()

                pnl_percent = ((current_price - entry_price) / entry_price) * 100 * LEVERAGE if side == "BUY" \
                    else ((entry_price - current_price) / entry_price) * 100 * LEVERAGE

                if pnl_percent < 0:
                    print(f"[2-Bar Exit] {symbol} {interval} → loss {pnl_percent:.2f}%. Exiting...")

                    if USE_BAR_HIGH_LOW_FOR_EXIT:
                        print(f"📊 High/Low limit exit → fallback to market in {BAR_EXIT_TIMEOUT_SEC}s.")
                        threading.Thread(
                            target=lambda: perform_exit(symbol, interval, reason="2-bar close (bar-based)"),
                            daemon=True
                        ).start()
                        time.sleep(BAR_EXIT_TIMEOUT_SEC)
                    else:
                        threading.Thread(
                            target=lambda: perform_exit(symbol, interval, reason="2-bar close exit"),
                            daemon=True
                        ).start()

                    log_trade_exit(symbol, trade["order_id"], current_price,
                                   reason=f"2-bar close exit ({interval})", interval=interval)
        except Exception as e:
            print("⚠️ 2-Bar Monitor Error:", e)

        time.sleep(30)


threading.Thread(target=monitor_2bar_exit, daemon=True).start()


# ==============================
# 📅 DAILY SUMMARY
# ==============================
def send_daily_summary():
    """Sends daily Telegram summary of trades"""
    while True:
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5.5)))
        next_run = now.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)
        time.sleep((next_run - now).total_seconds())

        with trades_lock:
            closed_trades = [t for t in trades.values() if t.get("closed")]
            total = len(trades)
            profitable = sum(1 for t in closed_trades if t["pnl"] > 0)
            lost = sum(1 for t in closed_trades if t["pnl"] < 0)
            open_trades = sum(1 for t in trades.values() if not t["closed"])
            net_pnl = round(sum(t["pnl_percent"] for t in closed_trades), 2)

            summary_lines = [
                f"#{t['symbol']} {t['side']} {'✅' if t['pnl'] > 0 else '⛔️'} "
                f"| Entry: {t['entry_price']} | Exit: {t['exit_price']} | "
                f"PnL%: {t['pnl_percent']} | PnL$: {t['pnl']}"
                for t in closed_trades
            ]

            msg = f"""{'\n'.join(summary_lines)}
👇🏻 <b>Daily Signals Summary</b>
➕ Total Signals: {total}
✔️ Profitable: {profitable}
✖️ Lost: {lost}
◼️ Open Trades: {open_trades}
✅ Net PnL %: {net_pnl}%"""

            send_telegram_message(msg)
            trades.clear()


threading.Thread(target=send_daily_summary, daemon=True).start()
