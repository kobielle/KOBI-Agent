"""
performance.py — Layer 6: Performance Tracking & Adaptive Learning.
Analyses trade history every 20 trades and adjusts position sizing
and pair exposure based on real outcomes.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Dict, Optional

from config import (
    PERFORMANCE_SAMPLE_SIZE,
    MIN_WIN_RATE,
    HIGH_WIN_RATE,
    MAX_RISK_PER_TRADE,
    RISK_PER_TRADE,
    PERFORMANCE_LOG_PATH,
)
from database import get_last_n_trades, get_trades_for_pair
from notifications import notify_performance_report, notify_info, notify_warning

logger = logging.getLogger("Performance")


class PerformanceTracker:
    """
    Tracks closed trades and fires an analysis every PERFORMANCE_SAMPLE_SIZE trades.
    Feeds findings back to the risk manager to adapt position sizing.
    """

    def __init__(self, risk_manager):
        self.rm                  = risk_manager
        self._trade_count        = 0          # Trades since last analysis
        self._total_trades_ever  = 0
        self._pair_loss_counts: Dict[str, int]   = {}   # per-pair consecutive losses
        self._pair_exposure_mult: Dict[str, float] = {}  # per-pair size multiplier

        os.makedirs(PERFORMANCE_LOG_PATH, exist_ok=True)

    def record_trade(self, pair: str, profit_loss: float):
        """Called after every trade closes."""
        self._trade_count       += 1
        self._total_trades_ever += 1

        # Track per-pair performance
        if profit_loss < 0:
            self._pair_loss_counts[pair] = self._pair_loss_counts.get(pair, 0) + 1
        else:
            self._pair_loss_counts[pair] = 0

        # Reduce exposure if a pair keeps losing
        if self._pair_loss_counts.get(pair, 0) >= 3:
            current = self._pair_exposure_mult.get(pair, 1.0)
            self._pair_exposure_mult[pair] = max(current * 0.5, 0.1)
            notify_warning(
                f"Pair {pair} has {self._pair_loss_counts[pair]} consecutive losses — "
                f"exposure reduced to {self._pair_exposure_mult[pair]*100:.0f}%"
            )

        # Fire full analysis every N trades
        if self._trade_count >= PERFORMANCE_SAMPLE_SIZE:
            self._run_analysis()
            self._trade_count = 0

    def get_pair_exposure_multiplier(self, pair: str) -> float:
        """Return the exposure multiplier for a pair (default 1.0)."""
        return self._pair_exposure_mult.get(pair, 1.0)

    # ─── Full performance analysis ─────────────────────────────────────────

    def _run_analysis(self):
        """Analyse the last N trades and adapt strategy accordingly."""
        trades = get_last_n_trades(PERFORMANCE_SAMPLE_SIZE)
        if len(trades) < PERFORMANCE_SAMPLE_SIZE:
            return  # Not enough data yet

        wins   = [t for t in trades if (t.get("profit_loss") or 0) > 0]
        losses = [t for t in trades if (t.get("profit_loss") or 0) <= 0]

        win_rate   = len(wins) / len(trades)
        avg_profit = sum(t["profit_loss"] for t in wins)   / len(wins)   if wins   else 0
        avg_loss   = sum(t["profit_loss"] for t in losses) / len(losses) if losses else 0
        total_pnl  = sum(t["profit_loss"] for t in trades if t.get("profit_loss"))

        # Best pair
        pair_pnl: Dict[str, float] = {}
        for t in trades:
            p = t["pair"]
            pair_pnl[p] = pair_pnl.get(p, 0) + (t.get("profit_loss") or 0)

        best_pair  = max(pair_pnl, key=pair_pnl.get) if pair_pnl else "N/A"
        worst_pair = min(pair_pnl, key=pair_pnl.get) if pair_pnl else "N/A"

        report = (
            f"Performance Report — Last {PERFORMANCE_SAMPLE_SIZE} Trades\n"
            f"  Win Rate    : {win_rate*100:.1f}%\n"
            f"  Total P&L   : ${total_pnl:+.2f}\n"
            f"  Avg Win     : ${avg_profit:+.2f}\n"
            f"  Avg Loss    : ${avg_loss:+.2f}\n"
            f"  Best Pair   : {best_pair}  (${pair_pnl.get(best_pair, 0):+.2f})\n"
            f"  Worst Pair  : {worst_pair} (${pair_pnl.get(worst_pair, 0):+.2f})\n"
            f"  Risk Mult   : {self.rm.risk_multiplier:.2f}×\n"
            f"  Balance     : ${self.rm.current_balance:.2f}"
        )

        notify_performance_report(report)
        self._save_report(report)

        # Adapt position sizing based on win rate
        if win_rate < MIN_WIN_RATE:
            old = self.rm.risk_multiplier
            self.rm.risk_multiplier = max(old * 0.5, 0.1)
            notify_warning(
                f"Win rate {win_rate*100:.1f}% < {MIN_WIN_RATE*100:.0f}% threshold — "
                f"position size halved to {self.rm.risk_multiplier:.2f}×"
            )

        elif win_rate > HIGH_WIN_RATE:
            old      = self.rm.risk_multiplier
            max_mult = MAX_RISK_PER_TRADE / RISK_PER_TRADE
            self.rm.risk_multiplier = min(old * 1.10, max_mult)
            notify_info(
                f"Win rate {win_rate*100:.1f}% > {HIGH_WIN_RATE*100:.0f}% — "
                f"position size increased to {self.rm.risk_multiplier:.2f}×"
            )

    def _save_report(self, report: str):
        """Write the performance report to a timestamped text file."""
        ts       = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = os.path.join(PERFORMANCE_LOG_PATH, f"report_{ts}.txt")
        try:
            with open(filename, "w") as f:
                f.write(f"Generated at: {datetime.now(tz=timezone.utc).isoformat()}\n\n")
                f.write(report)
            logger.info("Performance report saved to %s", filename)
        except OSError as exc:
            logger.warning("Could not save performance report: %s", exc)

    def get_summary(self) -> dict:
        """Return a JSON-serialisable summary for the Flask API."""
        trades = get_last_n_trades(PERFORMANCE_SAMPLE_SIZE)
        if not trades:
            return {"message": "No trades yet"}

        wins     = [t for t in trades if (t.get("profit_loss") or 0) > 0]
        losses   = [t for t in trades if (t.get("profit_loss") or 0) <= 0]
        win_rate = len(wins) / len(trades) if trades else 0
        total    = sum(t.get("profit_loss", 0) for t in trades)

        return {
            "sample_size":  len(trades),
            "win_rate_pct": round(win_rate * 100, 1),
            "total_pnl":    round(total, 2),
            "avg_win":      round(sum(t["profit_loss"] for t in wins)   / len(wins),   2) if wins   else 0,
            "avg_loss":     round(sum(t["profit_loss"] for t in losses) / len(losses), 2) if losses else 0,
            "risk_multiplier": round(self.rm.risk_multiplier, 4),
        }
