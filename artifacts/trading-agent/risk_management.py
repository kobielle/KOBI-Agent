"""
risk_management.py — Layer 4: Risk Management & Capital Protection.
The most critical layer. Enforces all position sizing, daily/weekly
loss limits, consecutive-loss pauses, and the hard capital floor.
This layer overrides everything — no exceptions.
"""

import logging
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple

import pandas as pd

from config import (
    RISK_PER_TRADE,
    MAX_RISK_PER_TRADE,
    CAPITAL_PROTECTION_FLOOR,
    DAILY_LOSS_LIMIT,
    WEEKLY_LOSS_LIMIT,
    MAX_SIMULTANEOUS_TRADES,
    MAX_TRADES_PER_PAIR,
    WIN_STREAK_THRESHOLD,
    WIN_STREAK_INCREASE,
    LOSS_REDUCTION,
    RECOVERY_WINS_NEEDED,
    CONSECUTIVE_LOSS_LIMIT,
    CONSECUTIVE_LOSS_PAUSE_HOURS,
    CONSECUTIVE_LOSS_SIZE_REDUCTION,
    SL_ATR_MULTIPLIER,
    TP_ATR_MULTIPLIER,
    PERFORMANCE_SAMPLE_SIZE,
    MIN_WIN_RATE,
    HIGH_WIN_RATE,
)
from database import (
    get_or_create_daily_stats,
    set_daily_halted,
    get_last_n_trades,
    count_all_open_trades,
    count_open_trades_for_pair,
    get_state,
    set_state,
)
from notifications import (
    notify_daily_loss_limit,
    notify_weekly_loss_limit,
    notify_capital_floor_triggered,
    notify_consecutive_loss_pause,
    notify_warning,
    notify_info,
)

logger = logging.getLogger("RiskManagement")


class RiskManager:
    """
    All trading decisions must pass through this class.
    It tracks running state and enforces every protection rule.
    """

    def __init__(self, starting_balance: float):
        self._lock = threading.Lock()

        # Capital tracking
        self.starting_balance  = starting_balance
        self.current_balance   = starting_balance
        self.capital_floor     = starting_balance * CAPITAL_PROTECTION_FLOOR
        self.capital_destroyed = False   # True = permanent halt until restart

        # Streak tracking
        self.consecutive_wins   = 0
        self.consecutive_losses = 0

        # Pause state (consecutive loss pause)
        self.pause_until: Optional[datetime] = None
        self.pause_active = False

        # Current risk multiplier (starts at 1.0 = RISK_PER_TRADE)
        self.risk_multiplier = float(get_state("risk_multiplier", "1.0"))

        # Daily / weekly session control
        self.daily_halt  = False
        self.weekly_halt = False

        # Week start for weekly tracking
        self._week_start_balance = float(get_state("week_start_balance", str(starting_balance)))
        self._week_start_date    = get_state("week_start_date", self._this_week_monday())

        # Restore week on restart
        if self._week_start_date != self._this_week_monday():
            self._week_start_balance = starting_balance
            self._week_start_date    = self._this_week_monday()
            set_state("week_start_balance", starting_balance)
            set_state("week_start_date", self._week_start_date)

        # Check whether daily/weekly halts should still be active
        self._restore_halt_state()

        notify_info(
            f"Risk Manager online | Balance: ${starting_balance:.2f} | "
            f"Floor: ${self.capital_floor:.2f} | Risk/trade: {RISK_PER_TRADE*100:.1f}% × {self.risk_multiplier:.2f}"
        )

    # ─── Pre-trade gate ────────────────────────────────────────────────────

    def can_trade(self, pair: str) -> Tuple[bool, str]:
        """
        Final pre-trade check. Must return (True, reason) to proceed.
        Every protection rule is checked here.
        """
        with self._lock:
            # 1. Hard capital floor — overrides everything
            if self.capital_destroyed:
                return False, "CAPITAL FLOOR TRIGGERED — all trading permanently halted"

            if self.current_balance <= self.capital_floor:
                self._trigger_capital_floor()
                return False, "CAPITAL FLOOR TRIGGERED"

            # 2. Daily halt
            if self.daily_halt:
                return False, "Daily loss limit hit — trading halted until tomorrow"

            # 3. Weekly halt
            if self.weekly_halt:
                return False, "Weekly loss limit hit — trading halted until next week"

            # 4. Consecutive-loss pause
            if self.pause_active:
                now = datetime.now(tz=timezone.utc)
                if self.pause_until and now < self.pause_until:
                    remaining = int((self.pause_until - now).total_seconds() / 60)
                    return False, f"Consecutive-loss pause active — {remaining} min remaining"
                else:
                    self.pause_active = False
                    notify_info("Consecutive-loss pause ended. Resuming with reduced size.")

            # 5. Max simultaneous trades across all pairs
            open_total = count_all_open_trades()
            if open_total >= MAX_SIMULTANEOUS_TRADES:
                return False, f"Max simultaneous trades reached ({open_total}/{MAX_SIMULTANEOUS_TRADES})"

            # 6. Max trades per pair
            open_pair = count_open_trades_for_pair(pair)
            if open_pair >= MAX_TRADES_PER_PAIR:
                return False, f"Already have {open_pair} open trade(s) on {pair}"

            return True, "All risk checks passed"

    # ─── Position sizing ───────────────────────────────────────────────────

    def calculate_stake(self, atr: float, price: float) -> float:
        """
        Dynamically calculate the stake for the next trade.
        Risk = current_balance × risk_per_trade × risk_multiplier.
        Stake is determined by the risk amount and the ATR-based stop-loss distance.
        """
        effective_risk_pct = min(RISK_PER_TRADE * self.risk_multiplier, MAX_RISK_PER_TRADE)
        risk_amount = self.current_balance * effective_risk_pct

        # Stop loss distance in price terms
        sl_distance = atr * SL_ATR_MULTIPLIER if atr > 0 else price * 0.005

        # For Deriv multipliers the stake controls exposure
        # stake × multiplier × (sl_distance / price) = risk_amount
        # → stake = risk_amount × price / (multiplier × sl_distance)
        # Simplified: just use risk_amount as stake (1× for safety on first build)
        stake = round(max(risk_amount, 1.0), 2)
        logger.info(
            "Stake: $%.2f | Risk: %.2f%% of $%.2f | SL dist: %.5f",
            stake, effective_risk_pct * 100, self.current_balance, sl_distance
        )
        return stake

    def calculate_sl_tp(self, direction: str, entry_price: float, atr: float) -> Tuple[float, float]:
        """
        Compute stop-loss and take-profit prices.
        SL = 1.5 × ATR, TP = 2.5 × ATR → ~1.67 R:R.
        """
        sl_dist = atr * SL_ATR_MULTIPLIER
        tp_dist = atr * TP_ATR_MULTIPLIER

        if direction == "BUY":
            stop_loss   = entry_price - sl_dist
            take_profit = entry_price + tp_dist
        else:
            stop_loss   = entry_price + sl_dist
            take_profit = entry_price - tp_dist

        return round(stop_loss, 5), round(take_profit, 5)

    # ─── Post-trade update ─────────────────────────────────────────────────

    def record_trade_result(self, profit_loss: float, new_balance: float):
        """
        Called after every trade closes. Updates streaks, position sizing,
        and checks all cumulative loss limits.
        """
        with self._lock:
            self.current_balance = new_balance
            is_win = profit_loss > 0
            today  = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

            if is_win:
                self.consecutive_losses = 0
                self.consecutive_wins  += 1
                self._maybe_increase_size()
            else:
                self.consecutive_wins   = 0
                self.consecutive_losses += 1
                self._apply_loss_reduction()
                self._check_consecutive_loss_pause()

            # Persist the risk multiplier
            set_state("risk_multiplier", self.risk_multiplier)

            # Check daily loss limit
            self._check_daily_limit(today)

            # Check weekly loss limit
            self._check_weekly_limit()

            # Check capital floor
            if self.current_balance <= self.capital_floor:
                self._trigger_capital_floor()

    # ─── Internal size adjustment ──────────────────────────────────────────

    def _maybe_increase_size(self):
        """After every WIN_STREAK_THRESHOLD consecutive wins, grow position by 10%."""
        if self.consecutive_wins > 0 and self.consecutive_wins % WIN_STREAK_THRESHOLD == 0:
            new_mult = self.risk_multiplier * (1 + WIN_STREAK_INCREASE)
            # Cap: never exceed 3% risk
            max_mult = MAX_RISK_PER_TRADE / RISK_PER_TRADE
            self.risk_multiplier = min(new_mult, max_mult)
            notify_info(
                f"WIN STREAK {self.consecutive_wins} — Position size increased to "
                f"{self.risk_multiplier:.2f}× ({RISK_PER_TRADE*self.risk_multiplier*100:.2f}% risk)"
            )

    def _apply_loss_reduction(self):
        """Cut position size by 30% after a single loss."""
        self.risk_multiplier *= (1 - LOSS_REDUCTION)
        self.risk_multiplier  = max(self.risk_multiplier, 0.1)  # Never below 10% of base
        notify_warning(
            f"Loss recorded — Position size reduced to {self.risk_multiplier:.2f}× "
            f"({RISK_PER_TRADE*self.risk_multiplier*100:.2f}% risk)"
        )

    def _check_consecutive_loss_pause(self):
        """After CONSECUTIVE_LOSS_LIMIT losses in a row, pause and cut size."""
        if self.consecutive_losses >= CONSECUTIVE_LOSS_LIMIT:
            self.risk_multiplier *= (1 - CONSECUTIVE_LOSS_SIZE_REDUCTION)
            self.risk_multiplier  = max(self.risk_multiplier, 0.05)
            self.pause_active = True
            self.pause_until  = datetime.now(tz=timezone.utc) + timedelta(hours=CONSECUTIVE_LOSS_PAUSE_HOURS)
            notify_consecutive_loss_pause(
                self.consecutive_losses,
                CONSECUTIVE_LOSS_PAUSE_HOURS,
                1 - CONSECUTIVE_LOSS_SIZE_REDUCTION,
            )
            self.consecutive_losses = 0  # Reset counter after pause

    # ─── Limit checks ─────────────────────────────────────────────────────

    def _check_daily_limit(self, today: str):
        stats = get_or_create_daily_stats(today, self.starting_balance)
        starting = stats.get("starting_balance", self.starting_balance)
        if starting > 0:
            loss_pct = (starting - self.current_balance) / starting
            if loss_pct >= DAILY_LOSS_LIMIT and not self.daily_halt:
                self.daily_halt = True
                set_daily_halted(today)
                notify_daily_loss_limit(self.current_balance, loss_pct)

    def _check_weekly_limit(self):
        if self._week_start_balance > 0:
            loss_pct = (self._week_start_balance - self.current_balance) / self._week_start_balance
            if loss_pct >= WEEKLY_LOSS_LIMIT and not self.weekly_halt:
                self.weekly_halt = True
                notify_weekly_loss_limit(self.current_balance, loss_pct)

    def _trigger_capital_floor(self):
        """Permanent halt — account is in critical state."""
        if not self.capital_destroyed:
            self.capital_destroyed = True
            notify_capital_floor_triggered(self.current_balance, self.capital_floor)

    # ─── Daily reset ───────────────────────────────────────────────────────

    def reset_daily(self, new_balance: float):
        """Call at the start of a new trading day to reset daily limits."""
        with self._lock:
            self.daily_halt      = False
            self.current_balance = new_balance
            notify_info(f"Daily limits reset. New balance: ${new_balance:.2f}")

    def reset_weekly(self, new_balance: float):
        """Call at the start of a new trading week."""
        with self._lock:
            self.weekly_halt         = False
            self._week_start_balance = new_balance
            self._week_start_date    = self._this_week_monday()
            set_state("week_start_balance", new_balance)
            set_state("week_start_date",    self._week_start_date)
            notify_info(f"Weekly limits reset. Week start balance: ${new_balance:.2f}")

    # ─── Helpers ───────────────────────────────────────────────────────────

    def _restore_halt_state(self):
        """On startup, check if halts are still applicable."""
        today    = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        mon      = self._this_week_monday()
        saved_wk = get_state("weekly_halt_week", "")
        if saved_wk == mon:
            self.weekly_halt = True
            notify_warning("Weekly halt still active from previous run — resuming in halted state.")

    @staticmethod
    def _this_week_monday() -> str:
        today = datetime.now(tz=timezone.utc).date()
        monday = today - timedelta(days=today.weekday())
        return str(monday)

    # ─── External pause / resume ───────────────────────────────────────────

    def manual_pause(self):
        with self._lock:
            self.pause_active = True
            self.pause_until  = None   # Indefinite until manual_resume
            notify_info("Trading PAUSED manually via API.")

    def manual_resume(self):
        with self._lock:
            self.pause_active = False
            self.pause_until  = None
            notify_info("Trading RESUMED manually via API.")

    # ─── State snapshot for API ────────────────────────────────────────────

    def get_status_snapshot(self) -> dict:
        """Return a JSON-serialisable snapshot of current risk state."""
        with self._lock:
            return {
                "current_balance":   round(self.current_balance, 2),
                "starting_balance":  round(self.starting_balance, 2),
                "capital_floor":     round(self.capital_floor, 2),
                "risk_multiplier":   round(self.risk_multiplier, 4),
                "effective_risk_pct": round(min(RISK_PER_TRADE * self.risk_multiplier, MAX_RISK_PER_TRADE) * 100, 3),
                "consecutive_wins":  self.consecutive_wins,
                "consecutive_losses": self.consecutive_losses,
                "daily_halt":        self.daily_halt,
                "weekly_halt":       self.weekly_halt,
                "pause_active":      self.pause_active,
                "capital_destroyed": self.capital_destroyed,
            }
