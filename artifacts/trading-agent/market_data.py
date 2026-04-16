"""
market_data.py — Layer 1: Market Data Engine.
Connects to Deriv WebSocket, fetches historical candles, subscribes to live
OHLCV and tick data, and computes all technical indicators on every new candle.
"""

import asyncio
import json
import logging
import time
from collections import defaultdict
from typing import Callable, Dict, List, Optional

import pandas as pd
import ta as ta_lib
import websockets

from config import (
    API_TOKEN,
    DERIV_WS_URL,
    FOREX_PAIRS,
    TIMEFRAME_5M,
    TIMEFRAME_15M,
    HISTORICAL_CANDLES,
    RSI_PERIOD,
    MACD_FAST,
    MACD_SLOW,
    MACD_SIGNAL,
    EMA_SHORT,
    EMA_LONG,
    BB_PERIOD,
    ATR_PERIOD,
)
from notifications import notify_info, notify_warning

logger = logging.getLogger("MarketData")


class MarketDataEngine:
    """
    Manages the Deriv WebSocket connection, maintains rolling candle DataFrames
    for each pair/timeframe, computes indicators, and fires callbacks when
    a new candle closes.
    """

    def __init__(self):
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.authorized = False

        # candle_data[pair][timeframe] = DataFrame with OHLCV + indicators
        self.candle_data: Dict[str, Dict[int, pd.DataFrame]] = {
            pair: {TIMEFRAME_5M: pd.DataFrame(), TIMEFRAME_15M: pd.DataFrame()}
            for pair in FOREX_PAIRS
        }

        # Latest tick price per pair
        self.latest_ticks: Dict[str, float] = {}

        # Maps Deriv subscription IDs → (pair, timeframe)
        self._subscription_map: Dict[str, tuple] = {}

        # Callback fired when a new candle closes: (pair, timeframe, df)
        self._candle_callbacks: List[Callable] = []

        # Request ID counter (thread-safe with asyncio single-thread)
        self._req_id = 0

        # Pending request futures: req_id → Future
        self._pending: Dict[int, asyncio.Future] = {}

    # ─── Public API ────────────────────────────────────────────────────────

    def add_candle_callback(self, fn: Callable):
        """Register a callback called each time a new confirmed candle closes."""
        self._candle_callbacks.append(fn)

    def get_candles(self, pair: str, timeframe: int) -> pd.DataFrame:
        """Return the current indicator-enriched candle DataFrame."""
        return self.candle_data[pair][timeframe]

    def get_latest_price(self, pair: str) -> Optional[float]:
        return self.latest_ticks.get(pair)

    # ─── Connection & auth ─────────────────────────────────────────────────

    async def connect(self):
        """Open WebSocket, authenticate, load history, and start subscriptions."""
        while True:
            try:
                logger.info("Connecting to Deriv WebSocket…")
                async with websockets.connect(
                    DERIV_WS_URL,
                    ping_interval=20,
                    ping_timeout=10,
                    open_timeout=15,
                ) as ws:
                    self.ws = ws
                    self.authorized = False
                    notify_info("WebSocket connected — authenticating…")

                    # Start the background reader task IMMEDIATELY so that
                    # _send_and_wait can get responses during auth/history loading.
                    reader_task = asyncio.ensure_future(self._reader_loop(ws))

                    try:
                        await self._authorize()
                        notify_info("Authorized — loading historical candles…")
                        await self._load_all_history()
                        await self._subscribe_all()
                        # Block here until the reader exits (WS closes)
                        await reader_task
                    finally:
                        reader_task.cancel()

            except (websockets.ConnectionClosedError, websockets.ConnectionClosedOK,
                    OSError, asyncio.TimeoutError) as exc:
                logger.warning("WebSocket disconnected: %s — reconnecting in 5s…", exc)
                self.ws = None
                self.authorized = False
                await asyncio.sleep(5)
            except Exception as exc:
                logger.error("Unexpected WS error: %s — reconnecting in 10s…", exc, exc_info=True)
                await asyncio.sleep(10)

    async def _reader_loop(self, ws):
        """Background task: continuously read and route every message from Deriv."""
        try:
            async for raw in ws:
                await self._handle_message(raw)
        except (websockets.ConnectionClosedError, websockets.ConnectionClosedOK):
            pass
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("Reader loop error: %s", exc, exc_info=True)

    # ─── Authorisation ─────────────────────────────────────────────────────

    async def _authorize(self):
        resp = await self._send_and_wait({"authorize": API_TOKEN})
        if "error" in resp:
            raise RuntimeError(f"Authorization failed: {resp['error']['message']}")
        self.authorized = True

    # ─── Historical candle loading ──────────────────────────────────────────

    async def _load_all_history(self):
        """Fetch 200 candles for each pair × timeframe before subscribing live."""
        tasks = []
        for pair in FOREX_PAIRS:
            for tf in [TIMEFRAME_5M, TIMEFRAME_15M]:
                tasks.append(self._fetch_history(pair, tf))
        await asyncio.gather(*tasks)
        notify_info("Historical candle data loaded for all pairs.")

    async def _fetch_history(self, pair: str, timeframe: int):
        req = {
            "ticks_history": pair,
            "adjust_start_time": 1,
            "count": HISTORICAL_CANDLES,
            "end": "latest",
            "granularity": timeframe,
            "style": "candles",
        }
        resp = await self._send_and_wait(req)
        if "error" in resp:
            logger.warning("History error for %s/%ds: %s", pair, timeframe, resp["error"]["message"])
            return
        candles = resp.get("candles", [])
        df = self._candles_to_df(candles)
        df = self._compute_indicators(df)
        self.candle_data[pair][timeframe] = df
        logger.info("Loaded %d historical candles for %s/%dm", len(df), pair, timeframe // 60)

    # ─── Live subscriptions ────────────────────────────────────────────────

    async def _subscribe_all(self):
        """Subscribe to live OHLCV candles and ticks for all pairs."""
        tasks = []
        for pair in FOREX_PAIRS:
            for tf in [TIMEFRAME_5M, TIMEFRAME_15M]:
                tasks.append(self._subscribe_candles(pair, tf))
            tasks.append(self._subscribe_ticks(pair))
        await asyncio.gather(*tasks)
        notify_info("Subscribed to live candles and ticks for all pairs.")

    async def _subscribe_candles(self, pair: str, timeframe: int):
        req = {
            "ticks_history": pair,
            "adjust_start_time": 1,
            "count": 1,
            "end": "latest",
            "granularity": timeframe,
            "style": "candles",
            "subscribe": 1,
        }
        resp = await self._send_and_wait(req)
        if "error" in resp:
            logger.warning("Candle sub error for %s/%ds: %s", pair, timeframe, resp["error"]["message"])
            return
        sub_id = resp.get("subscription", {}).get("id")
        if sub_id:
            self._subscription_map[sub_id] = (pair, timeframe)

    async def _subscribe_ticks(self, pair: str):
        req = {"ticks": pair, "subscribe": 1}
        resp = await self._send_and_wait(req)
        if "error" in resp:
            logger.warning("Tick sub error for %s: %s", pair, resp["error"]["message"])

    # ─── Message handling ──────────────────────────────────────────────────

    async def _handle_message(self, raw: str):
        """Route incoming Deriv messages to the correct handler."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        req_id = msg.get("req_id")
        msg_type = msg.get("msg_type")

        # Resolve pending request futures first
        if req_id and req_id in self._pending:
            fut = self._pending.pop(req_id)
            if not fut.done():
                fut.set_result(msg)
            return

        # Process live subscription updates
        if msg_type == "ohlc":
            await self._handle_ohlc(msg)
        elif msg_type == "tick":
            self._handle_tick(msg)
        elif msg_type == "candles":
            # Bulk candle response from subscribe (handled via pending)
            pass

    async def _handle_ohlc(self, msg: dict):
        """Process a new live OHLC update from Deriv."""
        ohlc = msg.get("ohlc", {})
        sub_id = msg.get("subscription", {}).get("id")
        pair_tf = self._subscription_map.get(sub_id)
        if not pair_tf:
            return
        pair, tf = pair_tf

        # Only act when a candle is confirmed closed (epoch changes)
        new_epoch = int(ohlc.get("open_time", 0))
        df = self.candle_data[pair][tf]

        if df.empty or new_epoch > int(df.index[-1].timestamp()):
            # New candle — append and recompute indicators
            new_row = {
                "open":   float(ohlc["open"]),
                "high":   float(ohlc["high"]),
                "low":    float(ohlc["low"]),
                "close":  float(ohlc["close"]),
                "volume": float(ohlc.get("tick_count", 0)),
            }
            ts = pd.Timestamp(new_epoch, unit="s", tz="UTC")
            new_df = pd.DataFrame([new_row], index=[ts])
            df = pd.concat([df, new_df]).tail(HISTORICAL_CANDLES)
            df = self._compute_indicators(df)
            self.candle_data[pair][tf] = df

            # Fire all registered callbacks
            for cb in self._candle_callbacks:
                try:
                    await cb(pair, tf, df)
                except Exception as exc:
                    logger.error("Candle callback error: %s", exc, exc_info=True)
        else:
            # Update the last candle in place (candle still forming)
            df.at[df.index[-1], "close"] = float(ohlc["close"])
            df.at[df.index[-1], "high"]  = max(df.at[df.index[-1], "high"], float(ohlc["high"]))
            df.at[df.index[-1], "low"]   = min(df.at[df.index[-1], "low"],  float(ohlc["low"]))
            self.candle_data[pair][tf] = self._compute_indicators(df)

    def _handle_tick(self, msg: dict):
        """Store the latest bid price for the pair."""
        tick = msg.get("tick", {})
        pair = tick.get("symbol")
        price = tick.get("quote")
        if pair and price:
            self.latest_ticks[pair] = float(price)

    # ─── Indicator calculation ─────────────────────────────────────────────

    @staticmethod
    def _candles_to_df(candles: list) -> pd.DataFrame:
        """Convert Deriv candle list to a pandas DataFrame."""
        if not candles:
            return pd.DataFrame()
        rows = []
        for c in candles:
            rows.append({
                "open":   float(c["open"]),
                "high":   float(c["high"]),
                "low":    float(c["low"]),
                "close":  float(c["close"]),
                "volume": float(c.get("tick_count", 0)),
            })
        idx = pd.to_datetime([c["epoch"] for c in candles], unit="s", utc=True)
        df = pd.DataFrame(rows, index=idx)
        return df

    @staticmethod
    def _compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
        """
        Compute all required technical indicators and attach them to the DataFrame.
        Uses the `ta` library for accurate, vectorised calculations.
        """
        min_required = max(EMA_LONG, MACD_SLOW + MACD_SIGNAL, BB_PERIOD, ATR_PERIOD) + 5
        if len(df) < min_required:
            return df  # Not enough candles yet — return unchanged

        # ── RSI ─────────────────────────────────────────────────────────────
        from ta.momentum import RSIIndicator
        df["rsi"] = RSIIndicator(close=df["close"], window=RSI_PERIOD).rsi()

        # ── MACD ────────────────────────────────────────────────────────────
        from ta.trend import MACD
        macd_obj = MACD(
            close=df["close"],
            window_fast=MACD_FAST,
            window_slow=MACD_SLOW,
            window_sign=MACD_SIGNAL,
        )
        df["macd"]        = macd_obj.macd()
        df["macd_signal"] = macd_obj.macd_signal()
        df["macd_hist"]   = macd_obj.macd_diff()

        # Crossover signals: True on the candle where MACD crosses ABOVE/BELOW signal
        df["macd_cross_up"]   = (
            (df["macd"] > df["macd_signal"]) &
            (df["macd"].shift(1) <= df["macd_signal"].shift(1))
        )
        df["macd_cross_down"] = (
            (df["macd"] < df["macd_signal"]) &
            (df["macd"].shift(1) >= df["macd_signal"].shift(1))
        )

        # ── EMAs ─────────────────────────────────────────────────────────────
        from ta.trend import EMAIndicator
        df["ema_fast"] = EMAIndicator(close=df["close"], window=EMA_SHORT).ema_indicator()
        df["ema_slow"] = EMAIndicator(close=df["close"], window=EMA_LONG).ema_indicator()

        # ── Bollinger Bands ──────────────────────────────────────────────────
        from ta.volatility import BollingerBands
        bb = BollingerBands(close=df["close"], window=BB_PERIOD)
        df["bb_upper"]  = bb.bollinger_hband()
        df["bb_middle"] = bb.bollinger_mavg()
        df["bb_lower"]  = bb.bollinger_lband()

        # ── ATR (Average True Range — volatility measure) ────────────────────
        from ta.volatility import AverageTrueRange
        df["atr"] = AverageTrueRange(
            high=df["high"], low=df["low"], close=df["close"], window=ATR_PERIOD
        ).average_true_range()

        return df
    
    # ─── Send/await helper ─────────────────────────────────────────────────

    async def _send_and_wait(self, payload: dict, timeout: float = 30.0) -> dict:
        """Send a Deriv API request and await its response by req_id."""
        self._req_id += 1
        payload["req_id"] = self._req_id
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        self._pending[self._req_id] = fut
        await self.ws.send(json.dumps(payload))
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(self._req_id, None)
            logger.warning("Request %d timed out", self._req_id)
            return {"error": {"message": "Request timeout"}}
