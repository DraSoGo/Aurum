"""
Risk manager — pre-entry gates and dynamic position sizing.

All thresholds are read from env on each call so the self-tuner can adjust
them at runtime without restarting the process.

Gates (in order):
    1. Daily loss cap              DAILY_LOSS_LIMIT_USD    (USD, per agent, UTC day)
    2. Consecutive-loss cooldown   MAX_CONSEC_LOSSES / LOSS_COOLDOWN_MIN
    3. Per-trade cooldown          TRADE_COOLDOWN_SECS
    4. Spread gate                 MAX_SPREAD_POINTS
    5. Max SL distance clamp       MAX_SL_POINTS
    6. Minimum reward:risk         MIN_RR

Dynamic sizing:
    compute_lot(balance, sl_distance_usd) → lot size such that a stop-out
    costs `RISK_PCT_PER_TRADE` of the account. Falls back to LOT_SIZE.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional, Tuple

from database import get_db_connection

logger = logging.getLogger(__name__)

# XAUUSD: 1 lot = 100oz → $1 move on price = $100 P&L per lot
CONTRACT_SIZE = 100


# ---------------------------------------------------------------------------
# Env helpers (read-each-call so self_tuner updates take effect immediately)
# ---------------------------------------------------------------------------

def _fget(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)) or default)
    except Exception:
        return default


def _iget(key: str, default: int) -> int:
    try:
        return int(float(os.getenv(key, str(default)) or default))
    except Exception:
        return default


def _today_utc_start_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


# ---------------------------------------------------------------------------
# DB reads
# ---------------------------------------------------------------------------

def get_daily_pnl(agent_id: int) -> float:
    """Realized P&L from closed trades since UTC midnight for this agent."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COALESCE(SUM(pnl), 0) AS total FROM signals "
            "WHERE agent_id = ? AND pnl IS NOT NULL AND executed_at >= ?",
            (agent_id, _today_utc_start_iso()),
        )
        row = cur.fetchone()
        return float(row["total"] or 0.0)
    finally:
        conn.close()


def get_rolling_pnl(agent_id: int, days: int = 7) -> float:
    """Realized P&L from closed trades over the last ``days`` UTC days.

    Used by the weekly drawdown gate — daily cap alone can't catch a
    5-trading-day losing streak that each stops just shy of the daily limit.
    """
    from datetime import timedelta
    conn = get_db_connection()
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=int(days))).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        cur = conn.cursor()
        cur.execute(
            "SELECT COALESCE(SUM(pnl), 0) AS total FROM signals "
            "WHERE agent_id = ? AND pnl IS NOT NULL AND executed_at >= ?",
            (agent_id, cutoff),
        )
        row = cur.fetchone()
        return float(row["total"] or 0.0)
    finally:
        conn.close()


def get_consecutive_losses(agent_id: int, lookback: int = 20) -> int:
    """Count leading streak of losses in the agent's most recent closed trades."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT pnl FROM signals "
            "WHERE agent_id = ? AND pnl IS NOT NULL "
            "ORDER BY id DESC LIMIT ?",
            (agent_id, int(lookback)),
        )
        streak = 0
        for row in cur.fetchall():
            if float(row["pnl"] or 0.0) < 0:
                streak += 1
            else:
                break
        return streak
    finally:
        conn.close()


def get_last_close_ts(agent_id: int) -> Optional[float]:
    """Unix timestamp of the most recent closed trade for this agent."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT executed_at FROM signals "
            "WHERE agent_id = ? AND pnl IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (agent_id,),
        )
        row = cur.fetchone()
        if not row or not row["executed_at"]:
            return None
        try:
            return datetime.fromisoformat(
                row["executed_at"].replace("Z", "+00:00")
            ).timestamp()
        except Exception:
            return None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Spread check (polls MT5 tick)
# ---------------------------------------------------------------------------

def check_spread(symbol: Optional[str] = None) -> Tuple[bool, float, str]:
    """Return (ok, spread_points, note). ok=False blocks entry."""
    max_spread = _fget("MAX_SPREAD_POINTS", 80.0)
    if max_spread <= 0:
        return True, 0.0, "spread check disabled"
    try:
        import MetaTrader5 as mt5
        from mt5_trader import _ensure_mt5, _sym
        if not _ensure_mt5():
            return True, 0.0, "mt5 unavailable — skip"
        sym = _sym(symbol)
        info = mt5.symbol_info(sym)
        tick = mt5.symbol_info_tick(sym)
        if not info or not tick:
            return True, 0.0, "no tick — skip"
        point = float(info.point or 0.01)
        spread_pts = (tick.ask - tick.bid) / point if point > 0 else 0.0
        if spread_pts > max_spread:
            return False, spread_pts, f"spread {spread_pts:.0f}pt > limit {max_spread:.0f}pt"
        return True, spread_pts, f"spread {spread_pts:.0f}pt"
    except Exception as exc:
        logger.warning("[risk] spread check failed: %s", exc)
        return True, 0.0, "spread check errored"


# ---------------------------------------------------------------------------
# MT5 balance
# ---------------------------------------------------------------------------

def get_mt5_balance() -> Optional[float]:
    try:
        from mt5_trader import _ensure_mt5
        if not _ensure_mt5():
            return None
        import MetaTrader5 as mt5
        info = mt5.account_info()
        return float(info.balance) if info else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------

def compute_lot(
    balance: Optional[float],
    sl_distance_usd: float,
    risk_pct_override: Optional[float] = None,
) -> float:
    """Risk-based position sizing. Returns a lot size such that a stop-out
    costs approximately ``RISK_PCT_PER_TRADE`` of ``balance``.

    ``risk_pct_override`` lets the caller pre-scale the risk budget (e.g.
    by confidence) so sizing responds to conviction even when MIN/MAX lot
    bounds would otherwise clip the usual post-multiplier. When omitted,
    falls back to the env-level ``RISK_PCT_PER_TRADE``.

    Falls back to ``LOT_SIZE`` when any input is missing/invalid.
    """
    lot_default = _fget("LOT_SIZE", 0.01)
    risk_pct    = float(risk_pct_override) if risk_pct_override is not None else _fget("RISK_PCT_PER_TRADE", 0.0)
    min_lot     = _fget("MIN_LOT_SIZE", 0.01)
    max_lot     = _fget("MAX_LOT_SIZE", 1.0)

    if risk_pct <= 0 or not balance or balance <= 0 or sl_distance_usd <= 0:
        return lot_default

    risk_usd = balance * risk_pct
    lots = risk_usd / (sl_distance_usd * CONTRACT_SIZE)
    lots = max(min_lot, min(round(lots, 2), max_lot))
    return lots


# ---------------------------------------------------------------------------
# Entry gates — split into two phases to save LLM tokens
# ---------------------------------------------------------------------------
# Phase 1 (check_preflight): decision-INDEPENDENT gates that can run BEFORE
# the Claude API call. If any fails, we skip the LLM entirely → $0 cost.
#   - Daily loss cap       (DB stat only)
#   - Consecutive-loss     (DB stat only)
#   - Per-trade cooldown   (DB stat only)
#   - Spread gate          (MT5 tick only)
#
# Phase 2 (check_postdecision): decision-DEPENDENT gates that can only run
# AFTER Claude has produced a {action, sl, tp, ...}. LLM tokens are already
# spent, but we still reject bad setups before the MT5 order:
#   - Max SL distance clamp (mutates decision.stop_loss)
#   - Minimum reward:risk   (after SL clamp)
# ---------------------------------------------------------------------------

def check_preflight(agent_id: int) -> Tuple[bool, str]:
    """Run decision-independent gates. Call BEFORE the LLM request.

    Returns (ok, reason). ok=False ⇒ skip the whole cycle.
    """
    now_ts = datetime.now(timezone.utc).timestamp()

    # 1. Daily loss cap
    daily_limit = _fget("DAILY_LOSS_LIMIT_USD", 0.0)
    if daily_limit > 0 and agent_id:
        pnl_today = get_daily_pnl(agent_id)
        if pnl_today <= -abs(daily_limit):
            return False, (
                f"daily loss cap {pnl_today:+.2f} <= -{daily_limit:.2f}"
            )

    # 1b. Weekly drawdown cap — protects against N successive days each
    # stopping just short of DAILY_LOSS_LIMIT. Disabled when env is unset
    # or zero. Intent: DAILY_LOSS_LIMIT bounds one day's bleed; this
    # bounds the 7-day rolling bleed so a slow drift can't blow the acct.
    weekly_limit = _fget("WEEKLY_LOSS_LIMIT_USD", 0.0)
    weekly_days  = _fget("WEEKLY_LOSS_LIMIT_DAYS", 7.0)
    if weekly_limit > 0 and agent_id:
        pnl_week = get_rolling_pnl(agent_id, days=int(weekly_days))
        if pnl_week <= -abs(weekly_limit):
            return False, (
                f"{int(weekly_days)}-day drawdown {pnl_week:+.2f} "
                f"<= -{weekly_limit:.2f}"
            )

    # 2. Consecutive-loss cooldown
    max_consec   = _iget("MAX_CONSEC_LOSSES", 0)
    cooldown_min = _fget("LOSS_COOLDOWN_MIN", 0.0)
    if max_consec > 0 and cooldown_min > 0 and agent_id:
        consec = get_consecutive_losses(agent_id)
        if consec >= max_consec:
            last_close = get_last_close_ts(agent_id)
            if last_close:
                age_min = (now_ts - last_close) / 60.0
                if age_min < cooldown_min:
                    return False, (
                        f"{consec} losses in a row — cooling down "
                        f"{age_min:.1f}/{cooldown_min:.0f}min"
                    )

    # 3. Per-trade cooldown
    per_cool = _fget("TRADE_COOLDOWN_SECS", 0.0)
    if per_cool > 0 and agent_id:
        last_close = get_last_close_ts(agent_id)
        if last_close and (now_ts - last_close) < per_cool:
            left = per_cool - (now_ts - last_close)
            return False, f"per-trade cooldown ({left:.0f}s left)"

    # 4. Spread gate — the big token saver (polls MT5 tick, no DB, no LLM)
    ok_s, spread_pts, note = check_spread()
    if not ok_s:
        return False, note

    return True, f"preflight ok (spread {spread_pts:.0f}pt)"


def check_postdecision(
    agent_id: int, decision: dict, price: float,
    atr: Optional[float] = None,
) -> Tuple[bool, str, dict]:
    """Run decision-dependent gates. Call AFTER the LLM decision is known.

    Mutations applied to ``decision`` in-place:
      - stop_loss clamped to adaptive-or-static max distance from entry
    """
    action = str(decision.get("action", "hold")).lower()
    if action not in ("buy", "sell"):
        return True, "hold — no gate", decision

    # 5. Clamp max SL distance — adaptive when ATR available.
    # Formula: cap = max(SL_FLOOR_POINTS, atr * SL_ATR_MULT)
    # - Low-vol regime: cap tightens toward floor -> better RR.
    # - High-vol regime: cap widens -> fewer noise stop-outs.
    # Falls back to static MAX_SL_POINTS when ATR is missing or
    # adaptive feature disabled (SL_ATR_MULT<=0).
    sl_floor    = _fget("SL_FLOOR_POINTS", 3.0)
    sl_atr_mult = _fget("SL_ATR_MULT", 0.0)
    static_cap  = _fget("MAX_SL_POINTS", 0.0)
    if atr and atr > 0 and sl_atr_mult > 0:
        max_sl_dist = max(sl_floor, atr * sl_atr_mult)
        logger.info("[risk] adaptive SL cap: %.2f (atr=%.2f x%.2f, floor=%.2f)",
                    max_sl_dist, atr, sl_atr_mult, sl_floor)
    else:
        max_sl_dist = static_cap
    sl = decision.get("stop_loss")
    if sl is not None and max_sl_dist > 0 and price > 0:
        if action == "buy":
            floor = round(price - max_sl_dist, 2)
            if sl < floor:
                logger.info("[risk] SL clamped (buy): %.2f -> %.2f (max dist %.2f)",
                            sl, floor, max_sl_dist)
                decision["stop_loss"] = floor
        else:
            ceil = round(price + max_sl_dist, 2)
            if sl > ceil:
                logger.info("[risk] SL clamped (sell): %.2f -> %.2f (max dist %.2f)",
                            sl, ceil, max_sl_dist)
                decision["stop_loss"] = ceil

    # 6. Minimum reward:risk (after SL clamp)
    min_rr = _fget("MIN_RR", 0.0)
    sl = decision.get("stop_loss")
    tp = decision.get("take_profit")
    if min_rr > 0 and sl and tp and price > 0:
        if action == "buy":
            risk   = price - sl
            reward = tp - price
        else:
            risk   = sl - price
            reward = price - tp
        if risk > 0 and reward > 0:
            rr = reward / risk
            if rr < min_rr:
                return False, (
                    f"reward:risk {rr:.2f} < min {min_rr:.2f} "
                    f"(risk=${risk:.2f} reward=${reward:.2f})"
                ), decision

    return True, "postdecision ok", decision


# Backward-compat wrapper (runs both phases in order). Kept for any external
# caller, but ai_agent.py now uses the split form directly.
def check_entry(agent_id: int, decision: dict, price: float) -> Tuple[bool, str, dict]:
    ok, reason = check_preflight(agent_id)
    if not ok:
        return False, reason, decision
    return check_postdecision(agent_id, decision, price)
