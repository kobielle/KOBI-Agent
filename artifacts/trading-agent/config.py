"""
config.py — Central configuration for the AI Trading Agent.
Change any setting here without touching the main code.
"""

import os

# ─── Deriv API ───────────────────────────────────────────────────────────────
DERIV_APP_ID = "1089"
DERIV_WS_URL = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"
API_TOKEN  = os.environ.get("DERIV_API_TOKEN",  "iKtGIhDi6Vb9LA3")   # Demo token fallback
ACCOUNT_ID = os.environ.get("DERIV_ACCOUNT_ID", "VRW1678235")

# Set DEMO_MODE env var to "false" / "0" to switch to a real account
DEMO_MODE = os.environ.get("DEMO_MODE", "True").strip().lower() not in ("false", "0", "no")

# ─── Trading Pairs ───────────────────────────────────────────────────────────
FOREX_PAIRS = ["frxEURUSD", "frxGBPUSD", "frxUSDJPY"]

# ─── Technical Indicator Parameters ──────────────────────────────────────────
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
EMA_SHORT = 20          # Fast EMA
EMA_LONG = 50           # Slow EMA
BB_PERIOD = 20          # Bollinger Band period
ATR_PERIOD = 14         # ATR period for volatility

# ─── Timeframes (seconds) ────────────────────────────────────────────────────
TIMEFRAME_5M = 300      # 5-minute candles
TIMEFRAME_15M = 900     # 15-minute candles
HISTORICAL_CANDLES = 200  # How many historical candles to fetch

# ─── AI Decision Layer ───────────────────────────────────────────────────────
CONFIDENCE_THRESHOLD = 55   # Minimum score (0-100) required to enter a trade
ATR_HIGH_MULT = 2.0         # ATR multiplier above which volatility is "too high"
ATR_LOW_MULT = 0.3          # ATR multiplier below which volatility is "too low"

# ─── Risk Management ─────────────────────────────────────────────────────────
RISK_PER_TRADE = 0.015          # 1.5% of account balance per trade
MAX_RISK_PER_TRADE = 0.03       # Hard cap at 3% even after good streaks
CAPITAL_PROTECTION_FLOOR = 0.20 # Stop all trading if balance drops to 20% of start
DAILY_LOSS_LIMIT = 0.05         # 5% daily loss — stop trading for the rest of the day
WEEKLY_LOSS_LIMIT = 0.10        # 10% weekly loss — stop for the week
MAX_SIMULTANEOUS_TRADES = 2     # Max open trades across all pairs
MAX_TRADES_PER_PAIR = 1         # Max open trades on a single pair

# Position sizing adjustment rules
WIN_STREAK_THRESHOLD = 5        # Wins needed to increase position size
WIN_STREAK_INCREASE = 0.10      # Increase by 10% of current size
LOSS_REDUCTION = 0.30           # Cut position 30% after any single loss
RECOVERY_WINS_NEEDED = 3        # Consecutive wins needed to stop reducing
CONSECUTIVE_LOSS_LIMIT = 3      # Pause trading after this many consecutive losses
CONSECUTIVE_LOSS_PAUSE_HOURS = 2  # Pause duration in hours
CONSECUTIVE_LOSS_SIZE_REDUCTION = 0.50  # Cut position by 50% on pause

# ─── Stop Loss / Take Profit ─────────────────────────────────────────────────
SL_ATR_MULTIPLIER = 1.5   # Stop loss = 1.5 × ATR from entry
TP_ATR_MULTIPLIER = 2.5   # Take profit = 2.5 × ATR from entry (≈1.67 R:R)

# ─── Performance & Learning ──────────────────────────────────────────────────
PERFORMANCE_SAMPLE_SIZE = 20   # Analyse every 20 trades
MIN_WIN_RATE = 0.45            # Below 45% → cut size by 50%
HIGH_WIN_RATE = 0.65           # Above 65% → increase size 10%

# ─── Trading Sessions (UTC hours) ────────────────────────────────────────────
ASIAN_OPEN_UTC = 0      # 00:00 UTC (Tokyo/Asian session open)
ASIAN_CLOSE_UTC = 9     # 09:00 UTC (Asian session close)
LONDON_OPEN_UTC = 8     # 08:00 UTC
LONDON_CLOSE_UTC = 17   # 17:00 UTC
NY_OPEN_UTC = 13        # 13:00 UTC
NY_CLOSE_UTC = 22       # 22:00 UTC
SESSION_BUFFER_MINUTES = 30  # Buffer at open/close of each session

# ─── News Filter ─────────────────────────────────────────────────────────────
# Set to True manually before high-impact news events to pause trading
NEWS_FILTER_ACTIVE = False

# ─── Database ────────────────────────────────────────────────────────────────
DB_PATH = "trading_agent.db"
PERFORMANCE_LOG_PATH = "performance_reports/"

# ─── Flask API Server ────────────────────────────────────────────────────────
FLASK_HOST = "0.0.0.0"
FLASK_PORT = 5000

# ─── Telegram Notifications (placeholder) ────────────────────────────────────
TELEGRAM_BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"   # Replace to activate
TELEGRAM_CHAT_ID = "YOUR_CHAT_ID_HERE"        # Replace to activate
TELEGRAM_ENABLED = False  # Set True after filling in above credentials

# ─── Deriv Multiplier Settings ───────────────────────────────────────────────
MULTIPLIER_VALUE = 100     # Leverage multiplier — Deriv forex accepts: 100,200,300,500,800
CONTRACT_DURATION = None   # None = open-ended multiplier contract
