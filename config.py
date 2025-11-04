#config.py

import os

# ==============================
# 🔹 BINANCE CONFIGURATION
# ==============================
USE_TESTNET = os.getenv("USE_TESTNET", "True") == "True"

TESTNET_API_KEY    = os.getenv("TESTNET_API_KEY")
TESTNET_SECRET_KEY = os.getenv("TESTNET_SECRET_KEY")
LIVE_API_KEY       = os.getenv("LIVE_API_KEY")
LIVE_SECRET_KEY    = os.getenv("LIVE_SECRET_KEY")

TESTNET_BASE_URL   = "https://testnet.binancefuture.com"
LIVE_BASE_URL      = "https://fapi.binance.com"

if USE_TESTNET:
    BINANCE_API_KEY    = TESTNET_API_KEY
    BINANCE_SECRET_KEY = TESTNET_SECRET_KEY
    BASE_URL           = TESTNET_BASE_URL
else:
    BINANCE_API_KEY    = LIVE_API_KEY
    BINANCE_SECRET_KEY = LIVE_SECRET_KEY
    BASE_URL           = LIVE_BASE_URL

# ==============================
# 🔹 TRADING PARAMETERS
# ==============================
TRADE_AMOUNT        = float(os.getenv("TRADE_AMOUNT", "50"))   # USD per trade
LEVERAGE            = int(os.getenv("LEVERAGE", "20"))         # Leverage multiplier
MARGIN_TYPE         = os.getenv("MARGIN_TYPE", "ISOLATED")     # CROSS or ISOLATED
MAX_ACTIVE_TRADES   = int(os.getenv("MAX_ACTIVE_TRADES", "5")) # Limit active trades
OPPOSITE_CLOSE_DELAY = int(os.getenv("OPPOSITE_CLOSE_DELAY", "3")) # Delay between opposite close & new entry

# ==============================
# 🔹 EXIT ORDER PARAMETERS
# ==============================
EXIT_MARKET_DELAY       = int(os.getenv("EXIT_MARKET_DELAY", "10"))  # Delay before market exit
USE_BAR_HIGH_LOW_FOR_EXIT = os.getenv("USE_BAR_HIGH_LOW_FOR_EXIT", "True") == "True"
EXIT_LIMIT_TIMEOUT      = int(os.getenv("EXIT_LIMIT_TIMEOUT", "5"))   # Timeout for limit exit fill

# ==============================
# 🔹 TELEGRAM & SUMMARY CONFIG
# ==============================
TELEGRAM_BOT_TOKEN = "8282710007:AAFbcLUwHRrMrBJ5VacJQQFM27qxdCplwO4"
TELEGRAM_CHAT_ID   = "-1003281678423"

# Daily summary time in IST (HH:MM format)
DAILY_SUMMARY_TIME_IST = os.getenv("DAILY_SUMMARY_TIME_IST", "00:00")

# ==============================
# 🔹 LOGGING
# ==============================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")  # INFO, DEBUG, ERROR

# ==============================
# 🔹 CONFIG SUMMARY
# ==============================
print(f"""
📘 CONFIGURATION LOADED
------------------------------
Environment:           {"TESTNET" if USE_TESTNET else "LIVE"}
Leverage:              {LEVERAGE}x ({MARGIN_TYPE})
Trade Amount:          ${TRADE_AMOUNT}
Exit Market Delay:     {EXIT_MARKET_DELAY}s
Exit Limit→Market:     {EXIT_LIMIT_TIMEOUT}s
Use Bar High/Low:      {USE_BAR_HIGH_LOW_FOR_EXIT}
Opposite Close Delay:  {OPPOSITE_CLOSE_DELAY}s
Max Active Trades:     {MAX_ACTIVE_TRADES}
------------------------------
""")
