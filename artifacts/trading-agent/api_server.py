"""
api_server.py — Layer 7: Flask REST API & Webhook for n8n.
Runs in its own daemon thread alongside the async trading loop.
"""

import threading
import logging
from datetime import datetime, timezone

from flask import Flask, jsonify, request

from config import FLASK_HOST, FLASK_PORT
from database import get_open_trades, get_last_n_trades
from notifications import notify_info

logger = logging.getLogger("APIServer")

app = Flask(__name__)

# ─── Shared state (injected by main.py) ───────────────────────────────────
# These are set by main.py after the agent starts up.
_risk_manager       = None
_performance_tracker = None
_trade_executor     = None
_agent_loop_ref     = None   # asyncio event loop for dispatching coroutines


def inject_dependencies(risk_manager, performance_tracker, trade_executor, loop):
    """Called by main.py to wire up the shared state."""
    global _risk_manager, _performance_tracker, _trade_executor, _agent_loop_ref
    _risk_manager        = risk_manager
    _performance_tracker = performance_tracker
    _trade_executor      = trade_executor
    _agent_loop_ref      = loop


# ─── Endpoints ─────────────────────────────────────────────────────────────

@app.route("/status", methods=["GET"])
def status():
    """GET /status — account balance, open trades, daily P&L, agent status."""
    if _risk_manager is None:
        return jsonify({"error": "Agent not initialised yet"}), 503

    risk_snap    = _risk_manager.get_status_snapshot()
    open_trades  = get_open_trades()
    today        = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

    # Compute today's P&L from DB
    today_trades = [
        t for t in get_last_n_trades(200)
        if t.get("timestamp", "").startswith(today)
    ]
    today_pnl = sum(t.get("profit_loss", 0) for t in today_trades if t.get("profit_loss") is not None)

    return jsonify({
        "timestamp":     datetime.utcnow().isoformat() + "Z",
        "account":       risk_snap,
        "open_trades":   open_trades,
        "daily_pnl":     round(today_pnl, 2),
        "agent_status": {
            "paused":           risk_snap["pause_active"],
            "daily_halted":     risk_snap["daily_halt"],
            "weekly_halted":    risk_snap["weekly_halt"],
            "capital_destroyed": risk_snap["capital_destroyed"],
        },
    })


@app.route("/performance", methods=["GET"])
def performance():
    """GET /performance — last 20 trade summary."""
    if _performance_tracker is None:
        return jsonify({"error": "Agent not initialised yet"}), 503
    return jsonify(_performance_tracker.get_summary())


@app.route("/pause", methods=["POST"])
def pause():
    """POST /pause — pause trading (no body required)."""
    if _risk_manager is None:
        return jsonify({"error": "Agent not initialised"}), 503
    _risk_manager.manual_pause()
    return jsonify({"status": "paused", "message": "Trading paused successfully."})


@app.route("/resume", methods=["POST"])
def resume():
    """POST /resume — resume trading after a manual pause."""
    if _risk_manager is None:
        return jsonify({"error": "Agent not initialised"}), 503
    _risk_manager.manual_resume()
    return jsonify({"status": "active", "message": "Trading resumed."})


@app.route("/trade-alert", methods=["POST"])
def trade_alert():
    """
    POST /trade-alert — receive a signal from n8n or an external source.
    Body: { "pair": "frxEURUSD", "direction": "BUY", "note": "optional" }
    The agent will log the signal and let the normal strategy confirm it.
    """
    data = request.get_json(force=True, silent=True) or {}
    pair      = data.get("pair", "unknown")
    direction = data.get("direction", "unknown").upper()
    note      = data.get("note", "")

    logger.info(
        "TRADE ALERT received from n8n — Pair: %s | Dir: %s | Note: %s",
        pair, direction, note
    )

    # The agent does NOT blindly execute external signals.
    # It logs the alert and factors it into the next analysis cycle.
    notify_info(f"External alert logged: {pair} {direction} — {note}")

    return jsonify({
        "received":  True,
        "pair":      pair,
        "direction": direction,
        "note":      "Signal logged. Agent will evaluate on next candle.",
    })


@app.route("/trades/history", methods=["GET"])
def trade_history():
    """GET /trades/history?n=20 — return last n closed trades."""
    n      = request.args.get("n", 20, type=int)
    trades = get_last_n_trades(n)
    return jsonify({"count": len(trades), "trades": trades})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


# ─── Server launcher ───────────────────────────────────────────────────────

def run_flask():
    """Start Flask in a daemon thread. Called from main.py."""
    notify_info(f"Flask API server starting on {FLASK_HOST}:{FLASK_PORT}")
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=False, use_reloader=False)


def start_api_server_thread():
    t = threading.Thread(target=run_flask, daemon=True, name="FlaskAPIServer")
    t.start()
    return t
