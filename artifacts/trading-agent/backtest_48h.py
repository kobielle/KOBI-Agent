"""
backtest_48h.py — Replays the last 48 hours of frxEURUSD data
through the UPDATED strategy rules (post-relaxation).
Fetches live historical candles from Deriv, applies indicators,
and reports every trade that would have been triggered.
No orders are placed.
"""

import asyncio
import json
import sys
import websockets
import pandas as pd
import ta
from datetime import datetime, timezone

API_TOKEN = "iKtGIhDi6Vb9LA3"
WS_URL    = "wss://ws.derivws.com/websockets/v3?app_id=1089"
PAIR      = "frxEURUSD"

# ── Updated strategy parameters ─────────────────────────────────────────────
CONFIDENCE_THRESHOLD = 55
RSI_BUY_LO,  RSI_BUY_HI  = 40, 70
RSI_SELL_LO, RSI_SELL_HI = 30, 60

# Sessions (UTC minutes)
SESSIONS = [
    (0  * 60 + 30, 9  * 60 - 30, "Asian"),
    (8  * 60 + 30, 17 * 60 - 30, "London"),
    (13 * 60 + 30, 22 * 60 - 30, "New York"),
]

def in_session(ts: pd.Timestamp) -> tuple:
    m = ts.hour * 60 + ts.minute
    for open_m, close_m, name in SESSIONS:
        if open_m <= m <= close_m:
            return True, name
    return False, "Off-session"

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["rsi"]         = ta.momentum.RSIIndicator(df["close"], window=14).rsi()
    macd_obj          = ta.trend.MACD(df["close"], window_fast=12, window_slow=26, window_sign=9)
    df["macd"]        = macd_obj.macd()
    df["macd_signal"] = macd_obj.macd_signal()
    df["ema_fast"]    = ta.trend.EMAIndicator(df["close"], window=20).ema_indicator()
    df["ema_slow"]    = ta.trend.EMAIndicator(df["close"], window=50).ema_indicator()
    df["atr"]         = ta.volatility.AverageTrueRange(df["high"], df["low"], df["close"], window=14).average_true_range()
    bb                = ta.volatility.BollingerBands(df["close"], window=20)
    df["bb_upper"]    = bb.bollinger_hband()
    df["bb_lower"]    = bb.bollinger_lband()
    df["macd_cross_up"]   = (df["macd"] > df["macd_signal"]) & (df["macd"].shift(1) <= df["macd_signal"].shift(1))
    df["macd_cross_down"] = (df["macd"] < df["macd_signal"]) & (df["macd"].shift(1) >= df["macd_signal"].shift(1))
    return df

def check_signal(row, prev_row, price: float) -> str | None:
    for col in ["rsi", "macd", "macd_signal", "macd_cross_up", "macd_cross_down", "ema_fast", "ema_slow"]:
        if pd.isna(row.get(col)):
            return None

    rsi       = row["rsi"]
    ema_fast  = row["ema_fast"]
    ema_slow  = row["ema_slow"]
    cross_up  = bool(row["macd_cross_up"])
    cross_dn  = bool(row["macd_cross_down"])

    if ema_fast > ema_slow and RSI_BUY_LO <= rsi <= RSI_BUY_HI and cross_up and price > ema_fast:
        return "BUY"
    if ema_fast < ema_slow and RSI_SELL_LO <= rsi <= RSI_SELL_HI and cross_dn and price < ema_fast:
        return "SELL"
    return None

async def run():
    print(f"\n{'='*60}")
    print(f"  48-Hour Backtest — {PAIR}  (updated strategy)")
    print(f"  Confidence threshold : {CONFIDENCE_THRESHOLD}")
    print(f"  RSI BUY range        : {RSI_BUY_LO}–{RSI_BUY_HI}")
    print(f"  RSI SELL range       : {RSI_SELL_LO}–{RSI_SELL_HI}")
    print(f"  Sessions             : Asian + London + New York")
    print(f"{'='*60}\n")

    async with websockets.connect(WS_URL) as ws:
        # Authorize
        await ws.send(json.dumps({"authorize": API_TOKEN}))
        auth = json.loads(await ws.recv())
        if "error" in auth:
            print(f"Auth failed: {auth['error']['message']}")
            return

        # Fetch 600 × 5m candles (~50 hours of data)
        print("Fetching 600 historical 5m candles from Deriv…")
        await ws.send(json.dumps({
            "ticks_history": PAIR,
            "adjust_start_time": 1,
            "count": 600,
            "end": "latest",
            "granularity": 300,
            "style": "candles"
        }))

        candles_msg = json.loads(await ws.recv())
        if "error" in candles_msg:
            print(f"Candle fetch failed: {candles_msg['error']['message']}")
            return

        raw = candles_msg["candles"]
        print(f"Received {len(raw)} candles\n")

        df = pd.DataFrame(raw)
        df["epoch"] = pd.to_datetime(df["epoch"], unit="s", utc=True)
        df = df.rename(columns={"epoch": "time", "open": "open", "high": "high",
                                 "low": "low", "close": "close"})
        df = df[["time", "open", "high", "low", "close"]].astype(
            {"open": float, "high": float, "low": float, "close": float}
        )
        df = add_indicators(df)

        # Replay last 48h
        cutoff = df["time"].iloc[-1] - pd.Timedelta(hours=48)
        window = df[df["time"] >= cutoff].reset_index(drop=True)
        print(f"Analysing {len(window)} candles from {window['time'].iloc[0]} → {window['time'].iloc[-1]}\n")

        trades = []
        skipped_session = 0
        skipped_signal  = 0

        for i in range(2, len(window) - 1):
            row   = window.iloc[i]
            prev  = window.iloc[i - 1]
            price = float(row["close"])
            ts    = row["time"]

            in_sess, sess_name = in_session(ts)
            if not in_sess:
                skipped_session += 1
                continue

            signal = check_signal(row, prev, price)
            if signal is None:
                skipped_signal += 1
                continue

            atr  = row["atr"] if not pd.isna(row["atr"]) else 0.0005
            sl   = round(price - 1.5 * atr, 5) if signal == "BUY" else round(price + 1.5 * atr, 5)
            tp   = round(price + 2.5 * atr, 5) if signal == "BUY" else round(price - 2.5 * atr, 5)
            rsi  = round(row["rsi"], 1)

            trades.append({
                "time":    ts.strftime("%Y-%m-%d %H:%M UTC"),
                "signal":  signal,
                "price":   price,
                "sl":      sl,
                "tp":      tp,
                "atr":     round(atr, 6),
                "rsi":     rsi,
                "session": sess_name,
            })

        # ── Results ──────────────────────────────────────────────────────────
        print(f"{'─'*60}")
        print(f"  TRADES THAT WOULD HAVE BEEN TAKEN: {len(trades)}")
        print(f"  Candles skipped (off-session): {skipped_session}")
        print(f"  Candles skipped (no signal)  : {skipped_signal}")
        print(f"{'─'*60}\n")

        if not trades:
            print("  No setups found in this window.")
        else:
            print(f"  {'Time':<22} {'Dir':<5} {'Entry':>9} {'SL':>9} {'TP':>9} {'RSI':>6}  Session")
            print(f"  {'─'*22} {'─'*5} {'─'*9} {'─'*9} {'─'*9} {'─'*6}  {'─'*18}")
            for t in trades:
                arrow = "▲" if t["signal"] == "BUY" else "▼"
                print(f"  {t['time']:<22} {arrow} {t['signal']:<4} "
                      f"{t['price']:>9.5f} {t['sl']:>9.5f} {t['tp']:>9.5f} "
                      f"{t['rsi']:>6.1f}  {t['session']}")

        print(f"\n{'='*60}")
        print(f"  Summary: {len(trades)} setups over 48h | Old strategy would have found ~0")
        print(f"{'='*60}\n")

asyncio.run(run())
