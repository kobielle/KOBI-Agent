"""
database.py — SQLite setup and all database operations.
Stores trades, daily stats, and agent state persistently.
"""

import sqlite3
import os
import logging
from datetime import datetime
from config import DB_PATH

logger = logging.getLogger(__name__)


def get_connection():
    """Return a SQLite connection with row_factory for dict-like access."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def initialize_database():
    """Create all tables if they don't exist yet."""
    conn = get_connection()
    cursor = conn.cursor()

    # ── Trade log ──────────────────────────────────────────────────────────
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp       TEXT NOT NULL,
            pair            TEXT NOT NULL,
            direction       TEXT NOT NULL,     -- BUY or SELL
            entry_price     REAL,
            exit_price      REAL,
            stake           REAL,
            profit_loss     REAL,
            confidence      REAL,
            entry_reason    TEXT,
            exit_reason     TEXT,
            contract_id     TEXT,
            status          TEXT DEFAULT 'OPEN'  -- OPEN, CLOSED, CANCELLED
        )
    """)

    # ── Daily statistics ───────────────────────────────────────────────────
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_stats (
            date            TEXT PRIMARY KEY,
            starting_balance REAL,
            ending_balance  REAL,
            total_trades    INTEGER DEFAULT 0,
            winning_trades  INTEGER DEFAULT 0,
            losing_trades   INTEGER DEFAULT 0,
            total_pnl       REAL DEFAULT 0.0,
            trading_halted  INTEGER DEFAULT 0  -- 1 if daily limit hit
        )
    """)

    # ── Agent state (balance, risk multiplier, etc.) ───────────────────────
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS agent_state (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    conn.commit()
    conn.close()
    logger.info("Database initialised at %s", DB_PATH)


# ─── Trade helpers ─────────────────────────────────────────────────────────

def log_trade_open(pair, direction, entry_price, stake, confidence, reason, contract_id):
    """Insert a new OPEN trade record and return its row id."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO trades
            (timestamp, pair, direction, entry_price, stake, confidence, entry_reason, contract_id, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')
    """, (datetime.utcnow().isoformat(), pair, direction, entry_price, stake, confidence, reason, contract_id))
    trade_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return trade_id


def log_trade_close(trade_id, exit_price, profit_loss, exit_reason):
    """Update a trade record when it closes."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE trades
        SET exit_price = ?, profit_loss = ?, exit_reason = ?, status = 'CLOSED'
        WHERE id = ?
    """, (exit_price, profit_loss, exit_reason, trade_id))
    conn.commit()
    conn.close()


def get_open_trades():
    """Return all currently open trades as a list of dicts."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM trades WHERE status = 'OPEN'")
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


def get_last_n_trades(n=20):
    """Return the last n closed trades."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM trades WHERE status = 'CLOSED' ORDER BY id DESC LIMIT ?", (n,)
    )
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


def get_trades_for_pair(pair, n=20):
    """Return last n closed trades for a specific pair."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM trades WHERE status = 'CLOSED' AND pair = ? ORDER BY id DESC LIMIT ?",
        (pair, n)
    )
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


def count_open_trades_for_pair(pair):
    """Return number of currently open trades for a given pair."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM trades WHERE status = 'OPEN' AND pair = ?", (pair,))
    count = cursor.fetchone()[0]
    conn.close()
    return count


def count_all_open_trades():
    """Return total number of currently open trades across all pairs."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM trades WHERE status = 'OPEN'")
    count = cursor.fetchone()[0]
    conn.close()
    return count


# ─── Daily stats helpers ───────────────────────────────────────────────────

def get_or_create_daily_stats(date_str, starting_balance):
    """Get today's stats row, creating it if it doesn't exist."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM daily_stats WHERE date = ?", (date_str,))
    row = cursor.fetchone()
    if not row:
        cursor.execute("""
            INSERT INTO daily_stats (date, starting_balance) VALUES (?, ?)
        """, (date_str, starting_balance))
        conn.commit()
        cursor.execute("SELECT * FROM daily_stats WHERE date = ?", (date_str,))
        row = cursor.fetchone()
    result = dict(row)
    conn.close()
    return result


def update_daily_stats(date_str, pnl_delta, is_win):
    """Increment daily P&L and trade counters."""
    conn = get_connection()
    cursor = conn.cursor()
    win_inc = 1 if is_win else 0
    loss_inc = 0 if is_win else 1
    cursor.execute("""
        UPDATE daily_stats
        SET total_trades = total_trades + 1,
            winning_trades = winning_trades + ?,
            losing_trades = losing_trades + ?,
            total_pnl = total_pnl + ?
        WHERE date = ?
    """, (win_inc, loss_inc, pnl_delta, date_str))
    conn.commit()
    conn.close()


def set_daily_halted(date_str):
    """Mark trading as halted for the day."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE daily_stats SET trading_halted = 1 WHERE date = ?", (date_str,)
    )
    conn.commit()
    conn.close()


# ─── Agent state helpers ───────────────────────────────────────────────────

def get_state(key, default=None):
    """Retrieve a persistent state value."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM agent_state WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else default


def set_state(key, value):
    """Persist a state value (upsert)."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO agent_state (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
    """, (key, str(value)))
    conn.commit()
    conn.close()
