"""
strategy.py — Layer 2: Strategy Engine.
Implements the trend-following rules and session-awareness logic.
Returns BUY, SELL, or None based on both 5m and 15m timeframes.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from config import (
    TIMEFRAME_5M,
    TIMEFRAME_15M,
    ASIAN_OPEN_UTC,
    ASIAN_CLOSE_UTC,
    LONDON_OPEN_UTC,
    LONDON_CLOSE_UTC,
    NY_OPEN_UTC,
    NY_CLOSE_UTC,
    SESSION_BUFFER_MINUTES,
    NEWS_FILTER_ACTIVE,
)
from notifications import notify_skipped_trade, notify_session_info

logger = logging.getLogger("Strategy")


class StrategyEngine:
    """
    Evaluates the current market state on both 5m and 15m timeframes.
    Only signals a trade when all conditions align on both timeframes
    AND the timing falls within a favourable trading session.
    """

    # ─── Session logic ─────────────────────────────────────────────────────

    @staticmethod
    def get_current_utc_hour_minute() -> tuple:
        now = datetime.now(tz=timezone.utc)
        return now.hour, now.minute

    def is_high_probability_session(self) -> tuple[bool, str]:
        """
        Return (is_active, session_name).
        Priority: London/NY overlap → pure London → pure NY → Asian → nothing.
        Outside sessions or within buffer windows → not active.
        """
        hour, minute = self.get_current_utc_hour_minute()
        total_minutes = hour * 60 + minute

        asian_open_m   = ASIAN_OPEN_UTC * 60 + SESSION_BUFFER_MINUTES
        asian_close_m  = ASIAN_CLOSE_UTC * 60 - SESSION_BUFFER_MINUTES
        london_open_m  = LONDON_OPEN_UTC * 60 + SESSION_BUFFER_MINUTES
        london_close_m = LONDON_CLOSE_UTC * 60 - SESSION_BUFFER_MINUTES
        ny_open_m      = NY_OPEN_UTC * 60 + SESSION_BUFFER_MINUTES
        ny_close_m     = NY_CLOSE_UTC * 60 - SESSION_BUFFER_MINUTES

        in_asian  = asian_open_m  <= total_minutes <= asian_close_m
        in_london = london_open_m <= total_minutes <= london_close_m
        in_ny     = ny_open_m     <= total_minutes <= ny_close_m

        if in_london and in_ny:
            return True, "London/NY Overlap (HIGHEST PRIORITY)"
        elif in_london:
            return True, "London Session"
        elif in_ny:
            return True, "New York Session"
        elif in_asian:
            return True, "Asian Session (Tokyo)"
        else:
            return False, "Off-session"

    def should_trade_now(self, pair: str) -> tuple[bool, str]:
        """
        Unified time/session gate.
        Returns (allowed, reason).
        """
        if NEWS_FILTER_ACTIVE:
            return False, "News filter is ACTIVE — trading paused manually"

        active, session = self.is_high_probability_session()
        if not active:
            return False, f"Outside high-probability session: {session}"

        return True, session

    # ─── Signal generation ─────────────────────────────────────────────────

    def evaluate_signal(
        self,
        pair: str,
        df_5m: pd.DataFrame,
        df_15m: pd.DataFrame,
        current_price: float,
    ) -> Optional[str]:
        """
        Evaluate BUY/SELL/None for a pair.
        Either timeframe confirming is sufficient — only skip if they conflict.
        """
        signal_5m  = self._check_direction(df_5m,  current_price)
        signal_15m = self._check_direction(df_15m, current_price)

        # Hard conflict — one says BUY, other says SELL — skip
        if signal_5m is not None and signal_15m is not None and signal_5m != signal_15m:
            notify_skipped_trade(pair, f"Timeframe conflict (5m={signal_5m}, 15m={signal_15m})")
            return None

        # Accept whichever timeframe has a signal (prefer 15m if both agree)
        signal = signal_15m or signal_5m
        if signal is None:
            notify_skipped_trade(pair, f"No clear signal on either timeframe")
        return signal

    def _check_direction(self, df: pd.DataFrame, price: float) -> Optional[str]:
        """
        Apply the exact strategy rules to a single timeframe DataFrame.
        Returns 'BUY', 'SELL', or None.
        """
        if df.empty or len(df) < 3:
            return None

        # Pull the latest confirmed candle (second-to-last row prevents look-ahead)
        row  = df.iloc[-2]
        prev = df.iloc[-3]

        # Guard against NaN in indicators
        required = ["rsi", "macd", "macd_signal", "macd_cross_up",
                    "macd_cross_down", "ema_fast", "ema_slow"]
        if any(pd.isna(row.get(c)) for c in required):
            return None

        rsi          = row["rsi"]
        ema_fast     = row["ema_fast"]
        ema_slow     = row["ema_slow"]
        macd         = row["macd"]
        macd_signal  = row["macd_signal"]

        # Accept crossover on the current candle OR within the last 3 candles (15-min window)
        recent_cross_up   = bool(df["macd_cross_up"].iloc[-4:-1].any())
        recent_cross_down = bool(df["macd_cross_down"].iloc[-4:-1].any())
        macd_bullish      = macd > macd_signal   # MACD momentum still positive
        macd_bearish      = macd < macd_signal   # MACD momentum still negative

        # ── BUY conditions ──────────────────────────────────────────────────
        # 1. EMA 20 > EMA 50  (uptrend)
        # 2. RSI between 40 and 70 (momentum — not overbought)
        # 3. MACD crossed above signal within last 3 candles AND still bullish
        # 4. Current price is above EMA 20
        if (
            ema_fast > ema_slow
            and 40 <= rsi <= 70
            and recent_cross_up and macd_bullish
            and price > ema_fast
        ):
            return "BUY"

        # ── SELL conditions ─────────────────────────────────────────────────
        # 1. EMA 20 < EMA 50  (downtrend)
        # 2. RSI between 30 and 60 (momentum — not oversold)
        # 3. MACD crossed below signal within last 3 candles AND still bearish
        # 4. Current price is below EMA 20
        if (
            ema_fast < ema_slow
            and 30 <= rsi <= 60
            and recent_cross_down and macd_bearish
            and price < ema_fast
        ):
            return "SELL"

        return None

    # ─── Trend/range regime ────────────────────────────────────────────────

    @staticmethod
    def is_trending(df: pd.DataFrame) -> tuple[bool, str]:
        """
        Simple regime detector: compare the slope of EMA_fast over the
        last 10 candles. If the slope is meaningful the market is trending.
        Returns (trending, description).
        """
        if df.empty or len(df) < 15 or "ema_fast" not in df.columns:
            return False, "Insufficient data"

        ema_now  = df["ema_fast"].iloc[-2]
        ema_prev = df["ema_fast"].iloc[-12]

        if pd.isna(ema_now) or pd.isna(ema_prev):
            return False, "EMA not ready"

        pct_move = abs(ema_now - ema_prev) / ema_prev * 100

        # A move of more than 0.05% in EMA over 10 candles indicates a trend
        if pct_move > 0.05:
            direction = "UP" if ema_now > ema_prev else "DOWN"
            return True, f"Trending {direction} ({pct_move:.3f}% EMA slope)"

        return False, f"Ranging market (EMA slope {pct_move:.4f}% — too flat)"
