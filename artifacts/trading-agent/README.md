# AI Trading Agent — Deriv Forex

A complete modular AI trading agent for automated forex trading on Deriv's platform. Built with 8 interconnected layers for intelligent, risk-controlled trading.

---

## Architecture

| Layer | File | Purpose |
|-------|------|---------|
| 1 | `market_data.py` | WebSocket market data, OHLCV, indicators |
| 2 | `strategy.py` | Trend-following signal engine |
| 3 | `ai_decision.py` | Confidence scoring, regime & volatility filters |
| 4 | `risk_management.py` | Capital protection, position sizing, limits |
| 5 | `trade_execution.py` | Deriv API order placement |
| 6 | `performance.py` | Self-adapting performance tracker |
| 7 | `api_server.py` | Flask REST API & n8n webhook |
| 8 | `notifications.py` | Console logging & Telegram |

---

## Quick Start

### 1. Install Python 3.10+

Make sure Python 3.10 or higher is installed:
```bash
python3 --version
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure the agent

Open `config.py` and check/update:
- `API_TOKEN` — your Deriv API token
- `ACCOUNT_ID` — your Deriv account ID
- `DEMO_MODE = True` — starts in demo mode (safe for testing)
- `NEWS_FILTER_ACTIVE = False` — set True before big news events

### 4. Run the agent

```bash
python main.py
```

The agent will:
- Connect to Deriv WebSocket
- Load 200 historical candles for all pairs
- Subscribe to live ticks and OHLCV data
- Start the Flask API server on port 5000
- Begin scanning for trade setups

---

## Switching to Live Account

1. Log in to Deriv and generate a **real account** API token
2. Open `config.py`
3. Update:
   ```python
   DEMO_MODE  = False
   API_TOKEN  = "YOUR_REAL_TOKEN_HERE"
   ACCOUNT_ID = "YOUR_REAL_ACCOUNT_ID"
   ```
4. Restart the agent

---

## REST API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/status` | GET | Balance, open trades, daily P&L, agent state |
| `/performance` | GET | Last 20 trade summary |
| `/pause` | POST | Pause all trading immediately |
| `/resume` | POST | Resume trading after manual pause |
| `/trade-alert` | POST | Send a signal from n8n |
| `/trades/history?n=20` | GET | Last N closed trades |
| `/health` | GET | Health check |

### Example

```bash
# Check status
curl http://localhost:5000/status

# Pause trading
curl -X POST http://localhost:5000/pause

# Resume trading
curl -X POST http://localhost:5000/resume

# Send a signal from n8n
curl -X POST http://localhost:5000/trade-alert \
  -H "Content-Type: application/json" \
  -d '{"pair": "frxEURUSD", "direction": "BUY", "note": "Strong breakout"}'
```

---

## Telegram Notifications

Telegram is built in but uses a placeholder. To activate:

1. Create a bot via [@BotFather](https://t.me/botfather) and copy the token
2. Get your chat ID from [@userinfobot](https://t.me/userinfobot)
3. Open `config.py` and set:
   ```python
   TELEGRAM_BOT_TOKEN = "your_bot_token"
   TELEGRAM_CHAT_ID   = "your_chat_id"
   TELEGRAM_ENABLED   = True
   ```

---

## Risk Rules Summary

| Rule | Value |
|------|-------|
| Max risk per trade | 1.5% of balance |
| Hard cap risk | 3% of balance |
| Capital protection floor | 20% of starting balance |
| Daily loss limit | 5% → halt for the day |
| Weekly loss limit | 10% → halt for the week |
| Consecutive loss pause | 3 losses → 2h pause + 50% size cut |
| Max simultaneous trades | 2 across all pairs |
| Max trades per pair | 1 at a time |
| Stop loss | 1.5 × ATR |
| Take profit | 2.5 × ATR (≈1.67:1 R:R) |

---

## Trading Sessions (UTC)

| Session | Hours (UTC) |
|---------|-------------|
| London | 08:00 – 17:00 |
| New York | 13:00 – 22:00 |
| **London/NY Overlap** | **13:00 – 17:00** ← highest priority |

The agent trades only during these windows and applies a 30-minute buffer at open/close.

---

## Files

```
trading-agent/
├── config.py           # All settings — edit this file
├── database.py         # SQLite operations
├── market_data.py      # Layer 1: WebSocket + indicators
├── strategy.py         # Layer 2: Signal rules
├── ai_decision.py      # Layer 3: Confidence scoring
├── risk_management.py  # Layer 4: Capital protection
├── trade_execution.py  # Layer 5: Order placement
├── performance.py      # Layer 6: Self-adaptation
├── api_server.py       # Layer 7: Flask API
├── notifications.py    # Layer 8: Logging & Telegram
├── main.py             # Entry point
├── requirements.txt    # Python dependencies
├── trading_agent.db    # SQLite database (auto-created)
└── performance_reports/ # Auto-saved performance reports
```

---

## News Filter

Before a high-impact news event (NFP, CPI, FOMC, etc.):

```python
# In config.py
NEWS_FILTER_ACTIVE = True
```

Or via the API:
```bash
curl -X POST http://localhost:5000/pause
```

---

## Important Notes

- This agent trades real money in live mode. Always test thoroughly in demo first.
- The capital floor (20% of starting balance) is a hard stop — it cannot be overridden.
- The agent is intentionally selective. Long periods of no trades mean it's waiting for genuinely good setups.
- Performance reports are saved automatically to `performance_reports/` every 20 trades.
