"""
AI Trader Agent — autonomous XAUUSD trading driven by Claude.

A single Claude-Trader agent runs on its own cycle, analyzes the current
MT5 gold price + recent-bar context, and executes trades through MT5.
HTTP calls into the local FastAPI backend record the signal in the DB so
the self-tuner can learn from outcomes.

Kept in this rewrite (from the source win_trader/ version):
  * `_last_position_id` bound via `get_last_opened_ticket(magic=202400)`
    immediately after trade open.
  * `_close_last_signal` with 30s timeout, 3 HTTP retries, and an SQLite
    direct-write fallback when the HTTP endpoint is unreachable.
  * External-close detection with a 3-retry loop + `_last_mt5_price` /
    `current_price` fallback so a closed trade is never silently lost.
  * `_last_position_id` cleared to None after close.
  * Uses `position_id=self._last_position_id` when calling
    `get_last_close_deal_sync` — strict match to avoid cross-agent
    contamination on a shared magic number.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from config import (
    AI_TRADER_AGENT_PASSWORD,
    AI_TRADER_BACKEND_URL,
    AI_TRADER_INTERVAL_SECONDS,
    ANTHROPIC_API_KEY,
    CLAUDE_TRADER_NAME,
    CLAUDE_TRADER_PASSWORD,
    LOT_SIZE,
    MAGIC_NUMBER,
    MT5_SYMBOL,
)

logger = logging.getLogger(__name__)

BACKEND_URL         = AI_TRADER_BACKEND_URL
TRADE_INTERVAL_SECS = AI_TRADER_INTERVAL_SECONDS
CONTRACT_SIZE       = 100   # XAUUSD: 1 lot = 100oz


# ---------------------------------------------------------------------------
# Trading-session gate
# ---------------------------------------------------------------------------
# XAUUSD is only reliably tight during London + NY overlap. Outside those
# hours OANDA's demo routinely shows 400+ point spreads, which would either
# blow through SL immediately or be rejected by MAX_SPREAD_POINTS anyway —
# either way, calling Claude is wasted tokens. This gate runs BEFORE the
# LLM call so off-hours cycles cost $0.
#
# Default: 07:00–21:00 UTC (covers London open through NY close).
# Weekends (Sat, and Sun before 22:00 UTC) are always skipped.
#
# Override via .env:
#     TRADING_HOURS_UTC_START=7
#     TRADING_HOURS_UTC_END=21
#     TRADING_SESSION_GATE_ENABLED=true
# ---------------------------------------------------------------------------

def _is_trading_session(now_utc: Optional[datetime] = None) -> tuple[bool, str]:
    """Return (open, reason). `open=False` means skip this cycle entirely."""
    if os.getenv("TRADING_SESSION_GATE_ENABLED", "true").strip().lower() in (
        "0", "false", "no", "off",
    ):
        return True, "session gate disabled"

    now = now_utc or datetime.now(timezone.utc)

    # Weekend: Saturday all day, Sunday before 22:00 UTC (gold re-opens Sun 22:00)
    # Python weekday(): Mon=0, Sat=5, Sun=6
    wd = now.weekday()
    if wd == 5:
        return False, "weekend (Sat) — market closed"
    if wd == 6 and now.hour < 22:
        return False, f"weekend (Sun {now.hour:02d}:00 UTC) — market re-opens 22:00"

    # Intraday window
    try:
        start = int(os.getenv("TRADING_HOURS_UTC_START", "7"))
        end   = int(os.getenv("TRADING_HOURS_UTC_END",   "21"))
    except Exception:
        start, end = 7, 21

    # Entry buffer: stop accepting NEW entries during the last N minutes
    # of the window. A trade opened at 20:45 with a 30-min cycle could
    # stagnation-close or hard-close after 21:00 when liquidity drops.
    # The trail loop keeps managing already-open positions — this buffer
    # only affects fresh entries.
    try:
        entry_buf_min = int(os.getenv("TRADING_ENTRY_BUFFER_MIN", "0"))
    except Exception:
        entry_buf_min = 0

    total_min = now.hour * 60 + now.minute
    start_min = start * 60
    end_min   = end   * 60
    cutoff_min = end_min - max(0, entry_buf_min)

    if start_min <= total_min < cutoff_min:
        return True, (
            f"session active ({now.hour:02d}:{now.minute:02d} UTC "
            f"∈ [{start:02d}:00, {end:02d}:00) buffer={entry_buf_min}m)"
        )
    if cutoff_min <= total_min < end_min:
        return False, (
            f"late-session entry buffer "
            f"({now.hour:02d}:{now.minute:02d} UTC — last "
            f"{entry_buf_min}min of window, no new entries)"
        )
    return False, (
        f"outside session ({now.hour:02d}:{now.minute:02d} UTC "
        f"∉ [{start:02d}:00, {end:02d}:00))"
    )


def _secs_until_session_open(now_utc: Optional[datetime] = None) -> int:
    """Seconds until the next valid trading session open. Walks past
    weekends (Sat, Sun before 22:00 UTC) so we don't busy-loop at 10-min
    cadence all weekend. Capped at 30min so we still wake up regularly
    to pick up self-tuner env changes & manual restarts."""
    from datetime import timedelta
    now = now_utc or datetime.now(timezone.utc)
    try:
        start = int(os.getenv("TRADING_HOURS_UTC_START", "7"))
    except Exception:
        start = 7

    # Find the next UTC datetime where:
    #   * weekday ∈ Mon-Fri, OR it's Sunday at/after 22:00
    #   * hour == START hour
    # Start from the first candidate (today's START, or tomorrow's if we
    # already passed it) and step forward 1 day at a time until it lands
    # on a valid session-open slot.
    target = now.replace(hour=start, minute=0, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)

    # Walk past Saturday (wd=5). For Sunday (wd=6), open only if START
    # hour >= 22; otherwise skip to Monday.
    for _ in range(8):  # hard safety cap; 7 iters is plenty
        wd = target.weekday()
        if wd == 5:                          # Saturday — always skip
            target = target + timedelta(days=1)
            continue
        if wd == 6 and start < 22:           # Sunday AM — skip to Mon
            target = target + timedelta(days=1)
            continue
        break

    delta = int((target - now).total_seconds())
    # Clamp: never sleep more than 30min in one go; floor at 10min so
    # we don't busy-spin right at the boundary.
    return max(600, min(delta, 1800))


# ---------------------------------------------------------------------------
# AI clients (lazy-loaded singletons)
# ---------------------------------------------------------------------------

_claude_client = None


def _get_claude_client():
    global _claude_client
    if _claude_client is not None:
        return _claude_client
    if not ANTHROPIC_API_KEY:
        return None
    try:
        import anthropic
        _claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        return _claude_client
    except Exception as e:
        logger.error("[AI] Claude client init failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Prompt + parsing
# ---------------------------------------------------------------------------

# Static rules — cached server-side via Anthropic prompt caching so only the
# small "user" block with live price/context incurs full input-token cost.
_PROMPT_SYSTEM = """You are a disciplined systematic trader on XAUUSD (gold / USD) on 30-minute bars.

Decide one of: buy, sell, hold. If unsure, pick hold.

Return STRICTLY valid JSON with these keys:
  action:               "buy" | "sell" | "hold"
  confidence:           integer 0-100 (hold anything below 50)
  quantity:             float lot size, 0.01 to 0.10
  stop_loss:            number (USD price) or null
  take_profit:          number (USD price) or null
  next_check_minutes:   integer 5-120
  reasoning:            one short sentence

Rules:
  - SL/TP must be on the correct side of current price and at least $1 away.
  - A buy needs SL < price < TP. A sell needs TP < price < SL.
  - Target reward:risk >= 2.0 (TP distance >= 2.0 * SL distance).
  - Stop-loss distance should be ~2 * atr. Tighter is noise; wider kills RR.
  - PRIMARY TRIGGER (preferred setup):
      BUY  when bb_break=="above" AND vol_ratio >= 1.2 AND trend_long=="up"
      SELL when bb_break=="below" AND vol_ratio >= 1.2 AND trend_long=="down"
    This is a breakout-follow-trend setup. Confidence >= 70 when all 3 line up.
  - SECONDARY (mean-reversion, trend_long=="flat" only):
      range_pos >= 0.75 + rsi14 >= 65 -> SELL to midpoint
      range_pos <= 0.25 + rsi14 <= 35 -> BUY to midpoint
    Confidence 60-70 range. Avoid in strong long-term trends.
  - COUNTER-TREND EXTREME (capitulation / exhaustion):
      bb_break=="below" + rsi14 <= 35 -> consider BUY (oversold flush)
      bb_break=="above" + rsi14 >= 65 -> consider SELL (overbought exhaustion)
    This fires when price breaks BB but RSI signals extreme in the opposite
    direction — classic reversal. Require confidence >= 70 (harder bar than
    primary) because you are fading the trend. Tight SL just beyond the
    bb_break extreme. If you're not convinced, hold.
  - trend_long (price vs EMA200) is the dominant trend filter. Fading it
    needs exceptional evidence (extreme RSI + S/R rejection + volume).
  - trend_h4/trend_d1 must at least not contradict your direction.
  - DXY context (gold is inverse to USD): dxy_bias=="weak_usd" supports BUY,
    dxy_bias=="strong_usd" supports SELL. If dxy_bias contradicts your
    intended direction, require higher confidence (≥75) or skip.
  - Session context: "ny_overlap" is highest liquidity; outside that,
    only take PRIMARY setups.
  - bb_break=="inside" + vol_ratio < 1.2 = no setup = HOLD.
  - Use nearest resistance as a natural SELL target or BUY stop-loss
    anchor; nearest support as a natural BUY target or SELL stop-loss
    anchor. Ignore if farther than 2 * atr from price.
  - If action is hold, set SL/TP to null.
No other text — JSON only."""

_PROMPT_USER = """Current price: ${price:.2f}
Current position: {position}
Recent context: {context}"""


def _build_user_prompt(
    current_price: float,
    current_position: Optional[str],
    snap: Optional[dict] = None,
) -> str:
    pos = current_position.upper() if current_position else "FLAT"
    # Snapshot is normally provided by the caller (the breakout gate in
    # run_cycle already fetched it). Fall back to an own fetch only when
    # none was supplied — this avoids the 2-3x redundant MT5 round-trip
    # pattern the old code had. The snapshot cache in mt5_trader would
    # also absorb a repeat call, but passing the same dict is strictly
    # cheaper.
    if snap is None:
        try:
            from mt5_trader import get_market_snapshot_sync
            snap = get_market_snapshot_sync(bars=48)
        except Exception:
            snap = None

    if snap:
        context = (
            f"timeframe={snap['timeframe']} bars={snap['bars']} "
            f"trend_m30={snap['trend']} "
            f"trend_long={snap.get('trend_long')} "
            f"trend_h4={snap.get('trend_h4')} "
            f"trend_d1={snap.get('trend_d1')} "
            f"session={snap.get('session')} "
            f"momentum_{snap['bars']}bars={snap['momentum']:+.2f} "
            f"rsi14={snap.get('rsi14')} "
            f"ema200={snap.get('ema200')} "
            f"bb_upper={snap.get('bb_upper')} "
            f"bb_lower={snap.get('bb_lower')} "
            f"bb_break={snap.get('bb_break')} "
            f"vol_ratio={snap.get('vol_ratio')} "
            f"dxy={snap.get('dxy')} "
            f"dxy_change_pct={snap.get('dxy_change_pct')} "
            f"dxy_bias={snap.get('dxy_bias')} "
            f"session_hi={snap['session_hi']:.2f} "
            f"session_lo={snap['session_lo']:.2f} "
            f"range_pos={snap.get('range_pos', 0.5)} "
            f"support={snap.get('support')} "
            f"resistance={snap.get('resistance')} "
            f"atr={snap['atr']:.2f} "
            f"sma10={snap['sma10']} sma30={snap['sma30']} "
            f"last6_closes={snap['last_closes']}"
        )
    else:
        context = "unavailable"

    return _PROMPT_USER.format(
        price=current_price,
        position=pos,
        context=context,
    )


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_json(text: str) -> Optional[dict]:
    if not text:
        return None
    m = _JSON_RE.search(text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _validate(raw: dict, price: float) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    action = str(raw.get("action", "hold")).lower().strip()
    if action not in ("buy", "sell", "hold"):
        action = "hold"
    try:
        confidence = int(raw.get("confidence", 0))
    except Exception:
        confidence = 0
    # Confidence gating happens centrally via the MIN_CONFIDENCE_PCT env
    # check in run_cycle (see "Confidence floor" block). The legacy
    # hardcoded "<50 -> hold" here used to double-gate: a 55% signal would
    # pass validate, spend LLM tokens, then get killed at the env floor.
    # Single gate is clearer and env-tunable.

    # quantity clamp honors MAX_LOT_SIZE env so per-profile broker/account
    # limits bind correctly (e.g. OANDA live is pinned at 0.01).
    try:
        min_lot = float(os.getenv("MIN_LOT_SIZE", "0.01"))
    except (TypeError, ValueError):
        min_lot = 0.01
    try:
        max_lot = float(os.getenv("MAX_LOT_SIZE", "0.10"))
    except (TypeError, ValueError):
        max_lot = 0.10
    try:
        quantity = max(min_lot, min(max_lot, round(float(raw.get("quantity", LOT_SIZE)), 2)))
    except Exception:
        quantity = max(min_lot, min(max_lot, LOT_SIZE))

    def _safe(v):
        # Accept SL/TP within 2% of price. 5% was absurd for our 30-min
        # XAUUSD setups (~$235 at price $4700) — the risk-manager SL clamp
        # would have to cut it anyway. 2% (~$94) is still plenty of room
        # while catching obvious model hallucinations early.
        if v is None:
            return None
        try:
            f = round(float(v), 2)
            return f if abs(f - price) <= price * 0.02 else None
        except (TypeError, ValueError):
            return None

    try:
        next_check = max(5, min(120, int(raw.get("next_check_minutes", 30))))
    except Exception:
        next_check = 30

    if action == "hold":
        return {
            "action": "hold", "confidence": confidence, "quantity": LOT_SIZE,
            "stop_loss": None, "take_profit": None,
            "next_check_minutes": next_check,
            "reasoning": str(raw.get("reasoning", ""))[:500],
        }

    sl_out = _safe(raw.get("stop_loss"))
    tp_out = _safe(raw.get("take_profit"))

    # Safety net: downgrade to hold if either SL or TP was rejected by
    # _safe (out of 2% range or unparsable). Previously this left the
    # action as buy/sell with stop_loss=None → order fired with NO SL,
    # bypassing the risk-manager RR check entirely.
    if sl_out is None or tp_out is None:
        logger.warning(
            "[AI] Forcing HOLD — invalid SL/TP from model: sl=%s tp=%s price=%.2f",
            raw.get("stop_loss"), raw.get("take_profit"), price,
        )
        return {
            "action": "hold", "confidence": confidence, "quantity": LOT_SIZE,
            "stop_loss": None, "take_profit": None,
            "next_check_minutes": next_check,
            "reasoning": "downgraded: invalid SL/TP",
        }

    return {
        "action":             action,
        "confidence":         confidence,
        "quantity":           quantity,
        "stop_loss":          sl_out,
        "take_profit":        tp_out,
        "next_check_minutes": next_check,
        "reasoning":          str(raw.get("reasoning", ""))[:500],
    }


def _get_claude_decision(
    price: float,
    current_position: Optional[str],
    snap: Optional[dict] = None,
) -> Optional[dict]:
    client = _get_claude_client()
    if not client:
        return None
    try:
        # Static rules go in `system` with cache_control so re-entrant cycles
        # (model retries, agent restarts within ~5 min, etc.) hit the cache
        # and pay ~10% of the normal rate on the rules prefix.
        msg = client.messages.create(
            model="claude-opus-4-7",
            max_tokens=512,
            system=[{
                "type": "text",
                "text": _PROMPT_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{
                "role":    "user",
                "content": _build_user_prompt(price, current_position, snap=snap),
            }],
        )
        usage = getattr(msg, "usage", None)
        if usage is not None:
            cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
            cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
            logger.debug(
                "[Claude] tokens in=%s out=%s cache_read=%s cache_write=%s",
                getattr(usage, "input_tokens", 0),
                getattr(usage, "output_tokens", 0),
                cache_read, cache_write,
            )
        text = msg.content[0].text
        logger.info("[Claude] Response: %s", text[:300])
        raw = _parse_json(text)
        return _validate(raw, price) if raw else None
    except Exception as e:
        logger.error("[Claude] decision error: %s", e)
        return None


# ---------------------------------------------------------------------------
# AITraderAgent
# ---------------------------------------------------------------------------

class AITraderAgent:
    """Autonomous trader backed by a single AI model."""

    def __init__(self, provider: str, agent_name: str, agent_password: str):
        assert provider in ("claude",), "invalid provider"
        self.provider       = provider
        self.agent_name     = agent_name
        self.agent_password = agent_password

        self._token:    Optional[str] = None
        self._agent_id: Optional[int] = None

        self._cycle_count = 0
        self._trade_count = 0
        self._running     = False

        # In-flight trade state
        self._current_position: Optional[str] = None
        self._last_signal_id:   Optional[int] = None
        self._last_entry_price: Optional[float] = None
        self._last_quantity:    Optional[float] = None
        self._last_side:        Optional[str]   = None
        self._last_sl:          Optional[float] = None
        self._last_tp:          Optional[float] = None
        # MT5 opening-ticket — strict match for the close deal
        self._last_position_id: Optional[int] = None
        self._last_mt5_price:   Optional[float] = None
        self._last_trade_at:    Optional[str]   = None
        self._last_decision:    Optional[dict]  = None
        self._next_check_secs:  int = TRADE_INTERVAL_SECS

        # Fast-cycle trigger: set by _check_external_close when a position
        # closes, so the main loop wakes early and looks for a re-entry
        # instead of waiting out the remainder of the ~30-min cycle. Lazy-
        # initialised on the first run because the event loop isn't up yet
        # at construction time.
        self._close_detected: Optional[asyncio.Event] = None

        # Shadow-log snapshot cache + last setup type. Reset per cycle in
        # run_cycle(). Powers evidence-based tuning via shadow_decisions.
        self._last_snap: Optional[dict] = None
        self._last_setup_type: Optional[str] = None

        # Trailing-stop state (per open position; reset on close)
        self._max_favorable:    float = 0.0   # max profit excursion in $
        self._be_locked:        bool  = False # BE+lock already applied?

    # ── HTTP helpers ────────────────────────────────────────────────────

    def _login(self) -> Optional[str]:
        if not self.agent_password:
            return None
        try:
            r = requests.post(
                f"{BACKEND_URL}/api/agents/login",
                json={"name": self.agent_name, "password": self.agent_password},
                timeout=10,
            )
            if r.status_code == 200:
                data = r.json()
                self._agent_id = data.get("agent_id")
                return data.get("token")
            logger.warning("[%s] login HTTP %s: %s",
                           self.agent_name, r.status_code, r.text[:200])
        except Exception as e:
            logger.error("[%s] login error: %s", self.agent_name, e)
        return None

    def _authenticate(self) -> bool:
        tok = self._login()
        if not tok:
            logger.error("[%s] authentication failed", self.agent_name)
            return False
        self._token = tok
        logger.info("[%s] authenticated (agent_id=%s)",
                    self.agent_name, self._agent_id)
        return True

    # ── Shadow decision log ─────────────────────────────────────────────

    def _log_shadow(
        self,
        price: Optional[float],
        snap: Optional[dict],
        *,
        gate: str,
        setup: Optional[str],
        action: Optional[str],
        confidence: Optional[int],
        reasoning: Optional[str],
        llm_called: bool,
        trade_fired: bool,
    ) -> None:
        """Best-effort shadow log. Never raises."""
        try:
            from database import log_shadow_decision
            snap = snap or {}
            log_shadow_decision({
                "ts_utc":      datetime.now(timezone.utc).isoformat(),
                "agent_name":  self.agent_name,
                "cycle":       self._cycle_count,
                "price":       price,
                "gate_result": gate,
                "setup_type":  setup,
                "bb_break":    snap.get("bb_break"),
                "vol_ratio":   snap.get("vol_ratio"),
                "trend_long":  snap.get("trend_long"),
                "trend_h4":    snap.get("trend_h4"),
                "trend_d1":    snap.get("trend_d1"),
                "rsi14":       snap.get("rsi14"),
                "range_pos":   snap.get("range_pos"),
                "atr":         snap.get("atr"),
                "action":      action,
                "confidence":  confidence,
                "reasoning":   (reasoning or "")[:500] or None,
                "llm_called":  1 if llm_called else 0,
                "trade_fired": 1 if trade_fired else 0,
            })
        except Exception as e:
            logger.debug("[%s] shadow log skipped: %s", self.agent_name, e)

    # ── Open trade (POST /api/signals) ──────────────────────────────────

    def _post_trade(self, decision: dict, mt5_fill_price: float) -> bool:
        if not self._token:
            return False

        action = decision["action"]
        payload = {
            "market":   "forex",
            "symbol":   MT5_SYMBOL,
            "action":   action,
            "price":    mt5_fill_price,
            "quantity": decision.get("quantity", LOT_SIZE),
            "content":  (
                f"[{self.agent_name}] {decision.get('reasoning', '')}"
                f" | Confidence: {decision.get('confidence', 0)}%"
                + (f" | SL: ${decision['stop_loss']:.2f}" if decision.get('stop_loss') else "")
                + (f" | TP: ${decision['take_profit']:.2f}" if decision.get('take_profit') else "")
            ),
            "stop_loss":   decision.get("stop_loss"),
            "take_profit": decision.get("take_profit"),
            "executed_at": "now",
        }
        try:
            r = requests.post(
                f"{BACKEND_URL}/api/signals",
                json=payload,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=30,
            )
            if r.status_code == 200:
                resp = r.json()
                self._last_signal_id   = resp.get("signal_id")
                self._last_entry_price = mt5_fill_price
                self._last_quantity    = float(payload["quantity"])
                self._last_side        = action
                self._last_sl          = decision.get("stop_loss")
                self._last_tp          = decision.get("take_profit")
                self._last_trade_at    = datetime.now(timezone.utc).isoformat()
                self._trade_count     += 1

                # Strict-match bind: record the MT5 opening ticket so the
                # eventual close deal matches THIS open, not another agent's.
                try:
                    from mt5_trader import get_last_opened_ticket
                    self._last_position_id = get_last_opened_ticket(magic=MAGIC_NUMBER)
                    if self._last_position_id:
                        logger.info(
                            "[%s] Bound MT5 position_id=%s to signal %s",
                            self.agent_name, self._last_position_id, self._last_signal_id,
                        )
                except Exception as exc:
                    logger.debug("[%s] ticket capture failed: %s", self.agent_name, exc)

                logger.info(
                    "[%s] Trade recorded: %s XAUUSD @ $%.2f qty=%s signal_id=%s",
                    self.agent_name, action.upper(), mt5_fill_price,
                    payload["quantity"], self._last_signal_id,
                )
                return True
            logger.warning("[%s] POST signal rejected: %s %s",
                           self.agent_name, r.status_code, r.text[:200])
            if r.status_code == 401:
                self._token = None
            return False
        except Exception as e:
            logger.error("[%s] POST signal error: %s", self.agent_name, e)
            return False

    # ── Close trade (POST /api/signals/{id}/close with retries + fallback) ─

    def _close_last_signal(self, exit_price: float) -> None:
        """Record exit_price + pnl on the tracked signal. 3 HTTP retries,
        then SQLite direct-write fallback so the tuner sees closed trades
        even when the backend is momentarily unreachable."""
        if not self._last_signal_id:
            return

        entry = self._last_entry_price or 0.0
        qty   = self._last_quantity or LOT_SIZE
        side  = (self._last_side or "").lower()
        if side in ("buy", "cover"):
            pnl = (exit_price - entry) * qty * CONTRACT_SIZE
        else:
            pnl = (entry - exit_price) * qty * CONTRACT_SIZE
        pnl_rounded = round(pnl, 2)

        closed_via_http = False
        if self._token:
            for attempt in range(3):
                try:
                    r = requests.post(
                        f"{BACKEND_URL}/api/signals/{self._last_signal_id}/close",
                        params={"exit_price": exit_price, "pnl": pnl_rounded},
                        headers={"Authorization": f"Bearer {self._token}"},
                        timeout=30,
                    )
                    if r.status_code == 200:
                        closed_via_http = True
                        logger.info(
                            "[%s] Closed signal %s: exit=$%s pnl=$%.2f",
                            self.agent_name, self._last_signal_id,
                            exit_price, pnl_rounded,
                        )
                        break
                    logger.warning(
                        "[%s] close HTTP attempt %d/3 returned %s: %s",
                        self.agent_name, attempt + 1, r.status_code, r.text[:120],
                    )
                except Exception as e:
                    logger.warning("[%s] close HTTP attempt %d/3 failed: %s",
                                   self.agent_name, attempt + 1, e)

        # SQLite direct-write fallback when all HTTP retries failed
        if not closed_via_http:
            try:
                from database import get_db_connection
                conn = get_db_connection()
                try:
                    conn.execute(
                        "UPDATE signals SET exit_price=?, pnl=? "
                        "WHERE signal_id=? AND exit_price IS NULL",
                        (float(exit_price), pnl_rounded, int(self._last_signal_id)),
                    )
                    conn.commit()
                    logger.warning(
                        "[%s] HTTP close failed — wrote exit directly to DB "
                        "for signal %s: exit=$%s pnl=$%.2f",
                        self.agent_name, self._last_signal_id,
                        exit_price, pnl_rounded,
                    )
                finally:
                    conn.close()
            except sqlite3.Error as db_exc:
                logger.error("[%s] DB fallback close failed for signal %s: %s",
                             self.agent_name, self._last_signal_id, db_exc)

        # Clear per-trade state
        self._last_signal_id   = None
        self._last_entry_price = None
        self._last_quantity    = None
        self._last_side        = None
        self._last_sl          = None
        self._last_tp          = None
        self._last_position_id = None
        self._max_favorable    = 0.0
        self._be_locked        = False

    # ── Main cycle ──────────────────────────────────────────────────────

    def run_cycle(self) -> None:
        """One analysis + trade cycle. Blocking — runs in thread executor."""
        self._cycle_count += 1
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        logger.info("[%s] ── Cycle #%d | %s ──",
                    self.agent_name, self._cycle_count, ts)

        # Reset per-cycle shadow-log state
        self._last_snap        = None
        self._last_setup_type  = None

        # Runtime kill switch from self_tuner
        kill_list = os.getenv("SELF_TUNER_DISABLED_AGENTS", "").strip()
        if kill_list:
            tokens = [t.strip().lower() for t in kill_list.split(",") if t.strip()]
            if any(tok and tok in self.agent_name.lower() for tok in tokens):
                self._next_check_secs = 3600
                logger.warning(
                    "[%s] disabled by self-tuner (%s) — skipping cycle",
                    self.agent_name, kill_list,
                )
                return

        if not self._token and not self._authenticate():
            logger.error("[%s] skipping cycle — not authenticated", self.agent_name)
            return

        # ── Trading-session gate (saves LLM tokens outside active hours) ──
        # Runs BEFORE the Claude API call so off-hours cycles cost $0.
        # Note: this only blocks NEW entries. The trail_loop keeps managing
        # any already-open position (BE-lock, stagnation, hard-close) 24/7.
        session_ok, session_note = _is_trading_session()
        if not session_ok:
            self._next_check_secs = _secs_until_session_open()
            logger.info(
                "[%s] skipping cycle — %s (sleep %dmin, no LLM call)",
                self.agent_name, session_note, self._next_check_secs // 60,
            )
            self._log_shadow(
                None, None, gate="session_closed",
                setup=None, action=None, confidence=None,
                reasoning=session_note,
                llm_called=False, trade_fired=False,
            )
            return
        logger.info("[%s] %s", self.agent_name, session_note)

        # Read live MT5 price — we use this as both the AI's ref price and
        # the source of truth for filling stops.
        from mt5_trader import get_current_price_sync
        price = get_current_price_sync()
        if not price:
            logger.error("[%s] no MT5 price — skipping cycle", self.agent_name)
            return
        self._last_mt5_price = price

        # ── News blackout (new entries only) ─────────────────────────
        # Don't open fresh positions around high-impact macro releases.
        # Open positions keep being managed by trail_loop.
        if not self._current_position:
            try:
                from news_filter import is_news_blackout
                blocked, reason = is_news_blackout()
                if blocked:
                    self._next_check_secs = 5 * 60  # recheck in 5min
                    logger.info("[%s] %s — skip cycle", self.agent_name, reason)
                    self._log_shadow(
                        price, None, gate="news_blackout",
                        setup=None, action=None, confidence=None,
                        reasoning=reason,
                        llm_called=False, trade_fired=False,
                    )
                    return
            except Exception as e:
                logger.debug("[%s] news filter errored (allowing): %s",
                             self.agent_name, e)

        # ── Preflight risk gates (BEFORE the LLM call) ──
        # Saves API tokens when spread is wide, daily cap is hit, or we're
        # in a cooldown. These checks don't need the decision at all.
        # Skipped when we already have an open position: exit/flip signals
        # must still be evaluated even if preflight would block a new open.
        try:
            from risk_manager import check_preflight
            if not self._current_position:
                ok, reason = check_preflight(self._agent_id or 0)
                if not ok:
                    logger.warning(
                        "[%s] preflight blocked (no LLM call): %s",
                        self.agent_name, reason,
                    )
                    self._log_shadow(
                        price, None, gate="preflight_block",
                        setup=None, action=None, confidence=None,
                        reasoning=reason,
                        llm_called=False, trade_fired=False,
                    )
                    return
                logger.info("[%s] %s", self.agent_name, reason)
        except Exception as e:
            logger.warning("[%s] preflight errored (allowing LLM call): %s",
                           self.agent_name, e)

        # ── Pre-LLM breakout gate ────────────────────────────────────
        # Only pay for Claude tokens when a concrete trigger exists:
        # either a BB20/2 breakout with volume confirmation, or a
        # range-extreme mean-reversion setup (trend_long=flat). If we
        # already hold a position, always ask (exit/flip must be evaluated).
        # Disable with BREAKOUT_GATE_ENABLED=false.
        if not self._current_position and os.getenv(
            "BREAKOUT_GATE_ENABLED", "true"
        ).strip().lower() not in ("0", "false", "no", "off"):
            try:
                from mt5_trader import get_market_snapshot_sync
                snap = get_market_snapshot_sync(bars=48)
            except Exception:
                snap = None
            # Cache for downstream (risk_manager adaptive SL) to avoid
            # a second MT5 fetch.
            self._last_snap = snap
            if snap:
                try:
                    vr_min = float(os.getenv("VOLUME_RATIO_MIN", "1.2"))
                except (TypeError, ValueError):
                    vr_min = 1.2
                bb_break       = snap.get("bb_break")
                vol_ratio      = snap.get("vol_ratio") or 0.0
                trend_long     = snap.get("trend_long")
                rsi14          = snap.get("rsi14") or 50.0
                range_pos      = snap.get("range_pos", 0.5)
                candle_age_pct = snap.get("candle_age_pct", 1.0)

                # PRIMARY: breakout + volume aligned with long-term trend.
                # Candle maturity guard: require ≥50% of M30 elapsed to
                # avoid fakeout entries on immature bars. Exception: vol≥2.0
                # signals institutional force strong enough to act early.
                try:
                    mature_thresh = float(os.getenv("CANDLE_MATURE_PCT", "0.5"))
                    vol_strong    = float(os.getenv("CANDLE_VOL_OVERRIDE", "2.0"))
                except (TypeError, ValueError):
                    mature_thresh, vol_strong = 0.5, 2.0

                candle_ok = (candle_age_pct >= mature_thresh) or (vol_ratio >= vol_strong)

                primary = (
                    (bb_break == "above" and trend_long == "up")
                    or (bb_break == "below" and trend_long == "down")
                ) and vol_ratio >= vr_min and candle_ok

                # SECONDARY: range-extreme mean-reversion in flat regime.
                # RSI thresholds 65/35 (was 70/30 — too extreme, never fired).
                try:
                    rsi_ob = float(os.getenv("SECONDARY_RSI_OB", "65"))
                    rsi_os = float(os.getenv("SECONDARY_RSI_OS", "35"))
                except (TypeError, ValueError):
                    rsi_ob, rsi_os = 65.0, 35.0

                secondary = trend_long == "flat" and (
                    (range_pos >= 0.75 and rsi14 >= rsi_ob)
                    or (range_pos <= 0.25 and rsi14 <= rsi_os)
                )

                # COUNTER-TREND EXTREME: capitulation flush or exhaustion spike.
                # Price breaks BB in one direction but RSI signals extreme in
                # the opposite — classic reversal setup. Gate passes; Claude
                # still decides. Requires bb_break + vol threshold + RSI extreme.
                # Example: trend_long=down, bb_break=below, rsi≤35 → oversold
                # capitulation. Let Claude consider a counter-trend BUY.
                try:
                    rsi_counter_os = float(os.getenv("COUNTER_RSI_OS", "35"))
                    rsi_counter_ob = float(os.getenv("COUNTER_RSI_OB", "65"))
                except (TypeError, ValueError):
                    rsi_counter_os, rsi_counter_ob = 35.0, 65.0

                counter_trend = vol_ratio >= vr_min and candle_ok and (
                    # Oversold capitulation: trending down but RSI exhausted
                    (bb_break == "below" and trend_long in ("down", "flat")
                     and rsi14 <= rsi_counter_os)
                    or
                    # Overbought exhaustion: trending up but RSI extreme
                    (bb_break == "above" and trend_long in ("up", "flat")
                     and rsi14 >= rsi_counter_ob)
                )

                # DISABLED_SETUPS: self-tuner (Rule 6) auto-disables any
                # setup type whose shadow-measured WR/expectancy is bad
                # enough. Env is a comma-separated list of setup names,
                # e.g. "counter_trend,secondary". When a setup matches,
                # we treat it as if it didn't fire.
                disabled_setups = {
                    s.strip() for s in os.getenv("DISABLED_SETUPS", "").split(",")
                    if s.strip()
                }
                if "primary" in disabled_setups:
                    primary = False
                if "secondary" in disabled_setups:
                    secondary = False
                if "counter_trend" in disabled_setups:
                    counter_trend = False

                if not (primary or secondary or counter_trend):
                    try:
                        poll = int(os.getenv("BREAKOUT_GATE_POLL_SECS", "60"))
                    except (TypeError, ValueError):
                        poll = 60
                    self._next_check_secs = max(30, poll)
                    disabled_note = (
                        f" disabled={sorted(disabled_setups)}" if disabled_setups else ""
                    )
                    logger.info(
                        "[%s] no setup — skip LLM (next poll %ds) "
                        "(bb_break=%s vol=%.2f age=%.0f%% trend_long=%s rsi=%s rp=%s%s)",
                        self.agent_name, self._next_check_secs,
                        bb_break, vol_ratio, candle_age_pct * 100,
                        trend_long, rsi14, range_pos, disabled_note,
                    )
                    self._log_shadow(
                        price, snap, gate="skip", setup=None,
                        action=None, confidence=None, reasoning=None,
                        llm_called=False, trade_fired=False,
                    )
                    return
                setup_name = (
                    "primary"       if primary       else
                    "secondary"     if secondary     else
                    "counter_trend"
                )
                logger.info(
                    "[%s] setup=%s (bb_break=%s vol=%.2f age=%.0f%% trend_long=%s)",
                    self.agent_name, setup_name,
                    bb_break, vol_ratio, candle_age_pct * 100, trend_long,
                )
                self._last_setup_type = setup_name

        # Query the AI — reuse the cached snapshot from the breakout gate
        # (same data the gate already evaluated). For cycles that skip the
        # gate (position open) snap is None; the cache inside mt5_trader
        # still prevents duplicate IPC round-trips within ~30s.
        decision = _get_claude_decision(price, self._current_position, snap=self._last_snap)
        if not decision:
            logger.warning("[%s] no decision returned", self.agent_name)
            return

        decision["current_price"] = price
        decision["analyzed_at"]   = datetime.now(timezone.utc).isoformat()
        self._last_decision       = decision
        self._next_check_secs     = decision.get("next_check_minutes", 30) * 60

        action = decision["action"]
        logger.info(
            "[%s] Decision: %s conf=%s%% price=$%.2f SL=%s TP=%s",
            self.agent_name, action.upper(), decision["confidence"],
            price, decision.get("stop_loss"), decision.get("take_profit"),
        )
        if action == "hold":
            self._log_shadow(
                price, self._last_snap, gate="pass",
                setup=self._last_setup_type, action="hold",
                confidence=int(decision.get("confidence", 0) or 0),
                reasoning=decision.get("reasoning"),
                llm_called=True, trade_fired=False,
            )
            return

        # ── Confidence floor ────────────────────────────────────────
        # Hard gate: below MIN_CONFIDENCE_PCT we treat the decision as
        # HOLD regardless of action. This runs BEFORE the risk gate so
        # low-confidence BUY/SELLs never reach MT5 and never pay spread.
        try:
            min_conf = float(os.getenv("MIN_CONFIDENCE_PCT", "0"))
        except (TypeError, ValueError):
            min_conf = 0.0
        try:
            cur_conf = float(decision.get("confidence", 0))
        except (TypeError, ValueError):
            cur_conf = 0.0
        if min_conf > 0 and cur_conf < min_conf:
            logger.info(
                "[%s] confidence %.0f%% < floor %.0f%% — forcing HOLD",
                self.agent_name, cur_conf, min_conf,
            )
            self._log_shadow(
                price, self._last_snap, gate="conf_floor",
                setup=self._last_setup_type, action=action,
                confidence=int(cur_conf),
                reasoning=decision.get("reasoning"),
                llm_called=True, trade_fired=False,
            )
            return

        # Map action to MT5 direction
        mt5_dir = "buy" if action in ("buy", "cover") else "sell"

        # ── Post-decision risk gate (SL clamp + RR check) ──
        # Preflight already ran before the LLM call. This phase handles the
        # two checks that need the decision itself.
        try:
            from risk_manager import check_postdecision, compute_lot, get_mt5_balance
            atr_hint = None
            try:
                snap_cached = self._last_snap
                if snap_cached:
                    atr_hint = snap_cached.get("atr")
                if atr_hint is None:
                    # Cache-miss fallback (e.g. position was already open so
                    # the breakout gate didn't fetch). mt5_trader's 30s cache
                    # still dedupes if anything else fetches in parallel.
                    from mt5_trader import get_market_snapshot_sync
                    snap_fresh = get_market_snapshot_sync(bars=48)
                    if snap_fresh:
                        atr_hint = snap_fresh.get("atr")
                        self._last_snap = snap_fresh
            except Exception:
                atr_hint = None
            ok, reason, decision = check_postdecision(
                self._agent_id or 0, decision, price, atr=atr_hint,
            )
            if not ok:
                logger.warning("[%s] entry blocked: %s", self.agent_name, reason)
                self._log_shadow(
                    price, self._last_snap, gate="risk_block",
                    setup=self._last_setup_type, action=action,
                    confidence=int(decision.get("confidence", 0) or 0),
                    reasoning=reason,
                    llm_called=True, trade_fired=False,
                )
                return
        except Exception as e:
            logger.warning("[%s] postdecision gate errored (allowing trade): %s",
                           self.agent_name, e)

        # If an opposite position is open, close it first
        if self._current_position and self._current_position != mt5_dir:
            logger.info("[%s] Switching %s -> %s — closing existing first",
                        self.agent_name, self._current_position.upper(), mt5_dir.upper())
            self._close_last_signal(price)
            try:
                from mt5_trader import close_all_positions
                asyncio.run(close_all_positions(MT5_SYMBOL))
            except Exception as e:
                logger.error("[%s] close_all error: %s", self.agent_name, e)
            self._current_position = None

        # ── Dynamic position sizing (risk% of balance / SL distance) ──
        # Confidence-scaled sizing (Implementation B): conviction scales
        # the RISK BUDGET (risk_pct) going INTO compute_lot, rather than
        # multiplying the final lot size. On small accounts where MIN_LOT
        # == MAX_LOT (e.g. OANDA $50 pinned at 0.01) a post-multiplier is
        # a dead code path; scaling the risk budget at least records the
        # conviction in logs and allows the multiplier to bite as soon as
        # the balance grows past the first broker step.
        #
        # Mapping:
        #   confidence=50 -> 1.00x risk
        #   confidence=80 -> 1.30x risk
        #   confidence=30 -> 0.80x risk
        # Clamped to [0.5x, 1.5x].
        sl_price  = decision.get("stop_loss")
        qty       = decision.get("quantity", LOT_SIZE)
        if sl_price is not None:
            try:
                sl_dist = abs(price - float(sl_price))
                balance = get_mt5_balance()
                if balance and sl_dist > 0:
                    try:
                        conf = float(decision.get("confidence", 50.0))
                    except (TypeError, ValueError):
                        conf = 50.0
                    multiplier = max(0.5, min(1.5, 1.0 + (conf - 50.0) / 100.0))

                    try:
                        base_pct = float(os.getenv("RISK_PCT_PER_TRADE", "0.01"))
                    except (TypeError, ValueError):
                        base_pct = 0.01
                    scaled_pct = base_pct * multiplier

                    sized = compute_lot(balance, sl_dist, risk_pct_override=scaled_pct)

                    min_lot = float(os.getenv("MIN_LOT_SIZE", "0.01"))
                    max_lot = float(os.getenv("MAX_LOT_SIZE", "1.0"))
                    sized = max(min_lot, min(max_lot, sized))

                    if abs(sized - qty) > 1e-6 or abs(multiplier - 1.0) > 1e-6:
                        pinned = (min_lot == max_lot)
                        logger.info(
                            "[%s] dynamic sizing: %.2f -> %.2f lots "
                            "(balance=$%.2f SL_dist=$%.2f risk=%.3f%%x%.2f=%.3f%%%s)",
                            self.agent_name, qty, sized,
                            balance, sl_dist, base_pct * 100, multiplier,
                            scaled_pct * 100,
                            " [pinned by MIN==MAX]" if pinned else "",
                        )
                        qty = sized
                        decision["quantity"] = sized
            except Exception as e:
                logger.warning("[%s] sizing failed: %s", self.agent_name, e)

        # Fire MT5 order
        try:
            from mt5_trader import execute_mt5_trade
            fill_price = asyncio.run(execute_mt5_trade(
                instrument=MT5_SYMBOL,
                direction=mt5_dir,
                lot_size=qty,
                stop_loss=decision.get("stop_loss"),
                take_profit=decision.get("take_profit"),
                ai_ref_price=price,
                magic=MAGIC_NUMBER,
            ))
        except Exception as e:
            logger.error("[%s] MT5 execute error: %s", self.agent_name, e)
            return

        if fill_price is None:
            logger.warning("[%s] MT5 rejected order — staying flat", self.agent_name)
            self._log_shadow(
                price, self._last_snap, gate="mt5_reject",
                setup=self._last_setup_type, action=action,
                confidence=int(decision.get("confidence", 0) or 0),
                reasoning=decision.get("reasoning"),
                llm_called=True, trade_fired=False,
            )
            return

        # Record the DB-side signal
        if self._post_trade(decision, fill_price):
            self._current_position = mt5_dir
        self._log_shadow(
            price, self._last_snap, gate="fired",
            setup=self._last_setup_type, action=action,
            confidence=int(decision.get("confidence", 0) or 0),
            reasoning=decision.get("reasoning"),
            llm_called=True, trade_fired=True,
        )

    # ── External-close detection (trail loop) ───────────────────────────

    async def _check_external_close(self) -> None:
        """If MT5 shows no open position but we think one is open, record
        the exit. Uses the strict `position_id` filter + 3 retries, and
        falls back to the last-known MT5 price so a close is never lost."""
        if not self._current_position or not self._last_entry_price:
            return
        try:
            from mt5_trader import (
                get_last_close_deal_sync,
                get_position_details_sync,
            )
            details = get_position_details_sync(MT5_SYMBOL, magic=MAGIC_NUMBER)
            mt5_side = details["side"] if details else None
            if details and details.get("current_price"):
                self._last_mt5_price = details["current_price"]

            if mt5_side is not None or self._current_position is None:
                return

            # Position is gone from MT5 — external close. Avoid stale picks
            # by only considering deals newer than when we opened.
            MIN_OPEN_SECS = 30
            last_trade_unix = 0.0
            if self._last_trade_at:
                try:
                    _lt = self._last_trade_at.replace("Z", "+00:00")
                    last_trade_unix = datetime.fromisoformat(_lt).timestamp()
                except Exception:
                    last_trade_unix = 0.0

            if last_trade_unix and (time.time() - last_trade_unix) < MIN_OPEN_SECS:
                return  # too early; let rollback catch rejected opens

            exit_price: Optional[float] = None
            for attempt in range(3):
                deal = get_last_close_deal_sync(
                    since_seconds=900,
                    after_unix=last_trade_unix or None,
                    position_id=self._last_position_id,
                    magic=MAGIC_NUMBER,
                )
                if deal and deal.get("price"):
                    exit_price = deal["price"]
                    logger.info(
                        "[%s] Close price from MT5 deal: $%.2f (ticket=%s)",
                        self.agent_name, exit_price, deal.get("ticket"),
                    )
                    break
                await asyncio.sleep(1.5)

            if exit_price is None:
                # No broker deal matched — fall back so the trade isn't lost
                fallback = self._last_mt5_price
                if fallback:
                    exit_price = fallback
                    logger.warning(
                        "[%s] No MT5 close deal after retries — recording "
                        "exit at last-known price $%.2f (approximate)",
                        self.agent_name, exit_price,
                    )
                else:
                    logger.warning(
                        "[%s] No MT5 deal and no price fallback; clearing "
                        "state without recording exit.", self.agent_name,
                    )
                    self._current_position = None
                    return

            logger.info("[%s] MT5 position closed externally at $%.2f",
                        self.agent_name, exit_price)
            # IMPORTANT: _close_last_signal does blocking requests.post() to
            # the local uvicorn — calling it directly from this async context
            # deadlocks the event loop (uvicorn can't serve the request while
            # we block waiting for its response). Push to the default thread
            # executor so the loop stays free.
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._close_last_signal, exit_price)
            self._current_position = None

            # Wake the main cycle loop — a fresh re-entry setup may already
            # be readable in this same session (hot market, M30 just printed
            # a clean candle). Waiting 20-28 min for the next scheduled
            # cycle would miss it. Session gate / preflight still apply.
            if self._close_detected is not None:
                self._close_detected.set()
        except Exception as exc:
            logger.debug("[%s] external close check skipped: %s",
                         self.agent_name, exc)

    # ── Trailing-stop manager ───────────────────────────────────────────

    async def _manage_open_position(self) -> None:
        """Runs every 15s against any open position. Five-stage SL/exit logic.

        Order of checks (early returns = mutually exclusive actions):
          1. External-close sync   (MT5 closed position → record it)
          2. Hard close             (age ≥ TRAIL_HARD_CLOSE_SECS)
          3. Stagnation close       (age ≥ TRAIL_STAGNATION_SECS & flat)
          4. Break-even lock        (once — first time favorable ≥ BE trigger)
          5. Profit trail (NEW)     (ratchet SL to lock TRAIL_LOCK_FRAC
                                     of max-favorable-excursion; only tightens)

        Env-driven (all read on each call so the self-tuner can retune live):
          * ``TRAIL_BE_ATR``           — favorable (in $/oz) as a fraction of
            ATR that triggers the break-even lock.
          * ``TRAIL_MICRO_LOCK_PTS``   — dollars beyond entry to lock at BE.
          * ``TRAIL_START_ATR``        — MFE as a fraction of ATR before the
            profit-trail starts to move SL beyond BE-lock.
          * ``TRAIL_LOCK_FRAC``        — fraction of MFE to preserve as SL.
            e.g. 0.50 ⇒ if MFE = $7.45, SL locks at entry ± $3.73.
          * ``TRAIL_MIN_ADVANCE_PTS``  — minimum SL advance to avoid
            broker-spam; skip modify if the new SL would only shift less.
          * ``TRAIL_STAGNATION_SECS``  — seconds before stagnation check.
          * ``TRAIL_STAGNATION_ATR``   — favorable < this fraction of ATR at
            the stagnation deadline ⇒ close to free up margin & avoid chop.
          * ``TRAIL_HARD_CLOSE_SECS``  — always close after this age.
        """
        # 1. External-close first (may clear state)
        await self._check_external_close()
        if not self._current_position or not self._last_entry_price:
            return

        try:
            from mt5_trader import (
                get_position_details_sync,
                get_market_snapshot_sync,
                modify_position_sl_tp,
                close_all_positions,
            )

            details = get_position_details_sync(MT5_SYMBOL, magic=MAGIC_NUMBER)
            if not details:
                return  # position gone — let next cycle catch via _check_external_close

            side          = details["side"]                # "buy" | "sell"
            current_price = float(details.get("current_price") or 0.0)
            entry         = float(self._last_entry_price)
            cur_sl        = float(details.get("sl") or 0.0)
            if current_price <= 0 or entry <= 0:
                return

            # favorable = unrealized profit in $/oz (positive = in profit)
            favorable = (current_price - entry) if side == "buy" else (entry - current_price)
            if favorable > self._max_favorable:
                self._max_favorable = favorable

            # Age in seconds since open
            age_secs = 0.0
            if self._last_trade_at:
                try:
                    _lt = self._last_trade_at.replace("Z", "+00:00")
                    age_secs = time.time() - datetime.fromisoformat(_lt).timestamp()
                except Exception:
                    age_secs = 0.0

            # Pull ATR for thresholds
            snap = get_market_snapshot_sync(MT5_SYMBOL, bars=24)
            atr = float(snap.get("atr") or 0.0) if snap else 0.0

            # Env thresholds (read every call)
            def _f(k, d):
                try:
                    return float(os.getenv(k, str(d)) or d)
                except Exception:
                    return d

            be_atr        = _f("TRAIL_BE_ATR",           0.10)
            micro_lock    = _f("TRAIL_MICRO_LOCK_PTS",   0.10)
            stag_secs     = _f("TRAIL_STAGNATION_SECS",  180.0)
            stag_atr      = _f("TRAIL_STAGNATION_ATR",   0.05)
            hard_secs     = _f("TRAIL_HARD_CLOSE_SECS",  1200.0)
            trail_start   = _f("TRAIL_START_ATR",        0.30)
            trail_frac    = _f("TRAIL_LOCK_FRAC",        0.50)
            trail_min_adv = _f("TRAIL_MIN_ADVANCE_PTS",  0.15)

            be_trigger      = be_atr      * atr if atr > 0 else 0.0
            stag_trigger    = stag_atr    * atr if atr > 0 else 0.0
            trail_start_usd = trail_start * atr if atr > 0 else 0.0

            # ── Hard close (cap on age) ────────────────────────────────
            if hard_secs > 0 and age_secs >= hard_secs:
                logger.info(
                    "[%s] hard-close: age=%.0fs >= %.0fs, favorable=$%.2f",
                    self.agent_name, age_secs, hard_secs, favorable,
                )
                closed = await close_all_positions(MT5_SYMBOL, magic=MAGIC_NUMBER)
                if closed:
                    # leave exit recording to _check_external_close on next tick
                    logger.info("[%s] hard-close dispatched (%d positions)",
                                self.agent_name, closed)
                return

            # ── Stagnation close ───────────────────────────────────────
            if (stag_secs > 0 and stag_trigger > 0 and
                    age_secs >= stag_secs and favorable < stag_trigger):
                logger.info(
                    "[%s] stagnation close: age=%.0fs fav=$%.2f < trigger=$%.2f "
                    "(atr=$%.2f × %.2f)",
                    self.agent_name, age_secs, favorable, stag_trigger,
                    atr, stag_atr,
                )
                closed = await close_all_positions(MT5_SYMBOL, magic=MAGIC_NUMBER)
                if closed:
                    logger.info("[%s] stagnation-close dispatched (%d positions)",
                                self.agent_name, closed)
                return

            # ── Break-even lock (one-shot, first time over the trigger) ──
            if (not self._be_locked and be_trigger > 0 and
                    favorable >= be_trigger):
                if side == "buy":
                    new_sl = round(entry + micro_lock, 2)
                    if new_sl > cur_sl:  # only tighten, never loosen
                        ok = await modify_position_sl_tp(MT5_SYMBOL, new_sl=new_sl)
                        if ok:
                            self._be_locked = True
                            self._last_sl   = new_sl
                            cur_sl          = new_sl
                            logger.info(
                                "[%s] BE-lock (buy): SL %.2f → %.2f "
                                "(fav=$%.2f >= trigger=$%.2f)",
                                self.agent_name, cur_sl, new_sl,
                                favorable, be_trigger,
                            )
                else:  # sell
                    new_sl = round(entry - micro_lock, 2)
                    if cur_sl == 0 or new_sl < cur_sl:  # only tighten
                        ok = await modify_position_sl_tp(MT5_SYMBOL, new_sl=new_sl)
                        if ok:
                            self._be_locked = True
                            self._last_sl   = new_sl
                            cur_sl          = new_sl
                            logger.info(
                                "[%s] BE-lock (sell): SL %.2f → %.2f "
                                "(fav=$%.2f >= trigger=$%.2f)",
                                self.agent_name, cur_sl, new_sl,
                                favorable, be_trigger,
                            )

            # ── Profit trail (ratchet, runs every tick after BE-lock) ──
            # Locks in TRAIL_LOCK_FRAC of MFE. Once MFE grows, SL tightens.
            # When price retraces, MFE doesn't change, SL stays put →
            # we bank the profit instead of giving it back to the market.
            if (self._be_locked and trail_frac > 0 and
                    trail_start_usd > 0 and
                    self._max_favorable >= trail_start_usd):
                lock_dist = self._max_favorable * trail_frac
                if side == "buy":
                    new_sl = round(entry + lock_dist, 2)
                    if new_sl > cur_sl + trail_min_adv:
                        ok = await modify_position_sl_tp(MT5_SYMBOL, new_sl=new_sl)
                        if ok:
                            self._last_sl = new_sl
                            logger.info(
                                "[%s] trail (buy): SL %.2f → %.2f "
                                "(MFE=$%.2f × %.2f = $%.2f locked)",
                                self.agent_name, cur_sl, new_sl,
                                self._max_favorable, trail_frac, lock_dist,
                            )
                else:  # sell
                    new_sl = round(entry - lock_dist, 2)
                    if cur_sl == 0 or new_sl < cur_sl - trail_min_adv:
                        ok = await modify_position_sl_tp(MT5_SYMBOL, new_sl=new_sl)
                        if ok:
                            self._last_sl = new_sl
                            logger.info(
                                "[%s] trail (sell): SL %.2f → %.2f "
                                "(MFE=$%.2f × %.2f = $%.2f locked)",
                                self.agent_name, cur_sl, new_sl,
                                self._max_favorable, trail_frac, lock_dist,
                            )

        except Exception as exc:
            logger.debug("[%s] manage_open_position skipped: %s",
                         self.agent_name, exc)

    # ── Status ──────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        last = None
        if self._last_decision:
            last = {k: self._last_decision.get(k) for k in (
                "action", "confidence", "reasoning",
                "stop_loss", "take_profit", "current_price", "analyzed_at",
            )}
        return {
            "provider":         self.provider,
            "agent_name":       self.agent_name,
            "agent_id":         self._agent_id,
            "running":          self._running,
            "authenticated":    self._token is not None,
            "cycle_count":      self._cycle_count,
            "trade_count":      self._trade_count,
            "last_trade_at":    self._last_trade_at,
            "current_position": self._current_position,
            "interval_minutes": self._next_check_secs // 60,
            "last_decision":    last,
        }

    # ── Async lifecycle ─────────────────────────────────────────────────

    async def start(self, initial_delay: float = 10.0) -> None:
        self._running = True
        loop = asyncio.get_event_loop()
        # Bind the fast-cycle event to the running loop now that we're
        # inside async context.
        self._close_detected = asyncio.Event()
        logger.info("[%s] starting (provider=%s, interval=%dmin)",
                    self.agent_name, self.provider, TRADE_INTERVAL_SECS // 60)
        await asyncio.sleep(initial_delay)

        ok = await loop.run_in_executor(None, self._authenticate)
        if not ok:
            logger.error("[%s] auth failed — will retry each cycle", self.agent_name)

        # Sync in-memory state with MT5 on startup
        try:
            from mt5_trader import get_open_position
            mt5_pos = await get_open_position(MT5_SYMBOL, magic=MAGIC_NUMBER)
            if mt5_pos != self._current_position:
                logger.info("[%s] position sync: mem=%s mt5=%s",
                            self.agent_name, self._current_position, mt5_pos)
                self._current_position = mt5_pos
        except Exception as exc:
            logger.warning("[%s] position sync failed: %s", self.agent_name, exc)

        # Position manager: external-close + trailing stop + stagnation
        async def _trail_loop():
            while self._running:
                await asyncio.sleep(15)
                try:
                    await self._manage_open_position()
                except Exception as exc:
                    logger.error("[%s] position-manager error: %s",
                                 self.agent_name, exc)
        asyncio.create_task(_trail_loop())

        # Main cycle loop — run_cycle is sync so use a thread executor
        while True:
            try:
                # Build a fresh event loop thread for the executor call
                await loop.run_in_executor(None, self.run_cycle)
            except Exception as e:
                logger.error("[%s] unhandled error: %s",
                             self.agent_name, e, exc_info=True)

            nxt = self._next_check_secs
            logger.info("[%s] next cycle in %dmin (or sooner if a trade closes)",
                        self.agent_name, nxt // 60)
            # Interruptible sleep: wake early if _check_external_close
            # detects a position close. Minimum re-cycle gap = 60s so we
            # don't spam Claude on rapid open→stop-loss sequences.
            try:
                await asyncio.wait_for(
                    self._close_detected.wait(), timeout=nxt,
                )
                self._close_detected.clear()
                logger.info(
                    "[%s] position closed — re-cycling after 60s cooldown",
                    self.agent_name,
                )
                await asyncio.sleep(60)
            except asyncio.TimeoutError:
                pass


# ---------------------------------------------------------------------------
# Global agent instances
# ---------------------------------------------------------------------------

claude_agent = AITraderAgent(
    provider="claude",
    agent_name=CLAUDE_TRADER_NAME,
    agent_password=CLAUDE_TRADER_PASSWORD or AI_TRADER_AGENT_PASSWORD,
)


async def start_ai_traders() -> None:
    """Schedule Claude-Trader as a background task."""
    if ANTHROPIC_API_KEY and claude_agent.agent_password:
        asyncio.create_task(claude_agent.start(initial_delay=12.0))
        logger.info("[AI Traders] Claude-Trader scheduled")
    else:
        logger.warning("[AI Traders] Claude disabled (missing API key or password)")


def get_all_status() -> dict:
    return {
        "claude_trader":    claude_agent.get_status(),
        "interval_minutes": TRADE_INTERVAL_SECS // 60,
    }
