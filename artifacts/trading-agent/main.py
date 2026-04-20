"""
main.py — Entry point and orchestrator.
Wires all 8 layers together and runs the trading loop.

Usage:
    python main.py

The agent runs continuously until stopped (Ctrl+C or capital floor).
"""

import asyncio
import logging
import sys
from datetime import datetime, timezone

import pandas as pd

# ─── Internal modules ─────────────────────────────────────────────────────
from notifications import setup_logging, notify_info, notify_warning
from database import initialize_database, get_or_create_daily_stats, get_state, set_state
from market_data import MarketDataEngine
from strategy import StrategyEngine
from ai_decision import AIDecisionLayer
from risk_management import RiskManager
from trade_execution import TradeExecutor
from performance import PerformanceTracker
from api_server import inject_dependencies, start_api_server_thread
from config import (
    FOREX_PAIRS,
    TIMEFRAME_5M,
    TIMEFRAME_15M,
    DEMO_MODE,
    ACCOUNT_ID,
    FLASK_PORT,
)

# ─── Boot ─────────────────────────────────────────────────────────────────
setup_logging()
logger = logging.getLogger("Main")


class TradingAgent:
    """
    Top-level orchestrator. Connects all layers and runs the main event loop.
    """

    def __init__(self):
        logger.info("="*60)
        logger.info("  AI Trading Agent — Starting Up")
        logger.info("  Mode    : %s", "DEMO" if DEMO_MODE else "LIVE ⚠️")
        logger.info("  Account : %s", ACCOUNT_ID)
        logger.info("="*60)

        # ── Initialise database ────────────────────────────────────────────
        initialize_database()

        # ── Get starting balance from DB or use a placeholder until WS auth ─
        # Real balance is fetched after authorisation; use stored value for startup
        stored_balance = float(get_state("last_known_balance", "10000.0"))

        # ── Instantiate all layers ─────────────────────────────────────────
        self.mde          = MarketDataEngine()
        self.strategy     = StrategyEngine()
        self.ai_layer     = AIDecisionLayer()
        self.risk_manager = RiskManager(starting_balance=stored_balance)
        self.executor     = TradeExecutor(self.mde, self.risk_manager)
        self.performance  = PerformanceTracker(self.risk_manager)

        # ── Wire market data callbacks ─────────────────────────────────────
        self.mde.add_candle_callback(self._on_new_candle)

        # ── Pairs that need analysis on next candle ────────────────────────
        self._pending_analysis: set = set()

        # ── Daily/weekly reset tracking ────────────────────────────────────
        self._last_day  = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        self._last_week = self._current_week_key()

    # ─── Main entry point ──────────────────────────────────────────────────

    async def run(self):
        """Start the WebSocket engine and the API server."""
        loop = asyncio.get_event_loop()

        # Wire Flask API with shared references
        inject_dependencies(self.risk_manager, self.performance, self.executor, loop)
        start_api_server_thread()
        notify_info(f"REST API available at http://0.0.0.0:{FLASK_PORT}")

        # Start the WebSocket connection (reconnects automatically)
        notify_info("Starting market data engine…")
        await self.mde.connect()

    # ─── Candle callback ───────────────────────────────────────────────────

    async def _on_new_candle(self, pair: str, timeframe: int, df: pd.DataFrame):
        """
        Called every time a new OHLCV candle closes on any subscribed pair/timeframe.
        Triggers analysis on every 5m candle close — 15m data is already in memory.
        """
        # Only trigger full analysis on 5m candle closes (every 5 minutes)
        if timeframe != TIMEFRAME_5M:
            return

        await self._analyse_pair(pair)

        # Daily / weekly reset checks
        await self._check_day_rollover()

    # ─── Per-pair analysis ─────────────────────────────────────────────────

    async def _analyse_pair(self, pair: str):
        """
        Full analysis pipeline for a single pair:
        session → candle data → strategy → AI score → risk → execute.
        """
        # 1. Session / timing gate
        session_ok, session_name = self.strategy.should_trade_now(pair)
        if not session_ok:
            from notifications import notify_skipped_trade
            notify_skipped_trade(pair, session_name)
            return

        # 2. Get current price and indicator data
        current_price = self.mde.get_latest_price(pair)
        df_5m         = self.mde.get_candles(pair, TIMEFRAME_5M)
        df_15m        = self.mde.get_candles(pair, TIMEFRAME_15M)

        if current_price is None or df_5m.empty or df_15m.empty:
            notify_warning(f"Insufficient data for {pair} — skipping")
            return

        # 3. Strategy signal
        signal = self.strategy.evaluate_signal(pair, df_5m, df_15m, current_price)
        if signal is None:
            return   # notify already done inside evaluate_signal

        # 4. Risk manager pre-trade gate
        can_trade, risk_reason = self.risk_manager.can_trade(pair)
        if not can_trade:
            from notifications import notify_skipped_trade
            notify_skipped_trade(pair, risk_reason)
            return

        # 5. AI confidence scoring
        approved, confidence, ai_reason = self.ai_layer.score_and_approve(
            pair, signal, df_5m, df_15m, current_price, session_name
        )
        if not approved:
            return   # notify already done inside score_and_approve

        # 6. Pair exposure multiplier (from performance tracker)
        exposure_mult = self.performance.get_pair_exposure_multiplier(pair)
        if exposure_mult < 1.0:
            notify_warning(f"{pair} exposure multiplier {exposure_mult:.2f}× due to recent losses")

        # 7. Get ATR for position sizing and SL/TP
        atr_value = df_5m["atr"].iloc[-2] if "atr" in df_5m.columns and len(df_5m) > 2 else 0.001

        # 8. Calculate stake and SL/TP
        base_stake = self.risk_manager.calculate_stake(atr_value, current_price)
        stake      = round(base_stake * exposure_mult, 2)
        stake      = max(stake, 1.0)  # Deriv minimum stake

        stop_loss, take_profit = self.risk_manager.calculate_sl_tp(signal, current_price, atr_value)

        reason = f"{signal} | {session_name} | {ai_reason}"

        # 9. Execute the trade
        contract_id = await self.executor.open_trade(
            pair, signal, stake,
            current_price, stop_loss, take_profit,
            confidence, reason
        )

        if contract_id:
            # Persist balance snapshot
            set_state("last_known_balance", self.risk_manager.current_balance)

    # ─── Daily / weekly rollover ───────────────────────────────────────────

    async def _check_day_rollover(self):
        """Reset daily limits at UTC midnight."""
        now       = datetime.now(tz=timezone.utc)
        today     = now.strftime("%Y-%m-%d")
        week_key  = self._current_week_key()

        if today != self._last_day:
            self._last_day = today
            self.risk_manager.reset_daily(self.risk_manager.current_balance)
            notify_info(f"New trading day: {today}")

        if week_key != self._last_week:
            self._last_week = week_key
            self.risk_manager.reset_weekly(self.risk_manager.current_balance)
            notify_info(f"New trading week: {week_key}")

    @staticmethod
    def _current_week_key() -> str:
        from datetime import timedelta
        today  = datetime.now(tz=timezone.utc).date()
        monday = today - timedelta(days=today.weekday())
        return str(monday)


# ─── Entry point ──────────────────────────────────────────────────────────

def main():
    agent = TradingAgent()
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        notify_info("Shutdown requested — stopping agent gracefully.")
        sys.exit(0)


if __name__ == "__main__":
    main()
