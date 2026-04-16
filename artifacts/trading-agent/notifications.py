"""
notifications.py — Centralised logging and notification layer.
Telegram is built but uses a placeholder token — set TELEGRAM_ENABLED = True
in config.py and fill in your bot credentials to activate it.
"""

import logging
import requests
from datetime import datetime
from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_ENABLED
)

# ─── Console logger ────────────────────────────────────────────────────────

def setup_logging():
    """Configure the root logger with a clean, timestamped format."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Quieten noisy libraries
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

logger = logging.getLogger("Notifications")


# ─── Telegram helper ────────────────────────────────────────────────────────

def send_telegram(message: str):
    """
    Send a message via Telegram Bot API.
    Silently skipped if TELEGRAM_ENABLED is False in config.py.
    """
    if not TELEGRAM_ENABLED:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        resp = requests.post(url, json=payload, timeout=5)
        if not resp.ok:
            logger.warning("Telegram send failed: %s", resp.text)
    except Exception as exc:
        logger.warning("Telegram error: %s", exc)


# ─── Trade notifications ────────────────────────────────────────────────────

def notify_trade_open(pair, direction, entry_price, stake, confidence, reason):
    msg = (
        f"\n{'='*55}\n"
        f"  TRADE OPENED\n"
        f"  Pair       : {pair}\n"
        f"  Direction  : {direction}\n"
        f"  Entry      : {entry_price:.5f}\n"
        f"  Stake      : ${stake:.2f}\n"
        f"  Confidence : {confidence:.1f}/100\n"
        f"  Reason     : {reason}\n"
        f"  Time (UTC) : {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"{'='*55}"
    )
    logger.info(msg)
    send_telegram(f"📈 <b>TRADE OPENED</b>\n{pair} {direction} @ {entry_price:.5f}\nConfidence: {confidence:.0f}/100\n{reason}")


def notify_trade_close(pair, direction, entry_price, exit_price, profit_loss, reason):
    emoji = "✅" if profit_loss >= 0 else "❌"
    msg = (
        f"\n{'='*55}\n"
        f"  TRADE CLOSED {emoji}\n"
        f"  Pair       : {pair}\n"
        f"  Direction  : {direction}\n"
        f"  Entry      : {entry_price:.5f}  →  Exit: {exit_price:.5f}\n"
        f"  P&L        : ${profit_loss:+.2f}\n"
        f"  Reason     : {reason}\n"
        f"  Time (UTC) : {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"{'='*55}"
    )
    logger.info(msg)
    send_telegram(f"{emoji} <b>TRADE CLOSED</b>\n{pair} {direction}\nP&L: ${profit_loss:+.2f}\n{reason}")


# ─── Risk / limit warnings ─────────────────────────────────────────────────

def notify_daily_loss_limit(balance, loss_pct):
    msg = (
        f"\n{'!'*55}\n"
        f"  ⚠️  DAILY LOSS LIMIT HIT\n"
        f"  Loss today : {loss_pct*100:.1f}%\n"
        f"  Balance    : ${balance:.2f}\n"
        f"  Trading HALTED for the rest of the day.\n"
        f"{'!'*55}"
    )
    logger.warning(msg)
    send_telegram(f"⚠️ <b>DAILY LOSS LIMIT HIT</b>\nLoss: {loss_pct*100:.1f}% | Balance: ${balance:.2f}\nTrading halted for today.")


def notify_weekly_loss_limit(balance, loss_pct):
    msg = (
        f"\n{'!'*55}\n"
        f"  ⚠️  WEEKLY LOSS LIMIT HIT\n"
        f"  Loss this week : {loss_pct*100:.1f}%\n"
        f"  Balance        : ${balance:.2f}\n"
        f"  Trading HALTED for the rest of the week.\n"
        f"{'!'*55}"
    )
    logger.warning(msg)
    send_telegram(f"⚠️ <b>WEEKLY LOSS LIMIT HIT</b>\nLoss: {loss_pct*100:.1f}% | Balance: ${balance:.2f}\nTrading halted for the week.")


def notify_capital_floor_triggered(balance, floor):
    msg = (
        f"\n{'*'*55}\n"
        f"  🚨  CRITICAL — CAPITAL PROTECTION FLOOR TRIGGERED\n"
        f"  Balance : ${balance:.2f}  (floor: ${floor:.2f})\n"
        f"  ALL TRADING STOPPED PERMANENTLY.\n"
        f"  Restart the agent manually after reviewing the situation.\n"
        f"{'*'*55}"
    )
    logger.critical(msg)
    send_telegram(f"🚨 <b>CAPITAL FLOOR TRIGGERED</b>\nBalance: ${balance:.2f}\nALL TRADING STOPPED. Manual restart required.")


def notify_consecutive_loss_pause(count, pause_hours, new_size_pct):
    msg = (
        f"\n{'!'*55}\n"
        f"  ⚠️  CONSECUTIVE LOSSES — TRADING PAUSED\n"
        f"  Consecutive losses : {count}\n"
        f"  Pause duration     : {pause_hours} hour(s)\n"
        f"  Position size cut  : {new_size_pct*100:.0f}% of previous\n"
        f"{'!'*55}"
    )
    logger.warning(msg)
    send_telegram(f"⚠️ <b>CONSECUTIVE LOSS PAUSE</b>\n{count} losses in a row. Pausing {pause_hours}h, size cut to {new_size_pct*100:.0f}%.")


# ─── Agent thinking / selective entry ─────────────────────────────────────

def notify_skipped_trade(pair, reason):
    """Log when the agent decides NOT to trade — so the user can see it's thinking."""
    logger.info("  SKIP %s — %s", pair.ljust(12), reason)


def notify_session_info(session_name, is_active):
    status = "ACTIVE 🟢" if is_active else "INACTIVE ⚪"
    logger.info("  Session: %s — %s", session_name, status)


def notify_performance_report(report: str):
    logger.info("\n%s\n%s\n%s", "="*55, report, "="*55)
    send_telegram(f"📊 <b>Performance Report</b>\n{report}")


def notify_info(msg: str):
    logger.info("  %s", msg)


def notify_warning(msg: str):
    logger.warning("  %s", msg)
