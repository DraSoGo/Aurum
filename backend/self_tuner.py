"""
Self-Tuning Layer
=================
Rule-based runtime parameter tuner. Every SELF_TUNER_INTERVAL_MINUTES it
reads the last SELF_TUNER_WINDOW closed signals from the DB, computes
rolling performance, and adjusts os.environ variables that the trading
loop already reads each cycle via ``os.getenv(...)``.

No ML — just deterministic rules with hard bounds. Goal: keep the system
on a sensible path as market conditions change, without human edits.

What it tunes
-------------
- MAX_SL_POINTS          widen after losing streaks, tighten after wins.
- TRAIL_STAGNATION_SECS  raise if stagnation exits winners too early;
                         lower if losers eat the full SL.
- LOT_SIZE               scale up on positive payoff, down on drawdown.
- MIN_RR                 raise if expectancy negative & payoff poor;
                         lower if expectancy strong but too many
                         borderline setups get rejected.
- Per-agent kill switch  add an agent name fragment to
                         SELF_TUNER_DISABLED_AGENTS (ai_agent reads this
                         at the top of each cycle).

Stats tracked:
  win_rate, avg_win, avg_loss, total_pnl, big_losses, scratches,
  expectancy (E[$]/trade), profit_factor (gross_W / |gross_L|),
  payoff (avg_W / |avg_L|).

All changes clamped to PARAM_BOUNDS and logged to server.log.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Bounds ──────────────────────────────────────────────────────────────
PARAM_BOUNDS: Dict[str, tuple] = {
    "MAX_SL_POINTS":         (1.0, 8.0),
    "SL_FLOOR_POINTS":       (1.0, 8.0),   # adaptive-SL floor; tuned in lieu of MAX_SL when SL_ATR_MULT>0
    "TRAIL_STAGNATION_SECS": (60, 600),
    "TRAIL_STAGNATION_ATR":  (0.02, 0.15),
    "TRAIL_BE_ATR":          (0.05, 0.30),
    "TRAIL_MICRO_LOCK_PTS":  (0.05, 0.50),
    "TRAIL_HARD_CLOSE_SECS": (300, 3600),
    "LOT_SIZE":              (0.01, 0.10),
    "MIN_RR":                (1.0, 3.0),
}

# Per-run ratchet caps — maximum fractional change allowed in a single
# tuning pass, to stop runaway drift when the same dataset repeatedly
# triggers a rule. Rules compute their ideal target, then _set_env()
# clamps movement to [cur × (1-cap), cur × (1+cap)] before bounds.
PARAM_STEP_CAP: Dict[str, float] = {
    "MAX_SL_POINTS":         0.15,   # <=15% change per run
    "SL_FLOOR_POINTS":       0.15,
    "TRAIL_STAGNATION_SECS": 0.15,
    "TRAIL_BE_ATR":          0.15,
    "TRAIL_MICRO_LOCK_PTS":  0.15,
    "TRAIL_HARD_CLOSE_SECS": 0.20,
    "LOT_SIZE":              0.10,
    "MIN_RR":                0.10,
}

# Fingerprint of the last dataset the tuner acted on. If the next run
# sees an identical set of closed trades (no new data), we skip rule
# application entirely — otherwise fixed stats compound every cycle.
_LAST_FINGERPRINT: Optional[str] = None


# ── DB reads ────────────────────────────────────────────────────────────

def _get_recent_closed_trades(limit: int = 50) -> List[dict]:
    """Pull the last N closed signals (exit_price + pnl set).

    Respects SELF_TUNER_SINCE_ISO (ISO-8601) — converted to epoch seconds
    so it compares correctly against the integer `timestamp` column, with
    a safety-net match against the ISO `executed_at` column.
    """
    from database import get_db_connection

    since_iso = os.getenv("SELF_TUNER_SINCE_ISO", "").strip()
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        if since_iso:
            try:
                cutoff_iso = since_iso.rstrip("Z")
                cutoff_epoch = int(
                    _dt.datetime.fromisoformat(cutoff_iso)
                       .replace(tzinfo=_dt.timezone.utc).timestamp()
                )
            except Exception:
                cutoff_epoch = 0
            cur.execute(
                """
                SELECT id, agent_id, symbol, side, entry_price, exit_price,
                       quantity, pnl, executed_at, timestamp
                FROM signals
                WHERE exit_price IS NOT NULL
                  AND pnl IS NOT NULL
                  AND message_type = 'operation'
                  AND (timestamp >= ? OR executed_at >= ?)
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (cutoff_epoch, since_iso, int(limit)),
            )
        else:
            cur.execute(
                """
                SELECT id, agent_id, symbol, side, entry_price, exit_price,
                       quantity, pnl, executed_at, timestamp
                FROM signals
                WHERE exit_price IS NOT NULL
                  AND pnl IS NOT NULL
                  AND message_type = 'operation'
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (int(limit),),
            )
        rows = cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _get_setup_stats(lookback_days: int = 14) -> Dict[str, Dict[str, Any]]:
    """Join shadow_decisions.setup_type with signals.pnl (matched loosely
    by timestamp proximity + agent) to compute per-setup WR/expectancy.

    We match each `trade_fired=1` shadow row to the closed signal whose
    `timestamp` lands closest to the shadow `ts_utc` for the same agent,
    within a 30-min window. This is approximate (ordering noise on busy
    agents) but good enough to spot a 15% WR setup type vs a 60% WR one.
    """
    from database import get_db_connection
    cutoff_epoch = int(
        _dt.datetime.now(_dt.timezone.utc).timestamp() - lookback_days * 86400
    )
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT s.setup_type, s.agent_name, s.ts_utc,
                   (SELECT sig.pnl FROM signals sig
                    JOIN agents ag ON ag.id = sig.agent_id
                    WHERE sig.pnl IS NOT NULL
                      AND sig.message_type='operation'
                      AND ag.name = s.agent_name
                      AND sig.timestamp BETWEEN
                          strftime('%s', s.ts_utc) - 60
                      AND strftime('%s', s.ts_utc) + 1800
                    ORDER BY ABS(sig.timestamp - strftime('%s', s.ts_utc)) ASC
                    LIMIT 1) AS pnl
            FROM shadow_decisions s
            WHERE s.trade_fired = 1
              AND s.setup_type IS NOT NULL
              AND strftime('%s', s.ts_utc) >= ?
            """,
            (cutoff_epoch,),
        )
        by_setup: Dict[str, List[float]] = {}
        for r in cur.fetchall():
            setup = r["setup_type"]
            pnl = r["pnl"]
            if pnl is None or setup is None:
                continue
            by_setup.setdefault(setup, []).append(float(pnl))
        out: Dict[str, Dict[str, Any]] = {}
        for setup, pnls in by_setup.items():
            if not pnls:
                continue
            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p < 0]
            wr = len(wins) / len(pnls)
            avg_w = sum(wins) / len(wins) if wins else 0.0
            avg_l = sum(losses) / len(losses) if losses else 0.0
            exp = wr * avg_w + (1 - wr) * avg_l
            out[setup] = {
                "n":          len(pnls),
                "win_rate":   round(wr, 3),
                "expectancy": round(exp, 2),
                "total":      round(sum(pnls), 2),
            }
        return out
    except Exception as e:
        logger.warning("[SelfTuner] setup stats query failed: %s", e)
        return {}
    finally:
        conn.close()


def _get_agent_names() -> Dict[int, str]:
    from database import get_db_connection
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, name FROM agents")
        return {r["id"]: r["name"] for r in cur.fetchall()}
    finally:
        conn.close()


def _mt5_balance() -> Optional[float]:
    try:
        from mt5_trader import _ensure_mt5
        if not _ensure_mt5():
            return None
        import MetaTrader5 as mt5
        info = mt5.account_info()
        return float(info.balance) if info else None
    except Exception:
        return None


# ── Stats ───────────────────────────────────────────────────────────────

def _compute_stats(trades: List[dict]) -> Dict[str, Any]:
    if not trades:
        return {"n": 0}
    wins   = [t for t in trades if (t.get("pnl") or 0.0) > 0]
    losses = [t for t in trades if (t.get("pnl") or 0.0) < 0]
    total  = sum(t.get("pnl") or 0.0 for t in trades)
    win_rate = len(wins) / len(trades)
    avg_win  = (sum(t["pnl"] for t in wins)   / len(wins))   if wins   else 0.0
    avg_loss = (sum(t["pnl"] for t in losses) / len(losses)) if losses else 0.0
    big_losses = [t for t in losses if t["pnl"] < -5.0]
    scratches  = [t for t in trades if -1.0 < (t.get("pnl") or 0.0) < 1.0]

    # Expectancy = average $ earned per trade (robust blend of WR + size).
    #   E = WR × avgWin + (1-WR) × avgLoss     (avg_loss is negative)
    # With the current 8-trade IC Markets sample (WR=0.75, aW=$22.65,
    # aL=-$3.40):  E = 0.75*22.65 + 0.25*-3.40 = +$16.14 / trade.
    expectancy = win_rate * avg_win + (1 - win_rate) * avg_loss

    # Profit factor = gross wins / |gross losses|. >1 is profitable;
    # >1.5 is solid; >2.0 is excellent. Infinity when no losses.
    gross_win  = sum(t["pnl"] for t in wins)
    gross_loss = sum(t["pnl"] for t in losses)
    if gross_loss < 0:
        profit_factor = round(gross_win / abs(gross_loss), 2)
    else:
        profit_factor = float("inf") if gross_win > 0 else 0.0

    # Payoff ratio = avgWin / |avgLoss|. Decoupled from WR.
    payoff = round(avg_win / abs(avg_loss), 2) if avg_loss < 0 else 0.0

    return {
        "n":             len(trades),
        "wins":          len(wins),
        "losses":        len(losses),
        "win_rate":      round(win_rate, 3),
        "avg_win":       round(avg_win, 2),
        "avg_loss":      round(avg_loss, 2),
        "total_pnl":     round(total, 2),
        "big_losses":    len(big_losses),
        "scratches":     len(scratches),
        "expectancy":    round(expectancy, 2),
        "profit_factor": profit_factor,
        "payoff":        payoff,
    }


def _per_agent_stats(trades: List[dict]) -> Dict[int, Dict[str, Any]]:
    by_agent: Dict[int, List[dict]] = {}
    for t in trades:
        by_agent.setdefault(t["agent_id"], []).append(t)
    return {aid: _compute_stats(lst) for aid, lst in by_agent.items()}


# ── Param helpers ───────────────────────────────────────────────────────

def _clamp(key: str, value: float) -> float:
    lo, hi = PARAM_BOUNDS.get(key, (None, None))
    if lo is not None and value < lo:
        value = lo
    if hi is not None and value > hi:
        value = hi
    return value


def _fmt(v: float) -> str:
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return s or "0"


def _set_env(key: str, value: float) -> Optional[tuple]:
    # Per-run ratchet: clamp the proposed change to PARAM_STEP_CAP[key]
    # of the current value, before the absolute PARAM_BOUNDS clamp.
    old_s = os.environ.get(key, "")
    try:
        cur_v = float(old_s) if old_s else None
    except (TypeError, ValueError):
        cur_v = None
    proposed = float(value)
    cap = PARAM_STEP_CAP.get(key)
    if cap is not None and cur_v is not None and cur_v > 0:
        lo = cur_v * (1.0 - cap)
        hi = cur_v * (1.0 + cap)
        if proposed < lo:
            proposed = lo
        elif proposed > hi:
            proposed = hi

    v = _clamp(key, proposed)
    new_s = _fmt(v)
    if old_s == new_s:
        return None
    os.environ[key] = new_s
    logger.info("[SelfTuner] %s: %s -> %s", key, old_s or "(unset)", new_s)
    return (old_s, new_s)


def _fget(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


# ── Tuning rules ────────────────────────────────────────────────────────

def tune_once() -> Dict[str, Any]:
    """Single tuner pass. Safe to call on any schedule."""
    global _LAST_FINGERPRINT

    window  = int(_fget("SELF_TUNER_WINDOW", 50))
    min_n   = int(_fget("SELF_TUNER_MIN_TRADES", 10))
    kill_wr = _fget("SELF_TUNER_AGENT_KILL_WR", 0.25)

    trades  = _get_recent_closed_trades(limit=window)
    stats   = _compute_stats(trades)
    changes: Dict[str, Any] = {}

    logger.info("[SelfTuner] window=%d stats=%s", window, stats)
    if stats["n"] < min_n:
        logger.info(
            "[SelfTuner] only %d closed trades (<%d) — skipping tune.",
            stats["n"], min_n,
        )
        return {"stats": stats, "changes": {}, "skipped": True}

    # Dataset fingerprint — ids + pnls. If identical to the last pass,
    # skip rule application so fixed stats don't compound the same
    # adjustment every cycle.
    try:
        fp = "|".join(f"{t['id']}:{round(t.get('pnl') or 0.0, 4)}" for t in trades)
    except Exception:
        fp = f"n={stats['n']}|pnl={stats['total_pnl']}"
    if fp == _LAST_FINGERPRINT:
        logger.info(
            "[SelfTuner] dataset unchanged since last run (n=%d) — skipping rule application.",
            stats["n"],
        )
        return {"stats": stats, "changes": {}, "skipped": True, "reason": "unchanged"}
    _LAST_FINGERPRINT = fp

    wr  = stats["win_rate"]
    big = stats["big_losses"]

    # Rule 1: SL cap. When adaptive SL is active (SL_ATR_MULT>0), the
    # risk_manager's cap = max(SL_FLOOR_POINTS, atr*SL_ATR_MULT) never
    # reads MAX_SL_POINTS, so tuning MAX_SL_POINTS in that mode is a
    # no-op. Target SL_FLOOR_POINTS instead so the tuner can still
    # widen/tighten the risk envelope during a losing/winning streak.
    sl_mult = _fget("SL_ATR_MULT", 0.0)
    sl_key  = "SL_FLOOR_POINTS" if sl_mult > 0 else "MAX_SL_POINTS"
    cur_sl  = _fget(sl_key, 3.5 if sl_key == "MAX_SL_POINTS" else 3.0)
    if big >= 3 and wr < 0.45:
        c = _set_env(sl_key, cur_sl * 1.15)
        if c:
            changes[sl_key] = {"reason": "big_losses>=3 & wr<45%", "to": c[1]}
    elif big <= 1 and wr >= 0.60:
        c = _set_env(sl_key, cur_sl * 0.92)
        if c:
            changes[sl_key] = {"reason": "big_losses<=1 & wr>=60%", "to": c[1]}

    # Rule 2: Stagnation secs
    cur_stag = _fget("TRAIL_STAGNATION_SECS", 180)
    if stats["scratches"] >= stats["n"] * 0.5 and wr < 0.5:
        c = _set_env("TRAIL_STAGNATION_SECS", cur_stag * 1.15)
        if c:
            changes["TRAIL_STAGNATION_SECS"] = {"reason": "too many scratches", "to": c[1]}
    elif big >= 3:
        c = _set_env("TRAIL_STAGNATION_SECS", cur_stag * 0.85)
        if c:
            changes["TRAIL_STAGNATION_SECS"] = {"reason": "big_losses>=3", "to": c[1]}

    # Rule 3: Lot size
    balance = _mt5_balance()
    cur_lot = _fget("LOT_SIZE", 0.01)
    if stats["total_pnl"] > 20 and stats["avg_win"] > abs(stats["avg_loss"]) * 1.2 and wr >= 0.5:
        c = _set_env("LOT_SIZE", cur_lot * 1.10)
        if c:
            changes["LOT_SIZE"] = {"reason": "positive payoff & wr>=50%", "to": c[1]}
    elif stats["total_pnl"] < -30:
        c = _set_env("LOT_SIZE", cur_lot * 0.80)
        if c:
            changes["LOT_SIZE"] = {"reason": "drawdown > $30", "to": c[1]}
    if balance is not None:
        # Hard cap: LOT_SIZE <= balance / 300000 (floored at 0.01).
        balance_cap = max(0.01, round(balance / 300000.0 * 100, 2))
        if _fget("LOT_SIZE", 0.01) > balance_cap:
            c = _set_env("LOT_SIZE", balance_cap)
            if c:
                changes["LOT_SIZE"] = {"reason": f"balance cap (${balance:.2f})", "to": c[1]}

    # Rule 4: MIN_RR (signal selectivity)
    # Lower RR → take more setups (more trades, potentially more noise).
    # Higher RR → be pickier (fewer trades, demand bigger winners).
    #
    # Signals:
    #   * expectancy negative, payoff < 1.0 → we're taking too many mediocre
    #     setups; raise RR floor to force pickier entries.
    #   * expectancy positive, wr >= 60%, scratches high → the payoff
    #     distribution is fine but RR is blocking borderline winners;
    #     loosen slightly.
    cur_rr  = _fget("MIN_RR", 1.5)
    payoff  = stats.get("payoff", 0.0)
    exp_val = stats.get("expectancy", 0.0)
    if exp_val < 0 and payoff < 1.0 and stats["n"] >= min_n:
        c = _set_env("MIN_RR", cur_rr * 1.10)
        if c:
            changes["MIN_RR"] = {"reason": "neg expectancy & payoff<1", "to": c[1]}
    elif exp_val > 5.0 and wr >= 0.60 and stats["scratches"] >= stats["n"] * 0.4:
        c = _set_env("MIN_RR", cur_rr * 0.93)
        if c:
            changes["MIN_RR"] = {"reason": "pos expectancy & many scratches", "to": c[1]}

    # Rule 5: Per-agent kill switch
    agent_names = _get_agent_names()
    per_agent   = _per_agent_stats(trades)
    existing = {
        s.strip() for s in os.getenv("SELF_TUNER_DISABLED_AGENTS", "").split(",")
        if s.strip()
    }
    newly_killed: List[str] = []
    for aid, astats in per_agent.items():
        if astats["n"] < min_n or astats["win_rate"] >= kill_wr:
            continue
        name = (agent_names.get(aid) or "").strip()
        if not name:
            continue
        token = name.split("-")[0]
        if token.lower() in {t.lower() for t in existing}:
            continue
        existing.add(token)
        newly_killed.append(f"{name} (wr={astats['win_rate']} n={astats['n']})")
        logger.warning(
            "[SelfTuner] Disabling agent '%s' — win_rate=%s over %d trades (< %s)",
            name, astats["win_rate"], astats["n"], kill_wr,
        )
    if newly_killed:
        os.environ["SELF_TUNER_DISABLED_AGENTS"] = ",".join(sorted(existing))
        changes["disabled_agents"] = newly_killed

    # Rule 6: Per-setup WR kill — shadow-based
    # Reads shadow_decisions over the last N days, joins each fired row
    # to its matching closed signal (by timestamp proximity), groups by
    # setup_type, and auto-adds consistently-losing setups to
    # DISABLED_SETUPS (honored by ai_agent's breakout gate).
    #
    # Intent: if counter_trend fires 12x with 17% WR and -$80 total, kill
    # it automatically rather than waiting for a human review.
    try:
        min_setup_n = int(_fget("SELF_TUNER_SETUP_MIN_N", 10))
        setup_kill_wr = _fget("SELF_TUNER_SETUP_KILL_WR", 0.30)
        setup_kill_exp = _fget("SELF_TUNER_SETUP_KILL_EXP", 0.0)
        setup_lookback = int(_fget("SELF_TUNER_SETUP_LOOKBACK_DAYS", 14))

        disabled = {
            s.strip() for s in os.getenv("DISABLED_SETUPS", "").split(",")
            if s.strip()
        }
        setup_stats = _get_setup_stats(lookback_days=setup_lookback)
        newly_disabled_setups: List[str] = []
        for setup, st in setup_stats.items():
            if setup in disabled:
                continue
            if st["n"] < min_setup_n:
                continue
            if st["win_rate"] < setup_kill_wr and st["expectancy"] <= setup_kill_exp:
                disabled.add(setup)
                newly_disabled_setups.append(
                    f"{setup} (wr={st['win_rate']} exp=${st['expectancy']} n={st['n']})"
                )
                logger.warning(
                    "[SelfTuner] Disabling setup '%s' — wr=%s exp=$%s over %d trades",
                    setup, st["win_rate"], st["expectancy"], st["n"],
                )
        if newly_disabled_setups:
            os.environ["DISABLED_SETUPS"] = ",".join(sorted(disabled))
            changes["disabled_setups"] = newly_disabled_setups
        if setup_stats:
            changes.setdefault("setup_stats", setup_stats)
    except Exception as e:
        logger.warning("[SelfTuner] per-setup rule skipped: %s", e)

    return {"stats": stats, "changes": changes, "skipped": False}


# ── Async loop ──────────────────────────────────────────────────────────

async def run_self_tuner_loop() -> None:
    """Background coroutine: tune every SELF_TUNER_INTERVAL_MINUTES."""
    if os.getenv("SELF_TUNER_ENABLED", "false").strip().lower() not in (
        "true", "1", "yes", "on",
    ):
        logger.info("[SelfTuner] disabled (SELF_TUNER_ENABLED != true)")
        return
    try:
        interval_min = int(_fget("SELF_TUNER_INTERVAL_MINUTES", 15))
    except Exception:
        interval_min = 15
    interval_s = max(60, interval_min * 60)
    logger.info(
        "[SelfTuner] started — tuning every %d min (window=%d, min_trades=%d)",
        interval_min,
        int(_fget("SELF_TUNER_WINDOW", 50)),
        int(_fget("SELF_TUNER_MIN_TRADES", 10)),
    )
    await asyncio.sleep(min(interval_s, 300))
    while True:
        try:
            tune_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("[SelfTuner] tune_once failed: %s", exc)
        try:
            await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            break
