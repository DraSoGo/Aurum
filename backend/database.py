"""
Database — SQLite only (WAL, busy_timeout=30s). Schema kept to the five tables
the rewrite actually uses: agents, signals, signal_sequence, positions,
profit_history.
"""
from __future__ import annotations

import logging
import os
import sqlite3

from config import DB_PATH

logger = logging.getLogger(__name__)


def get_db_connection() -> sqlite3.Connection:
    """Return a raw sqlite3.Connection with Row factory, WAL, 30s busy timeout."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_database() -> None:
    """Create the minimal schema used by AI-Trader."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS agents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                token TEXT,
                password_hash TEXT,
                reputation_score INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER UNIQUE NOT NULL,
                agent_id INTEGER NOT NULL,
                message_type TEXT NOT NULL,
                market TEXT NOT NULL,
                signal_type TEXT,
                symbol TEXT,
                side TEXT,
                entry_price REAL,
                exit_price REAL,
                quantity REAL,
                pnl REAL,
                title TEXT,
                content TEXT,
                timestamp INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                executed_at TEXT,
                FOREIGN KEY (agent_id) REFERENCES agents(id)
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS signal_sequence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                market TEXT NOT NULL DEFAULT 'forex',
                side TEXT NOT NULL,
                quantity REAL NOT NULL,
                entry_price REAL NOT NULL,
                current_price REAL,
                opened_at TEXT NOT NULL,
                FOREIGN KEY (agent_id) REFERENCES agents(id)
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS profit_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id INTEGER NOT NULL,
                total_value REAL NOT NULL,
                cash REAL NOT NULL,
                position_value REAL NOT NULL,
                profit REAL NOT NULL,
                recorded_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (agent_id) REFERENCES agents(id)
            )
        """)

        # Shadow decisions: every cycle's gate/LLM outcome is written here
        # regardless of whether a trade fires. Powers evidence-based tuning
        # of VOLUME_RATIO_MIN, MIN_CONFIDENCE_PCT, setup thresholds, etc.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS shadow_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_utc TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                cycle INTEGER,
                price REAL,
                gate_result TEXT,
                setup_type TEXT,
                bb_break TEXT,
                vol_ratio REAL,
                trend_long TEXT,
                trend_h4 TEXT,
                trend_d1 TEXT,
                rsi14 REAL,
                range_pos REAL,
                atr REAL,
                action TEXT,
                confidence INTEGER,
                reasoning TEXT,
                llm_called INTEGER DEFAULT 0,
                trade_fired INTEGER DEFAULT 0
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_shadow_ts ON shadow_decisions(ts_utc)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_shadow_agent ON shadow_decisions(agent_name)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_shadow_setup ON shadow_decisions(setup_type, trade_fired)")

        # Rotation: prune shadow rows older than SHADOW_RETENTION_DAYS to
        # stop unbounded growth. Default 90 days — longer than any rolling
        # lookback used by the self-tuner / analytics. Runs once on init;
        # if you want rolling rotation, call this from a scheduled task.
        try:
            retention_days = int(os.getenv("SHADOW_RETENTION_DAYS", "90"))
        except (TypeError, ValueError):
            retention_days = 90
        if retention_days > 0:
            cur.execute(
                "DELETE FROM shadow_decisions "
                "WHERE ts_utc < datetime('now', ?)",
                (f"-{retention_days} days",),
            )
            if cur.rowcount:
                logger.info(
                    "[DB] shadow rotation: pruned %d rows older than %d days",
                    cur.rowcount, retention_days,
                )

        # Seed signal_sequence with the current max signal_id so new inserts
        # never collide with existing rows in a restored DB.
        cur.execute("SELECT COALESCE(MAX(signal_id), 0) AS m FROM signals")
        max_sig = int(cur.fetchone()["m"] or 0)
        cur.execute("SELECT COALESCE(MAX(id), 0) AS m FROM signal_sequence")
        max_seq = int(cur.fetchone()["m"] or 0)
        if max_seq < max_sig:
            cur.executemany(
                "INSERT INTO signal_sequence DEFAULT VALUES",
                [()] * (max_sig - max_seq),
            )

        # Indexes
        cur.execute("CREATE INDEX IF NOT EXISTS idx_signals_agent ON signals(agent_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_signals_agent_message_type ON signals(agent_id, message_type)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_signals_message_type ON signals(message_type)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_signals_created_at ON signals(created_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_positions_agent ON positions(agent_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_profit_history_agent ON profit_history(agent_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_profit_history_recorded_at ON profit_history(recorded_at DESC)")

        conn.commit()
        logger.info("[DB] initialized at %s", DB_PATH)
    finally:
        conn.close()


def reserve_signal_id(cursor: sqlite3.Cursor) -> int:
    """Reserve a unique signal_id using signal_sequence as an auto-increment."""
    cursor.execute("INSERT INTO signal_sequence DEFAULT VALUES")
    return int(cursor.lastrowid)


def log_shadow_decision(row: dict) -> None:
    """Persist one cycle's gate/LLM outcome to shadow_decisions. Best-effort
    — logs and swallows errors so a DB glitch never stops trading."""
    try:
        conn = get_db_connection()
        try:
            conn.execute(
                """INSERT INTO shadow_decisions (
                    ts_utc, agent_name, cycle, price, gate_result,
                    setup_type, bb_break, vol_ratio, trend_long,
                    trend_h4, trend_d1, rsi14, range_pos, atr,
                    action, confidence, reasoning, llm_called, trade_fired
                ) VALUES (
                    :ts_utc, :agent_name, :cycle, :price, :gate_result,
                    :setup_type, :bb_break, :vol_ratio, :trend_long,
                    :trend_h4, :trend_d1, :rsi14, :range_pos, :atr,
                    :action, :confidence, :reasoning, :llm_called, :trade_fired
                )""",
                row,
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning("[DB] shadow_decisions insert failed: %s", e)
