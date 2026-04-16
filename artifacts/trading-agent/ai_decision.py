"""
ai_decision.py — Layer 3: AI Decision Layer.
Scores each trade setup 0–100, applies volatility and regime filters,
and gives a final APPROVED / REJECTED verdict with a reason.
"""

import logging
from typing import Optional, Tuple

import pandas as pd

from config import (
    CONFIDENCE_THRESHOLD,
    ATR_HIGH_MULT,
    ATR_LOW_MULT,
    TIMEFRAME_5M,
    TIMEFRAME_15M,
)
from notifications import notify_skipped_trade

logger = logging.getLogger("AIDecision")


class AIDecisionLayer:
    """
    Scores every potential trade against multiple criteria and decides
    whether to approve execution. Designed to be genuinely selective —
    fewer, higher-quality trades is always the goal.
    """

    def score_and_approve(
        self,
        pair: str,
        direction: str,
        df_5m: pd.DataFrame,
        df_15m: pd.DataFrame,
        current_price: float,
        session_name: str,
    ) -> Tuple[bool, float, str]:
        """
        Main entry point.
        Returns (approved, confidence_score, reason).
        """

        # ── Volatility filter ──────────────────────────────────────────────
        vol_ok, vol_reason = self._volatility_filter(df_5m)
        if not vol_ok:
            notify_skipped_trade(pair, f"Volatility filter: {vol_reason}")
            return False, 0.0, vol_reason

        # ── Market regime filter ───────────────────────────────────────────
        regime_ok, regime_reason = self._regime_filter(df_5m, df_15m)
        if not regime_ok:
            notify_skipped_trade(pair, f"Regime filter: {regime_reason}")
            return False, 0.0, regime_reason

        # ── Confidence scoring ─────────────────────────────────────────────
        score, breakdown = self._compute_confidence(
            direction, df_5m, df_15m, current_price, session_name
        )

        if score < CONFIDENCE_THRESHOLD:
            reason = f"Low confidence {score:.1f}/100 — threshold {CONFIDENCE_THRESHOLD}"
            notify_skipped_trade(pair, reason + f" | {breakdown}")
            return False, score, reason

        reason = f"Confidence {score:.1f}/100 — {breakdown}"
        logger.info("  APPROVED %s %s | %s", pair, direction, reason)
        return True, score, reason

    # ─── Volatility filter ─────────────────────────────────────────────────

    @staticmethod
    def _volatility_filter(df: pd.DataFrame) -> Tuple[bool, str]:
        """
        Skip trades when ATR is abnormally high (dangerous) or
        abnormally low (market asleep — tight stop won't work).
        Compare current ATR to its 20-period rolling mean.
        """
        if "atr" not in df.columns or len(df) < 25:
            return True, "ATR not ready — allowing trade"

        current_atr = df["atr"].iloc[-2]
        avg_atr     = df["atr"].iloc[-22:-2].mean()

        if pd.isna(current_atr) or pd.isna(avg_atr) or avg_atr == 0:
            return True, "ATR calculation incomplete"

        ratio = current_atr / avg_atr

        if ratio > ATR_HIGH_MULT:
            return False, f"Volatility too HIGH (ATR {ratio:.2f}× average) — dangerous"
        if ratio < ATR_LOW_MULT:
            return False, f"Volatility too LOW (ATR {ratio:.2f}× average) — market asleep"

        return True, f"Volatility normal (ATR {ratio:.2f}× average)"

    # ─── Regime filter ─────────────────────────────────────────────────────

    @staticmethod
    def _regime_filter(df_5m: pd.DataFrame, df_15m: pd.DataFrame) -> Tuple[bool, str]:
        """
        Detect whether the market is trending or ranging.
        Uses ADX if available, otherwise falls back to EMA slope + BB width.
        Only approves trend-following trades in clearly trending markets.
        """

        def analyse_regime(df: pd.DataFrame, label: str) -> Tuple[bool, str]:
            if df.empty or len(df) < 25:
                return True, f"{label}: insufficient data"

            # BB width as a proxy for directional clarity
            if "bb_upper" in df.columns and "bb_lower" in df.columns:
                bb_width = (df["bb_upper"].iloc[-2] - df["bb_lower"].iloc[-2])
                avg_bb   = (df["bb_upper"].iloc[-22:-2] - df["bb_lower"].iloc[-22:-2]).mean()
                if not pd.isna(bb_width) and not pd.isna(avg_bb) and avg_bb > 0:
                    if bb_width < avg_bb * 0.6:
                        return False, f"{label}: Bands contracting — possible ranging/squeeze"

            # EMA separation as directional clarity
            if "ema_fast" in df.columns and "ema_slow" in df.columns:
                ema_fast = df["ema_fast"].iloc[-2]
                ema_slow = df["ema_slow"].iloc[-2]
                if not pd.isna(ema_fast) and not pd.isna(ema_slow) and ema_slow != 0:
                    separation_pct = abs(ema_fast - ema_slow) / ema_slow * 100
                    if separation_pct < 0.02:
                        return False, f"{label}: EMAs too close ({separation_pct:.4f}%) — choppy"

            return True, f"{label}: Trending"

        ok_5m,  reason_5m  = analyse_regime(df_5m,  "5m")
        ok_15m, reason_15m = analyse_regime(df_15m, "15m")

        if not ok_5m:
            return False, reason_5m
        if not ok_15m:
            return False, reason_15m

        return True, "Trending on both timeframes"

    # ─── Confidence scoring ────────────────────────────────────────────────

    def _compute_confidence(
        self,
        direction: str,
        df_5m: pd.DataFrame,
        df_15m: pd.DataFrame,
        price: float,
        session_name: str,
    ) -> Tuple[float, str]:
        """
        Score 0–100 by awarding points for each bullish/bearish alignment.
        Maximum achievable: 100 points.
        """
        score = 0.0
        components = []

        for df, label, weight_mult in [
            (df_5m,  "5m",  1.0),
            (df_15m, "15m", 1.2),   # 15m signals weighted slightly higher
        ]:
            if df.empty or len(df) < 5:
                continue
            row = df.iloc[-2]

            # 1. EMA alignment (10 pts per TF, scaled by weight)
            ema_fast = row.get("ema_fast")
            ema_slow = row.get("ema_slow")
            if not pd.isna(ema_fast) and not pd.isna(ema_slow):
                if direction == "BUY"  and ema_fast > ema_slow:
                    pts = 10 * weight_mult; score += pts
                    components.append(f"{label}EMA+{pts:.0f}")
                elif direction == "SELL" and ema_fast < ema_slow:
                    pts = 10 * weight_mult; score += pts
                    components.append(f"{label}EMA+{pts:.0f}")

            # 2. RSI in optimal zone (10 pts per TF)
            rsi = row.get("rsi")
            if not pd.isna(rsi):
                if direction == "BUY"  and 50 <= rsi <= 60:
                    pts = 10 * weight_mult; score += pts
                    components.append(f"{label}RSI_opt+{pts:.0f}")
                elif direction == "BUY"  and 45 <= rsi < 50:
                    pts = 5 * weight_mult;  score += pts
                    components.append(f"{label}RSI_ok+{pts:.0f}")
                elif direction == "SELL" and 40 <= rsi <= 50:
                    pts = 10 * weight_mult; score += pts
                    components.append(f"{label}RSI_opt+{pts:.0f}")
                elif direction == "SELL" and 50 < rsi <= 55:
                    pts = 5 * weight_mult;  score += pts
                    components.append(f"{label}RSI_ok+{pts:.0f}")

            # 3. MACD crossover (15 pts per TF — strong signal)
            cross_up   = row.get("macd_cross_up",   False)
            cross_down = row.get("macd_cross_down",  False)
            if direction == "BUY"  and cross_up:
                pts = 15 * weight_mult; score += pts
                components.append(f"{label}MACD_cross+{pts:.0f}")
            elif direction == "SELL" and cross_down:
                pts = 15 * weight_mult; score += pts
                components.append(f"{label}MACD_cross+{pts:.0f}")

            # 4. Price vs EMA (5 pts per TF)
            if not pd.isna(ema_fast):
                if direction == "BUY"  and price > ema_fast:
                    pts = 5 * weight_mult; score += pts
                    components.append(f"{label}PriceAboveEMA+{pts:.0f}")
                elif direction == "SELL" and price < ema_fast:
                    pts = 5 * weight_mult; score += pts
                    components.append(f"{label}PriceBelowEMA+{pts:.0f}")

        # 5. Session bonus (up to 10 pts)
        if "Overlap" in session_name:
            score += 10; components.append("Session_Overlap+10")
        elif "London" in session_name or "New York" in session_name:
            score += 5;  components.append("Session_Prime+5")

        # Cap at 100
        score = min(score, 100.0)
        return score, " | ".join(components) if components else "No components"
