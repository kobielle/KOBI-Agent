"""
trade_execution.py — Layer 5: Trade Execution.
Sends buy/sell contracts to Deriv via WebSocket, monitors open trades,
and logs everything to the database.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, List

from config import MULTIPLIER_VALUE, FOREX_PAIRS
from database import (
    log_trade_open,
    log_trade_close,
    get_open_trades,
    count_open_trades_for_pair,
    update_daily_stats,
)
from notifications import notify_trade_open, notify_trade_close, notify_warning

logger = logging.getLogger("TradeExecution")


class TradeExecutor:
    """
    Handles all trade lifecycle events:
    open → monitor → close.
    Communicates with Deriv through the shared WebSocket managed by MarketDataEngine.
    """

    def __init__(self, market_data_engine, risk_manager):
        self.mde  = market_data_engine   # MarketDataEngine instance (owns the WS)
        self.rm   = risk_manager          # RiskManager instance
        self._open_positions: Dict[str, dict] = {}  # contract_id → trade info

    # ─── Open a trade ──────────────────────────────────────────────────────

    async def open_trade(
        self,
        pair: str,
        direction: str,
        stake: float,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        confidence: float,
        reason: str,
    ) -> Optional[str]:
        """
        Place a multipliers contract on Deriv.
        Returns the contract_id on success, None on failure.
        All parameters must be validated by risk manager before calling this.
        """
        if not self.mde.ws or not self.mde.authorized:
            logger.warning("Cannot open trade — WebSocket not connected or not authorised.")
            return None

        # Deriv contract type: MULTUP for BUY, MULTDOWN for SELL
        contract_type = "MULTUP" if direction == "BUY" else "MULTDOWN"

        payload = {
            "buy": 1,
            "subscribe": 1,
            "price": stake,
            "parameters": {
                "amount":          stake,
                "basis":           "stake",
                "contract_type":   contract_type,
                "currency":        "USD",
                "symbol":          pair,
                "multiplier":      MULTIPLIER_VALUE,
                # limit_order sets stop-loss and take-profit on the contract itself
                "limit_order": {
                    "stop_loss":   stop_loss,
                    "take_profit": take_profit,
                },
            },
        }

        resp = await self.mde._send_and_wait(payload, timeout=20)

        if "error" in resp:
            err = resp["error"]["message"]
            logger.error("Failed to open %s %s: %s", direction, pair, err)
            return None

        buy_data    = resp.get("buy", {})
        contract_id = str(buy_data.get("contract_id", ""))

        if not contract_id:
            logger.error("No contract_id returned for %s %s", direction, pair)
            return None

        # Log to database
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        trade_id = log_trade_open(
            pair, direction, entry_price, stake, confidence, reason, contract_id
        )

        # Track locally
        self._open_positions[contract_id] = {
            "trade_id":    trade_id,
            "pair":        pair,
            "direction":   direction,
            "entry_price": entry_price,
            "stake":       stake,
            "stop_loss":   stop_loss,
            "take_profit": take_profit,
            "confidence":  confidence,
        }

        notify_trade_open(pair, direction, entry_price, stake, confidence, reason)
        return contract_id

    # ─── Handle contract update messages ──────────────────────────────────

    async def handle_contract_update(self, msg: dict):
        """
        Called by market_data when a 'proposal_open_contract' or 'buy' message
        arrives for one of our open positions. Checks if trade is closed.
        """
        poc = msg.get("proposal_open_contract", {})
        contract_id = str(poc.get("contract_id", ""))

        if contract_id not in self._open_positions:
            return

        # Check if the contract has settled
        is_sold = poc.get("is_sold", 0)
        if not is_sold:
            return

        # Trade closed on Deriv side
        exit_price  = float(poc.get("exit_tick", 0) or poc.get("current_spot", 0))
        profit_loss = float(poc.get("profit", 0))
        exit_reason = poc.get("sell_reason", "Contract closed")

        info = self._open_positions.pop(contract_id)
        new_balance = self.rm.current_balance + profit_loss

        # Update database
        log_trade_close(info["trade_id"], exit_price, profit_loss, exit_reason)

        # Update daily stats
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        update_daily_stats(today, profit_loss, profit_loss > 0)

        # Notify user
        notify_trade_close(
            info["pair"], info["direction"],
            info["entry_price"], exit_price, profit_loss, exit_reason
        )

        # Feed result back to risk manager (updates streaks, limits)
        self.rm.record_trade_result(profit_loss, new_balance)

    # ─── Manually close a trade (emergency) ───────────────────────────────

    async def close_trade(self, contract_id: str) -> bool:
        """Force-sell an open contract (e.g., on capital floor trigger)."""
        if not self.mde.ws:
            return False
        payload = {"sell": contract_id, "price": 0}
        resp = await self.mde._send_and_wait(payload, timeout=15)
        if "error" in resp:
            logger.error("Failed to close contract %s: %s", contract_id, resp["error"]["message"])
            return False
        logger.info("Closed contract %s", contract_id)
        return True

    async def close_all_trades(self):
        """Emergency: close every open position immediately."""
        for cid in list(self._open_positions.keys()):
            await self.close_trade(cid)

    # ─── Position info ─────────────────────────────────────────────────────

    def get_open_positions(self) -> List[dict]:
        return list(self._open_positions.values())

    def is_pair_open(self, pair: str) -> bool:
        return any(v["pair"] == pair for v in self._open_positions.values())
