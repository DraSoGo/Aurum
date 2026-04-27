"""
FastAPI routes — consolidated.

Endpoints:
  GET  /health
  POST /api/agents/login
  POST /api/signals                              (operation/open)
  POST /api/signals/{signal_id}/close            (exit_price + pnl)
  GET  /api/signals/feed
  GET  /api/ai-trader/status
  GET  /api/mt5/price
  GET  /api/mt5/positions
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from config import CORS_ORIGINS, MT5_SYMBOL
from database import get_db_connection, reserve_signal_id

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Password helpers (salted SHA-256)
# ---------------------------------------------------------------------------

def _hash_password(password: str) -> str:
    # NOTE: order is (password + salt) — matches the existing hashes stored by
    # win_trader/backend/utils.py so we can reuse its DB verbatim.
    salt = secrets.token_hex(16)
    digest = hashlib.sha256((password + salt).encode("utf-8")).hexdigest()
    return f"{salt}${digest}"


def _verify_password(password: str, stored: Optional[str]) -> bool:
    if not stored or "$" not in stored:
        return False
    salt, digest = stored.split("$", 1)
    candidate = hashlib.sha256((password + salt).encode("utf-8")).hexdigest()
    return hmac.compare_digest(candidate, digest)


def _extract_token(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return authorization.strip()


def _agent_by_token(token: Optional[str]) -> Optional[dict]:
    if not token:
        return None
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM agents WHERE token = ?", (token,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class AgentLogin(BaseModel):
    name: str
    password: str


class OperationSignal(BaseModel):
    """Open-trade signal posted by an AI trader agent."""
    market: str = "forex"
    symbol: str = Field(default_factory=lambda: MT5_SYMBOL)
    action: str                        # buy | sell | short | cover
    price: float
    quantity: float = 0.01
    content: Optional[str] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    executed_at: str = "now"


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def create_app() -> FastAPI:
    app = FastAPI(title="AI-Trader", version="1.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ---- Health --------------------------------------------------------

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    # ---- Agent login ---------------------------------------------------

    @app.post("/api/agents/login")
    async def agent_login(data: AgentLogin):
        """Password-based agent login. Auto-creates the agent row on first
        login so the AI traders can bring themselves up without a separate
        registration endpoint."""
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM agents WHERE name = ?", (data.name,))
            row = cur.fetchone()

            if row is None:
                # Auto-provision on first login
                pw_hash = _hash_password(data.password)
                token   = secrets.token_urlsafe(32)
                cur.execute(
                    "INSERT INTO agents (name, password_hash, token) VALUES (?, ?, ?)",
                    (data.name, pw_hash, token),
                )
                agent_id = int(cur.lastrowid)
                conn.commit()
                logger.info("[auth] auto-provisioned agent '%s' (id=%s)", data.name, agent_id)
                return {"agent_id": agent_id, "token": token}

            if not _verify_password(data.password, row["password_hash"]):
                raise HTTPException(status_code=401, detail="Invalid credentials")

            token = secrets.token_urlsafe(32)
            cur.execute("UPDATE agents SET token = ? WHERE id = ?", (token, row["id"]))
            conn.commit()
            return {"agent_id": int(row["id"]), "token": token}
        finally:
            conn.close()

    # ---- Open-trade signal --------------------------------------------

    @app.post("/api/signals")
    async def post_operation(
        data: OperationSignal,
        authorization: str = Header(None),
    ):
        """Agent posts an open-trade operation signal. The trade itself is
        already executed in MT5 by the agent — this endpoint records it."""
        agent = _agent_by_token(_extract_token(authorization))
        if not agent:
            raise HTTPException(status_code=401, detail="Invalid token")

        if data.quantity <= 0 or data.price <= 0:
            raise HTTPException(status_code=400, detail="Invalid price/quantity")

        if data.executed_at.lower() == "now":
            executed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            executed_at = data.executed_at

        try:
            timestamp = int(
                datetime.fromisoformat(executed_at.replace("Z", "+00:00")).timestamp()
            )
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid executed_at")

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        conn = get_db_connection()
        try:
            cur = conn.cursor()
            signal_id = reserve_signal_id(cur)
            cur.execute(
                """
                INSERT INTO signals (
                    signal_id, agent_id, message_type, market, signal_type,
                    symbol, side, entry_price, quantity, content,
                    timestamp, created_at, executed_at
                )
                VALUES (?, ?, 'operation', ?, 'realtime', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal_id, agent["id"], data.market, data.symbol,
                    data.action, float(data.price), float(data.quantity),
                    data.content, timestamp, now_iso, executed_at,
                ),
            )
            # Mirror into positions table (simple one-row-per-open model).
            side = "long" if data.action in ("buy", "cover") else "short"
            cur.execute(
                """
                INSERT INTO positions (
                    agent_id, symbol, market, side, quantity, entry_price,
                    current_price, opened_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent["id"], data.symbol, data.market, side,
                    float(data.quantity), float(data.price), float(data.price),
                    executed_at,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        return {
            "success":   True,
            "signal_id": signal_id,
            "symbol":    data.symbol,
            "action":    data.action,
            "price":     data.price,
        }

    # ---- Close signal (CRITICAL — AI agent calls this) -----------------

    @app.post("/api/signals/{signal_id}/close")
    async def close_signal(
        signal_id: int,
        exit_price: Optional[float] = None,
        pnl: Optional[float] = None,
        authorization: str = Header(None),
    ):
        """Record exit_price + pnl when a trade closes. This is the endpoint
        the AI agent calls — with retries + DB-write fallback — so it MUST
        be robust. Also updates the agent's reputation_score by +10/-5."""
        agent = _agent_by_token(_extract_token(authorization))
        if not agent:
            raise HTTPException(status_code=401, detail="Invalid token")

        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, agent_id, message_type, exit_price "
                "FROM signals WHERE signal_id = ?",
                (signal_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Signal not found")
            if row["agent_id"] != agent["id"]:
                raise HTTPException(status_code=403, detail="Not your signal")

            updates: list[str] = []
            params: list = []
            if exit_price is not None:
                updates.append("exit_price = ?")
                params.append(float(exit_price))
            if pnl is not None:
                updates.append("pnl = ?")
                params.append(float(pnl))

            if not updates:
                return {"success": True, "signal_id": signal_id,
                        "message": "Nothing to update"}

            params.append(signal_id)
            cur.execute(
                f"UPDATE signals SET {', '.join(updates)} WHERE signal_id = ?",
                params,
            )

            # Reputation
            if pnl is not None and pnl != 0:
                rep_delta = 10 if pnl > 0 else -5
                cur.execute(
                    "UPDATE agents SET reputation_score = MAX(0, reputation_score + ?) "
                    "WHERE id = ?",
                    (rep_delta, agent["id"]),
                )

            # Drop mirror position
            cur.execute(
                "DELETE FROM positions WHERE agent_id = ? AND symbol = ?",
                (agent["id"], MT5_SYMBOL),
            )
            conn.commit()
        finally:
            conn.close()

        return {
            "success":    True,
            "signal_id":  signal_id,
            "exit_price": exit_price,
            "pnl":        pnl,
        }

    # ---- Signal feed ---------------------------------------------------

    @app.get("/api/signals/feed")
    async def feed(
        message_type: Optional[str] = None,
        market: Optional[str] = None,
        limit: int = 20,
    ):
        limit = max(1, min(int(limit), 100))
        conds: list[str] = []
        params: list = []
        if message_type:
            conds.append("s.message_type = ?")
            params.append(message_type)
        if market:
            conds.append("s.market = ?")
            params.append(market)
        where = " AND ".join(conds) if conds else "1=1"
        params.append(limit)

        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                f"""
                SELECT s.signal_id, s.agent_id, a.name AS agent_name,
                       s.message_type, s.market, s.symbol, s.side,
                       s.entry_price, s.exit_price, s.quantity, s.pnl,
                       s.content, s.created_at, s.executed_at
                FROM signals s
                JOIN agents a ON a.id = s.agent_id
                WHERE {where}
                ORDER BY s.created_at DESC
                LIMIT ?
                """,
                params,
            )
            rows = [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

        return {"signals": rows, "count": len(rows)}

    # ---- AI trader status ---------------------------------------------

    @app.get("/api/ai-trader/status")
    async def ai_status():
        """Latest decision / position / P&L snapshot per agent."""
        from ai_agent import get_all_status
        status = get_all_status()

        # Overlay cumulative P&L from DB
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT a.name AS agent_name,
                       COALESCE(SUM(s.pnl), 0)        AS cumulative_pnl,
                       COUNT(CASE WHEN s.pnl IS NOT NULL THEN 1 END) AS closed_trades
                FROM agents a
                LEFT JOIN signals s
                  ON s.agent_id = a.id AND s.message_type = 'operation'
                GROUP BY a.id, a.name
                """
            )
            rollup = {r["agent_name"]: dict(r) for r in cur.fetchall()}
        finally:
            conn.close()

        for key in ("claude_trader",):
            entry = status.get(key) or {}
            roll = rollup.get(entry.get("agent_name") or "", {})
            entry["cumulative_pnl"] = float(roll.get("cumulative_pnl", 0.0))
            entry["closed_trades"]  = int(roll.get("closed_trades", 0))
        return status

    # ---- MT5 price -----------------------------------------------------

    @app.get("/api/mt5/price")
    async def mt5_price():
        from mt5_trader import get_current_price
        price = await get_current_price()
        return {"symbol": MT5_SYMBOL, "price": price}

    # ---- MT5 positions -------------------------------------------------

    @app.get("/api/mt5/positions")
    async def mt5_positions():
        from mt5_trader import get_positions_list_sync
        import asyncio as _asyncio
        # to_thread is the 3.10+ replacement for get_event_loop().run_in_executor
        # — no DeprecationWarning under Python 3.13.
        positions = await _asyncio.to_thread(get_positions_list_sync, None)
        return {"positions": positions, "count": len(positions)}

    return app
