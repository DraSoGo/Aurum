"""
MT5 Trader — uses the MetaTrader5 Python library for order execution.

Ports `win_trader/backend/mt5_direct_trader.py` with every recent fix:
  * `_resolve_symbol()` broker-symbol scan for XAU*USD variants
    (OANDA's XAUUSD.sml, XAU_USD, XAUUSD.pro, ...).
  * `_sym()` always returns the resolved broker symbol even when a caller
    hardcodes "XAUUSD".
  * `_ensure_mt5()` tries `MT5_PATH` first, falls back to a bare attach.
  * Filling-mode bitmask logic in both `_place_order_sync` and
    `_close_all_sync` (reads `info.filling_mode`; 2→IOC, 1→FOK, else RETURN).
  * `_LAST_OPENED_TICKET` registry + `get_last_opened_ticket(magic)`
    pop-style getter for binding a just-opened position to a DB signal.
  * `get_last_close_deal_sync` with strict `position_id` filter and
    `after_unix` filter to prevent cross-agent contamination.
  * `get_history_positions_sync` with live overlay for open positions.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Optional

from config import MT5_LOGIN, MT5_PASSWORD, MT5_PATH, MT5_SERVER, MT5_SYMBOL

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_MT5_INITIALIZED = False
_INSTRUMENT: str = MT5_SYMBOL
_RESOLVED_SYMBOL: Optional[str] = None

# Registry of most-recent opened ticket per magic — pop-style read.
_LAST_OPENED_TICKET: dict[int, int] = {}

_trade_lock = asyncio.Lock()


def get_last_opened_ticket(magic: int = 202400) -> Optional[int]:
    """Return ticket of the most recent successful open for this magic,
    then remove it — prevents stale reuse on the next cycle."""
    return _LAST_OPENED_TICKET.pop(magic, None)


# ---------------------------------------------------------------------------
# Initialization / symbol resolution
# ---------------------------------------------------------------------------

def _resolve_symbol(preferred: str) -> Optional[str]:
    """Find the broker's actual gold symbol. Caches result globally."""
    global _RESOLVED_SYMBOL, _INSTRUMENT
    if _RESOLVED_SYMBOL:
        return _RESOLVED_SYMBOL
    try:
        import MetaTrader5 as mt5

        if mt5.symbol_select(preferred, True) and mt5.symbol_info_tick(preferred):
            _RESOLVED_SYMBOL = preferred
            return preferred

        all_syms = mt5.symbols_get() or []
        candidates = [s.name for s in all_syms
                      if "XAU" in s.name.upper() and "USD" in s.name.upper()]
        candidates.sort(key=lambda n: (len(n), n))
        for cand in candidates:
            if mt5.symbol_select(cand, True):
                tick = mt5.symbol_info_tick(cand)
                if tick and tick.bid > 0:
                    _RESOLVED_SYMBOL = cand
                    _INSTRUMENT = cand
                    logger.warning(
                        "[MT5] Symbol '%s' not found on broker; using '%s'",
                        preferred, cand,
                    )
                    return cand

        logger.error(
            "[MT5] No XAU/USD symbol on broker. preferred='%s' candidates=%s",
            preferred, candidates[:10],
        )
        return None
    except Exception as e:
        logger.error("[MT5] _resolve_symbol error: %s", e)
        return None


def _ensure_mt5() -> bool:
    """Initialize / reconnect MT5. Returns True when ready."""
    global _MT5_INITIALIZED
    try:
        import MetaTrader5 as mt5

        if _MT5_INITIALIZED:
            info = mt5.account_info()
            if info is not None:
                return True
            logger.warning("[MT5] Connection lost — reconnecting")
            _MT5_INITIALIZED = False

        # Try MT5_PATH first (pins us to the exact terminal install with the
        # correct broker). Fall back to bare attach if path launch fails —
        # picks up whichever MT5 is already registered on the machine.
        ok = mt5.initialize(path=MT5_PATH, login=MT5_LOGIN,
                            password=MT5_PASSWORD, server=MT5_SERVER)
        if not ok:
            logger.warning(
                "[MT5] Launch via MT5_PATH=%s failed (%s); bare attach",
                MT5_PATH, mt5.last_error(),
            )
            ok = mt5.initialize(login=MT5_LOGIN,
                                password=MT5_PASSWORD, server=MT5_SERVER)
        if ok:
            info = mt5.account_info()
            logger.info(
                "[MT5] Connected: account=%s balance=%.2f %s",
                info.login, info.balance, info.currency,
            )
            _MT5_INITIALIZED = True
            _resolve_symbol(_INSTRUMENT)
            return True

        logger.error("[MT5] Init failed: %s", mt5.last_error())
        return False
    except ImportError:
        logger.error("[MT5] MetaTrader5 package not installed")
        return False
    except Exception as e:
        logger.error("[MT5] Init error: %s", e)
        return False


def _lot_to_volume(lot_size: float) -> float:
    return max(0.01, round(lot_size, 2))


def _sym(symbol: Optional[str]) -> str:
    """Return the broker's resolved gold symbol regardless of caller input."""
    resolved = _RESOLVED_SYMBOL or _resolve_symbol(_INSTRUMENT)
    return resolved or (symbol or _INSTRUMENT)


def _pick_filling_mode(info) -> int:
    """Pick a filling mode the broker supports.

    `info.filling_mode` is a bitmask of SYMBOL_FILLING_FOK (1) /
    SYMBOL_FILLING_IOC (2). If neither is set, fall back to RETURN.
    """
    import MetaTrader5 as mt5
    mask = int(getattr(info, "filling_mode", 0) or 0) if info else 0
    if mask & 2:
        return mt5.ORDER_FILLING_IOC
    if mask & 1:
        return mt5.ORDER_FILLING_FOK
    return mt5.ORDER_FILLING_RETURN


# ---------------------------------------------------------------------------
# Current price
# ---------------------------------------------------------------------------

def get_current_price_sync(symbol: Optional[str] = None) -> Optional[float]:
    """Mid-price (bid+ask)/2, rounded to 2dp. Synchronous."""
    if not _ensure_mt5():
        return None
    try:
        import MetaTrader5 as mt5
        s = _sym(symbol)
        mt5.symbol_select(s, True)
        tick = mt5.symbol_info_tick(s)
        if tick is None:
            logger.warning("[MT5] No tick for %s: %s", s, mt5.last_error())
            return None
        return round((tick.bid + tick.ask) / 2, 2)
    except Exception as e:
        logger.error("[MT5] get_current_price error: %s", e)
        return None


async def get_current_price(symbol: Optional[str] = None) -> Optional[float]:
    return await asyncio.get_event_loop().run_in_executor(
        None, get_current_price_sync, symbol,
    )


# ---------------------------------------------------------------------------
# Open position snapshots
# ---------------------------------------------------------------------------

def get_open_position_sync(
    symbol: Optional[str] = None,
    magic: Optional[int] = None,
) -> Optional[str]:
    """'buy' if net long, 'sell' if net short, None if flat.

    Optional ``magic`` restricts the check to this agent's positions so
    other EAs / manual trades on the same symbol don't confuse state sync.
    """
    if not _ensure_mt5():
        return None
    try:
        import MetaTrader5 as mt5
        s = _sym(symbol)
        positions = mt5.positions_get(symbol=s)
        if not positions:
            return None
        if magic is not None:
            positions = [p for p in positions if int(getattr(p, "magic", 0)) == int(magic)]
            if not positions:
                return None
        net = sum(p.volume if p.type == 0 else -p.volume for p in positions)
        if net > 0:
            return "buy"
        if net < 0:
            return "sell"
        return None
    except Exception as e:
        logger.error("[MT5] get_open_position error: %s", e)
        return None


async def get_open_position(
    symbol: Optional[str] = None,
    magic: Optional[int] = None,
) -> Optional[str]:
    return await asyncio.get_event_loop().run_in_executor(
        None, get_open_position_sync, symbol, magic,
    )


def get_position_details_sync(
    symbol: Optional[str] = None,
    magic: Optional[int] = None,
) -> Optional[dict]:
    """Return {side, fill_price, current_price, units, ticket, sl, tp}.

    When ``magic`` is provided, only positions with a matching ``magic``
    field are considered. Callers (ai_agent trail loop, external-close
    detection) pass ``MAGIC_NUMBER`` so another agent's / EA's position on
    the same symbol is ignored.
    """
    if not _ensure_mt5():
        return None
    try:
        import MetaTrader5 as mt5
        s = _sym(symbol)
        mt5.symbol_select(s, True)
        positions = mt5.positions_get(symbol=s)
        if not positions:
            return None
        if magic is not None:
            positions = [p for p in positions if int(getattr(p, "magic", 0)) == int(magic)]
            if not positions:
                return None

        tick = mt5.symbol_info_tick(s)
        current_price = round((tick.bid + tick.ask) / 2, 2) if tick else None
        pos = positions[0]
        return {
            "side":          "buy" if pos.type == 0 else "sell",
            "fill_price":    round(pos.price_open, 2),
            "current_price": current_price,
            "units":         pos.volume,
            "ticket":        pos.ticket,
            "sl":            pos.sl,
            "tp":            pos.tp,
        }
    except Exception as e:
        logger.error("[MT5] get_position_details error: %s", e)
        return None


async def get_position_details(
    symbol: Optional[str] = None,
    magic: Optional[int] = None,
) -> Optional[dict]:
    return await asyncio.get_event_loop().run_in_executor(
        None, get_position_details_sync, symbol, magic,
    )


def get_positions_list_sync(symbol: Optional[str] = None) -> list[dict]:
    """Return all open MT5 positions (list of dicts)."""
    if not _ensure_mt5():
        return []
    try:
        import MetaTrader5 as mt5
        s = _sym(symbol)
        positions = mt5.positions_get(symbol=s) or []
        out = []
        for p in positions:
            out.append({
                "ticket":        int(p.ticket),
                "symbol":        p.symbol,
                "side":          "buy" if p.type == 0 else "sell",
                "volume":        float(p.volume),
                "open_price":    round(float(p.price_open), 2),
                "current_price": round(float(p.price_current or 0.0), 2),
                "sl":            round(float(p.sl or 0.0), 2),
                "tp":            round(float(p.tp or 0.0), 2),
                "profit":        round(float(p.profit or 0.0), 2),
                "open_time":     int(p.time or 0),
            })
        return out
    except Exception as e:
        logger.error("[MT5] get_positions_list error: %s", e)
        return []


# ---------------------------------------------------------------------------
# SL/TP adjustment
# ---------------------------------------------------------------------------

def _adjust_sl_tp(
    direction: str,
    ask: float,
    bid: float,
    stop_loss: Optional[float],
    take_profit: Optional[float],
    min_distance: float,
    ai_ref_price: Optional[float] = None,
) -> tuple[Optional[float], Optional[float], list[str]]:
    """Translate + clamp SL/TP into the broker's stops-rules frame."""
    notes: list[str] = []
    ref = ask if direction == "buy" else bid

    if ai_ref_price and abs(ref - ai_ref_price) > min_distance:
        offset = ref - ai_ref_price
        if stop_loss:
            stop_loss = round(stop_loss + offset, 2)
        if take_profit:
            take_profit = round(take_profit + offset, 2)
        notes.append(
            f"shifted SL/TP by {offset:+.2f} "
            f"(AI ref=${ai_ref_price:.2f} vs MT5 ref=${ref:.2f})"
        )

    if direction == "buy":
        if stop_loss and stop_loss > ref - min_distance:
            new_sl = round(ref - 2 * min_distance, 2)
            notes.append(f"SL {stop_loss:.2f} too close/wrong side → {new_sl:.2f}")
            stop_loss = new_sl
        if take_profit and take_profit < ref + min_distance:
            new_tp = round(ref + 2 * min_distance, 2)
            notes.append(f"TP {take_profit:.2f} too close/wrong side → {new_tp:.2f}")
            take_profit = new_tp
    else:
        if stop_loss and stop_loss < ref + min_distance:
            new_sl = round(ref + 2 * min_distance, 2)
            notes.append(f"SL {stop_loss:.2f} too close/wrong side → {new_sl:.2f}")
            stop_loss = new_sl
        if take_profit and take_profit > ref - min_distance:
            new_tp = round(ref - 2 * min_distance, 2)
            notes.append(f"TP {take_profit:.2f} too close/wrong side → {new_tp:.2f}")
            take_profit = new_tp

    return stop_loss, take_profit, notes


# ---------------------------------------------------------------------------
# Place order
# ---------------------------------------------------------------------------

def _place_order_sync(
    instrument: Optional[str],
    direction: str,
    lot_size: float,
    stop_loss: Optional[float],
    take_profit: Optional[float],
    ai_ref_price: Optional[float] = None,
    magic: int = 202400,
) -> Optional[float]:
    """Place a MARKET order. Returns fill price on success, None on failure."""
    if not _ensure_mt5():
        return None
    try:
        import MetaTrader5 as mt5

        s = _sym(instrument)
        mt5.symbol_select(s, True)
        tick = mt5.symbol_info_tick(s)
        info = mt5.symbol_info(s)
        if tick is None or info is None:
            logger.error("[MT5] No tick/info for %s", s)
            return None

        order_type = mt5.ORDER_TYPE_BUY if direction == "buy" else mt5.ORDER_TYPE_SELL
        price      = tick.ask if direction == "buy" else tick.bid
        volume     = _lot_to_volume(lot_size)

        point    = float(info.point or 0.01)
        stops_lv = int(info.trade_stops_level or 0)
        # Respect broker's minimum stop distance, with a 10-pt safety floor
        # ($0.10 on XAUUSD) so stops aren't so close they trigger on one
        # tick of noise. Previously hardcoded 50-pt ($0.50) which silently
        # widened every tight-SL scalp on IC Markets (stops_level=0).
        min_dist = max(stops_lv, 10) * point

        stop_loss, take_profit, notes = _adjust_sl_tp(
            direction, tick.ask, tick.bid, stop_loss, take_profit,
            min_dist, ai_ref_price,
        )
        for note in notes:
            logger.warning("[MT5] %s", note)

        request = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       s,
            "volume":       volume,
            "type":         order_type,
            "price":        price,
            "deviation":    20,
            "magic":        magic,
            "comment":      "AI-Trader",
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": _pick_filling_mode(info),
        }
        if stop_loss and stop_loss > 0:
            request["sl"] = round(stop_loss, 2)
        if take_profit and take_profit > 0:
            request["tp"] = round(take_profit, 2)

        result = mt5.order_send(request)
        if result is None:
            logger.error("[MT5] order_send returned None: %s", mt5.last_error())
            return None

        if result.retcode == mt5.TRADE_RETCODE_DONE:
            fill_price = round(result.price, 2)
            _LAST_OPENED_TICKET[int(magic)] = int(result.order)
            logger.info(
                "[MT5] %s %s lots filled @ %.2f (ticket=%s SL=%s TP=%s)",
                direction.upper(), volume, fill_price, result.order,
                stop_loss, take_profit,
            )
            return fill_price

        logger.error("[MT5] Order failed retcode=%s (%s)",
                     result.retcode, result.comment)
        return None
    except Exception as e:
        logger.error("[MT5] Order error: %s", e)
        return None


async def execute_mt5_trade(
    instrument: Optional[str] = None,
    direction: str = "buy",
    lot_size: float = 0.01,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
    ai_ref_price: Optional[float] = None,
    magic: int = 202400,
) -> Optional[float]:
    """Async market order. Returns the actual MT5 fill price, or None."""
    async with _trade_lock:
        return await asyncio.get_event_loop().run_in_executor(
            None,
            _place_order_sync,
            instrument, direction, lot_size, stop_loss, take_profit,
            ai_ref_price, magic,
        )


# ---------------------------------------------------------------------------
# Close all
# ---------------------------------------------------------------------------

def _close_all_sync(symbol: Optional[str], magic: int = 202400) -> int:
    """Close all positions for this symbol AND matching magic. Returns 1 if
    something closed.

    Magic filter is critical: multiple agents or a manual EA sharing the
    terminal would otherwise get their positions closed when this agent's
    trail/flip logic fires. MetaTrader's positions_get has no magic kwarg,
    so we filter after the fetch.
    """
    if not _ensure_mt5():
        return 0
    try:
        import MetaTrader5 as mt5
        s = _sym(symbol)
        all_positions = mt5.positions_get(symbol=s)
        if not all_positions:
            logger.info("[MT5] No open positions for %s", s)
            return 0
        positions = [p for p in all_positions if int(getattr(p, "magic", 0)) == int(magic)]
        if not positions:
            logger.info(
                "[MT5] No positions matching magic=%s for %s (found %d other)",
                magic, s, len(all_positions),
            )
            return 0

        mt5.symbol_select(s, True)
        tick = mt5.symbol_info_tick(s)
        info = mt5.symbol_info(s)
        type_filling = _pick_filling_mode(info)

        closed = 0
        for pos in positions:
            close_type  = mt5.ORDER_TYPE_SELL if pos.type == 0 else mt5.ORDER_TYPE_BUY
            close_price = tick.bid if pos.type == 0 else tick.ask
            req = {
                "action":       mt5.TRADE_ACTION_DEAL,
                "symbol":       s,
                "volume":       pos.volume,
                "type":         close_type,
                "position":     pos.ticket,
                "price":        close_price,
                "deviation":    20,
                "magic":        magic,
                "comment":      "AI-Trader close",
                "type_time":    mt5.ORDER_TIME_GTC,
                "type_filling": type_filling,
            }
            res = mt5.order_send(req)
            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                logger.info("[MT5] Closed ticket=%s @ %.2f", pos.ticket, res.price)
                closed += 1
            else:
                logger.error("[MT5] Close failed: %s",
                             res.comment if res else mt5.last_error())

        return 1 if closed > 0 else 0
    except Exception as e:
        logger.error("[MT5] close_all error: %s", e)
        return 0


async def close_all_positions(symbol: Optional[str] = None, magic: int = 202400) -> int:
    async with _trade_lock:
        return await asyncio.get_event_loop().run_in_executor(
            None, _close_all_sync, symbol, magic,
        )


# ---------------------------------------------------------------------------
# Modify SL/TP
# ---------------------------------------------------------------------------

def _modify_sl_tp_sync(
    symbol: Optional[str],
    new_sl: Optional[float],
    new_tp: Optional[float],
) -> bool:
    """Modify SL/TP on open positions, respecting the broker's minimum
    stop-distance (`info.trade_stops_level`). IC Markets and most ECN
    brokers reject a TRADE_ACTION_SLTP with retcode 'Invalid stops' when
    the new SL is closer to the current price than stops_level points.

    When the requested SL would violate the stop level, we push it out to
    exactly `stops_level + 1` points away instead of failing. This is
    strictly safer: the SL ends up slightly worse than requested (further
    from profit for a ratchet, closer to entry for an initial SL), never
    more aggressive.
    """
    if not _ensure_mt5():
        return False
    try:
        import MetaTrader5 as mt5
        s = _sym(symbol)
        info = mt5.symbol_info(s)
        tick = mt5.symbol_info_tick(s)
        positions = mt5.positions_get(symbol=s)
        if not positions or not info or not tick:
            return False

        point       = float(info.point or 0.01)
        min_dist_pt = int(getattr(info, "trade_stops_level", 0) or 0)
        min_dist    = (min_dist_pt + 1) * point  # +1 pt safety margin

        modified = False
        for pos in positions:
            # Use latest tick for distance check (pos.price_current may be stale)
            ref_bid = float(tick.bid)
            ref_ask = float(tick.ask)

            sl = round(new_sl, 2) if new_sl else pos.sl
            tp = round(new_tp, 2) if new_tp else pos.tp

            # Clamp SL to broker's minimum distance from current market.
            if new_sl and min_dist > 0:
                if pos.type == 0:  # buy: SL must be BELOW bid by min_dist
                    floor = round(ref_bid - min_dist, 2)
                    if sl > floor:
                        logger.info(
                            "[MT5] SL %.2f too close to bid %.2f (min dist "
                            "%.2f = %d pts) — pushing to %.2f",
                            sl, ref_bid, min_dist, min_dist_pt, floor,
                        )
                        sl = floor
                else:  # sell: SL must be ABOVE ask by min_dist
                    ceil = round(ref_ask + min_dist, 2)
                    if sl < ceil:
                        logger.info(
                            "[MT5] SL %.2f too close to ask %.2f (min dist "
                            "%.2f = %d pts) — pushing to %.2f",
                            sl, ref_ask, min_dist, min_dist_pt, ceil,
                        )
                        sl = ceil

            # Same clamp for TP (broker rejects TP inside min distance too)
            if new_tp and min_dist > 0:
                if pos.type == 0:  # buy: TP must be ABOVE ask by min_dist
                    floor_tp = round(ref_ask + min_dist, 2)
                    if tp < floor_tp:
                        tp = floor_tp
                else:  # sell: TP must be BELOW bid by min_dist
                    ceil_tp = round(ref_bid - min_dist, 2)
                    if tp > ceil_tp:
                        tp = ceil_tp

            req = {
                "action":   mt5.TRADE_ACTION_SLTP,
                "symbol":   s,
                "position": pos.ticket,
                "sl":       sl,
                "tp":       tp,
            }
            res = mt5.order_send(req)
            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                logger.info("[MT5] Modified ticket=%s SL=%s TP=%s", pos.ticket, sl, tp)
                modified = True
            else:
                logger.warning("[MT5] Modify failed: %s (sl=%s tp=%s stops_lvl=%dpt)",
                               res.comment if res else mt5.last_error(),
                               sl, tp, min_dist_pt)
        return modified
    except Exception as e:
        logger.error("[MT5] modify error: %s", e)
        return False


async def modify_position_sl_tp(
    instrument: Optional[str] = None,
    new_sl: Optional[float] = None,
    new_tp: Optional[float] = None,
) -> bool:
    return await asyncio.get_event_loop().run_in_executor(
        None, _modify_sl_tp_sync, instrument, new_sl, new_tp,
    )


async def modify_position_sl(instrument: Optional[str], new_sl: float) -> bool:
    return await modify_position_sl_tp(instrument, new_sl=new_sl, new_tp=None)


# ---------------------------------------------------------------------------
# Deal history
# ---------------------------------------------------------------------------

def get_last_deal_sync(
    symbol: Optional[str] = None,
    since_seconds: int = 30,
    magic: int = 202400,
) -> Optional[dict]:
    """Most recent deal for this symbol/magic, any entry type."""
    if not _ensure_mt5():
        return None
    try:
        import MetaTrader5 as mt5
        s = _sym(symbol)
        now = datetime.now(timezone.utc)
        since = now - timedelta(seconds=max(5, since_seconds))
        deals = mt5.history_deals_get(since, now)
        if not deals:
            return None
        ours = [d for d in deals if d.magic == magic and d.symbol == s]
        if not ours:
            return None
        ours.sort(key=lambda d: d.time, reverse=True)
        d = ours[0]
        return {
            "price":  round(d.price, 2),
            "entry":  d.entry,
            "time":   d.time,
            "ticket": d.ticket,
            "volume": d.volume,
            "type":   d.type,
            "profit": d.profit,
        }
    except Exception as e:
        logger.error("[MT5] get_last_deal error: %s", e)
        return None


def get_last_close_deal_sync(
    symbol: Optional[str] = None,
    since_seconds: int = 60,
    magic: int = 202400,
    after_unix: Optional[float] = None,
    position_id: Optional[int] = None,
) -> Optional[dict]:
    """Most recent CLOSING (OUT) deal, strictly filtered by position_id when
    supplied — the ONLY reliable way to avoid cross-agent contamination when
    multiple traders share a magic number."""
    if not _ensure_mt5():
        return None
    try:
        import MetaTrader5 as mt5
        s = _sym(symbol)
        # UTC-aware datetimes — MT5 treats naive as UTC, naive local
        # would shift the window on non-UTC hosts.
        now = datetime.now(timezone.utc)
        since = now - timedelta(seconds=max(10, since_seconds))
        deals = mt5.history_deals_get(since, now)
        if not deals:
            return None

        closings = [
            d for d in deals
            if d.magic == magic and d.symbol == s and d.entry == mt5.DEAL_ENTRY_OUT
        ]
        if position_id is not None:
            closings = [
                d for d in closings
                if getattr(d, "position_id", 0) == int(position_id)
            ]
        if after_unix is not None:
            closings = [d for d in closings if getattr(d, "time", 0) >= after_unix]
        if not closings:
            return None
        closings.sort(key=lambda d: d.time, reverse=True)
        d = closings[0]
        return {
            "price":  round(d.price, 2),
            "time":   d.time,
            "ticket": d.ticket,
            "volume": d.volume,
            "profit": d.profit,
        }
    except Exception as e:
        logger.error("[MT5] get_last_close_deal error: %s", e)
        return None


# ---------------------------------------------------------------------------
# Position history (with live overlay)
# ---------------------------------------------------------------------------

def get_history_positions_sync(
    symbol: Optional[str] = None,
    since_hours: int = 72,
    magic: Optional[int] = None,
) -> list[dict]:
    """Return recent positions with MT5-backed pricing metadata.

    Each item: {position_id, ticket, symbol, side, volume, open_time,
                open_price, sl, tp, close_time, close_price, profit, closed}.
    For still-open positions, `sl`/`tp`/`profit` are overlaid from the live
    `positions_get` snapshot, while `match_sl`/`match_tp` preserve the
    original opening-order values for signal matching.
    """
    if not _ensure_mt5():
        return []
    try:
        import MetaTrader5 as mt5
    except ImportError:
        logger.error("[MT5] MetaTrader5 package not installed")
        return []

    s = _sym(symbol)
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=max(1, since_hours))
    deals  = mt5.history_deals_get(since, now)  or []
    orders = mt5.history_orders_get(since, now) or []
    live_positions = mt5.positions_get(symbol=s) or []

    def _flt(o):
        if s and getattr(o, "symbol", "") != s:
            return False
        if magic is not None and getattr(o, "magic", 0) != magic:
            return False
        return True

    deals  = [d for d in deals  if _flt(d)]
    orders = [o for o in orders if _flt(o)]

    positions: dict[int, dict] = {}
    for d in deals:
        pid = int(d.position_id or 0)
        if pid == 0:
            continue
        slot = positions.setdefault(pid, {"in": None, "out": None, "profit": 0.0})
        if d.entry == mt5.DEAL_ENTRY_IN:
            if slot["in"] is None or d.time < slot["in"].time:
                slot["in"] = d
        elif d.entry == mt5.DEAL_ENTRY_OUT:
            if slot["out"] is None or d.time > slot["out"].time:
                slot["out"] = d
        slot["profit"] += (
            float(d.profit or 0)
            + float(getattr(d, "swap", 0) or 0)
            + float(getattr(d, "commission", 0) or 0)
        )

    opening_order_by_pid: dict[int, object] = {}
    for o in orders:
        if int(o.type) not in (mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_SELL):
            continue
        pid = int(getattr(o, "position_id", 0) or o.ticket)
        prev = opening_order_by_pid.get(pid)
        if prev is None or o.time_setup < prev.time_setup:
            opening_order_by_pid[pid] = o

    def _iso(ts: int) -> str:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc) \
                       .isoformat().replace("+00:00", "Z")

    live_by_pid: dict[int, dict] = {}
    for pos in live_positions:
        try:
            pid = int(getattr(pos, "ticket", 0) or getattr(pos, "identifier", 0) or 0)
        except Exception:
            pid = 0
        if pid == 0:
            continue
        side = "buy" if int(pos.type) == mt5.ORDER_TYPE_BUY else "sell"
        cur = getattr(pos, "price_current", None)
        live_by_pid[pid] = {
            "sl":            round(float(getattr(pos, "sl", 0.0) or 0.0), 2),
            "tp":            round(float(getattr(pos, "tp", 0.0) or 0.0), 2),
            "profit":        round(float(getattr(pos, "profit", 0.0) or 0.0), 2),
            "current_price": round(float(cur), 2) if cur else None,
            "side":          side,
        }

    out: list[dict] = []
    for pid, slot in positions.items():
        ind = slot["in"]
        if not ind:
            continue
        outd = slot["out"]
        oo = opening_order_by_pid.get(pid)
        side = "buy" if int(ind.type) == mt5.DEAL_TYPE_BUY else "sell"
        row = {
            "position_id":  pid,
            "ticket":       int(ind.ticket),
            "symbol":       ind.symbol,
            "side":         side,
            "volume":       float(ind.volume),
            "open_time":    _iso(ind.time),
            "open_price":   round(float(ind.price), 2),
            "sl":           round(float(oo.sl), 2) if oo and oo.sl else 0.0,
            "tp":           round(float(oo.tp), 2) if oo and oo.tp else 0.0,
            "match_sl":     round(float(oo.sl), 2) if oo and oo.sl else 0.0,
            "match_tp":     round(float(oo.tp), 2) if oo and oo.tp else 0.0,
            "close_time":   _iso(outd.time) if outd else None,
            "close_price":  round(float(outd.price), 2) if outd else None,
            "profit":       round(float(slot["profit"]), 2),
            "closed":       outd is not None,
        }
        out.append(row)

    # Overlay live data for still-open positions
    for item in out:
        if item.get("closed"):
            continue
        live = live_by_pid.get(int(item.get("position_id") or 0))
        if not live:
            continue
        item["sl"]            = live["sl"]
        item["tp"]            = live["tp"]
        item["current_price"] = live["current_price"]
        item["profit"]        = live["profit"]

    out.sort(key=lambda x: x["open_time"], reverse=True)
    return out


async def get_history_positions(
    symbol: Optional[str] = None,
    since_hours: int = 72,
    magic: Optional[int] = None,
) -> list[dict]:
    return await asyncio.get_event_loop().run_in_executor(
        None, get_history_positions_sync, symbol, since_hours, magic,
    )


# ---------------------------------------------------------------------------
# Market snapshot (recent bars) — feeds LLM prompt context
# ---------------------------------------------------------------------------

def _rsi(closes: list, period: int = 14) -> Optional[float]:
    """Classic Wilder RSI on a closes series. Returns None if too few bars."""
    if len(closes) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        delta = closes[i] - closes[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    avg_g = gains / period
    avg_l = losses / period
    for i in range(period + 1, len(closes)):
        delta = closes[i] - closes[i - 1]
        g = delta if delta > 0 else 0.0
        l = -delta if delta < 0 else 0.0
        avg_g = (avg_g * (period - 1) + g) / period
        avg_l = (avg_l * (period - 1) + l) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return round(100.0 - (100.0 / (1.0 + rs)), 1)


# ---------------------------------------------------------------------------
# Macro context — DXY (US Dollar Index) via TwelveData
# ---------------------------------------------------------------------------
# Gold is inversely correlated to DXY. Having the current DXY + recent change
# in the snapshot lets the model confirm/reject direction: BB break up + DXY
# falling = stronger BUY. BB break down + DXY rising = stronger SELL.
# Cache 5 min to stay under TwelveData free-tier limits (8 req/min, 800/day).

_DXY_CACHE: dict = {"ts": 0.0, "price": None, "change_pct": None}
_DXY_TTL_SECS = 300


def _fetch_dxy() -> tuple[Optional[float], Optional[float]]:
    """Return (dxy_price, dxy_change_pct) with 5-min in-memory cache.
    Returns (None, None) on any failure — snapshot degrades gracefully."""
    now = time.time()
    if (now - _DXY_CACHE["ts"]) < _DXY_TTL_SECS and _DXY_CACHE["price"] is not None:
        return _DXY_CACHE["price"], _DXY_CACHE["change_pct"]
    key = os.getenv("TWELVE_DATA_API_KEY", "").strip()
    if not key:
        return None, None
    try:
        url = (
            "https://api.twelvedata.com/quote?"
            + urllib.parse.urlencode({"symbol": "DXY", "apikey": key})
        )
        req = urllib.request.Request(url, headers={"User-Agent": "ai-trader/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if not isinstance(data, dict) or "close" not in data:
            return None, None
        price = float(data["close"])
        change_pct = float(data.get("percent_change", 0.0))
        _DXY_CACHE["ts"] = now
        _DXY_CACHE["price"] = price
        _DXY_CACHE["change_pct"] = change_pct
        return price, change_pct
    except Exception as e:
        logger.debug("[DXY] fetch failed: %s", e)
        return None, None


def _ema(values: list, period: int) -> Optional[float]:
    """Standard EMA. Seeds with SMA over first `period`, then applies the
    k = 2/(period+1) multiplier for each remaining bar. Returns None when
    there aren't enough bars."""
    if not values or len(values) < period:
        return None
    sma = sum(values[:period]) / period
    k = 2.0 / (period + 1)
    ema = sma
    for v in values[period:]:
        ema = (v - ema) * k + ema
    return ema


def _bbands(closes: list, period: int = 20, stddev: float = 2.0) -> tuple:
    """Bollinger Bands on the last `period` closes. Returns
    (upper, middle, lower) or (None, None, None) if too few bars."""
    if len(closes) < period:
        return (None, None, None)
    window = closes[-period:]
    mid = sum(window) / period
    var = sum((c - mid) ** 2 for c in window) / period
    sd = var ** 0.5
    return (mid + stddev * sd, mid, mid - stddev * sd)


def _swing_levels(highs: list, lows: list, lookback: int = 3) -> tuple:
    """Find the nearest recent swing high above cur and swing low below cur.

    A swing high is a bar whose high is >= `lookback` bars on each side.
    Returns (nearest_resistance, nearest_support). Either may be None when
    no qualifying swing exists in the window.
    """
    n = len(highs)
    if n < lookback * 2 + 1:
        return (None, None)
    cur = (highs[-1] + lows[-1]) / 2.0
    swing_hi = []
    swing_lo = []
    for i in range(lookback, n - lookback):
        window_h = highs[i - lookback:i + lookback + 1]
        window_l = lows[i - lookback:i + lookback + 1]
        if highs[i] == max(window_h):
            swing_hi.append(highs[i])
        if lows[i] == min(window_l):
            swing_lo.append(lows[i])
    # Nearest resistance = lowest swing-high strictly above current price.
    resistance = min((h for h in swing_hi if h > cur), default=None)
    # Nearest support = highest swing-low strictly below current price.
    support    = max((l for l in swing_lo if l < cur), default=None)
    return (resistance, support)


def _session_label(utc_hour: int) -> str:
    """XAUUSD liquidity label based on UTC hour."""
    if   0 <= utc_hour < 7:   return "asia"
    elif 7 <= utc_hour < 12:  return "london"
    elif 12 <= utc_hour < 16: return "ny_overlap"  # London+NY — peak volume
    elif 16 <= utc_hour < 21: return "ny_afternoon"
    else:                     return "off_hours"


# HTF trend cache: H4/D1 trends rarely change inside an M30 cycle. Cache
# for 30 min (H4) / 4 h (D1) to cut MT5 bar-fetch round-trips.
_HTF_CACHE: dict = {}  # key=(symbol, timeframe_const) -> (ts, value)
_HTF_TTL_DEFAULT = 1800  # 30 min


def _htf_trend(symbol: str, timeframe_const: int, need: int = 30,
               ttl_secs: int = _HTF_TTL_DEFAULT) -> Optional[str]:
    """Pull higher-timeframe bars and return 'up' / 'down' / 'flat' based on
    SMA10 vs SMA30 separation. None if the fetch fails.

    Results are cached per (symbol, timeframe) for ``ttl_secs``. Higher
    timeframes shift slowly enough that re-fetching every M30 cycle is
    wasted IPC; a 30-min cache matches the system's decision cadence.
    """
    key = (symbol, int(timeframe_const))
    now = time.time()
    cached = _HTF_CACHE.get(key)
    if cached and (now - cached[0]) < ttl_secs:
        return cached[1]
    try:
        import MetaTrader5 as mt5
        rates = mt5.copy_rates_from_pos(symbol, timeframe_const, 0, need)
        if rates is None or len(rates) < need:
            return None
        closes = [float(r["close"]) for r in rates]
        highs  = [float(r["high"])  for r in rates]
        lows   = [float(r["low"])   for r in rates]
        sma10 = sum(closes[-10:]) / 10
        sma30 = sum(closes[-30:]) / 30
        atr   = sum(h - l for h, l in zip(highs, lows)) / len(highs)
        sep   = abs(sma10 - sma30)
        if sep < 0.1 * atr:
            out = "flat"
        else:
            out = "up" if sma10 > sma30 else "down"
        _HTF_CACHE[key] = (now, out)
        return out
    except Exception:
        return None


# Snapshot cache: every cycle historically fetched the M30 snapshot 2-3
# times (pre-LLM gate, LLM prompt build, postdecision ATR). Same 30-min
# bar data in each case. Cache for SNAPSHOT_TTL_SECS (default 30s) so one
# cycle re-uses one fetch; long enough to dedupe, short enough that the
# next M30 close still invalidates mid-cycle.
_SNAPSHOT_CACHE: dict = {}  # key=(symbol, bars) -> (ts, value)
_SNAPSHOT_TTL_SECS = 30


def get_market_snapshot_sync(
    symbol: Optional[str] = None,
    bars: int = 48,
    *,
    fresh: bool = False,
) -> Optional[dict]:
    """Return a compact summary of the last `bars` M30 candles for the LLM
    prompt: current close, session high/low, momentum vs `bars` ago, average
    true range, higher-timeframe trends, RSI, nearest S/R levels, BB20/2,
    EMA200 long-term trend anchor, volume vs 20-bar MA, session label, and
    the last 6 closes. All prices rounded to 2dp.

    Results cached ~30s per (symbol, bars). Pass ``fresh=True`` to bypass.

    Returns None if MT5 is unavailable or the bar request fails.
    """
    if not fresh:
        key = (_sym(symbol) if symbol else _sym(None), int(bars))
        cached = _SNAPSHOT_CACHE.get(key)
        if cached and (time.time() - cached[0]) < _SNAPSHOT_TTL_SECS:
            return cached[1]
    if not _ensure_mt5():
        return None
    try:
        import MetaTrader5 as mt5
        import datetime as _dt
        symbol = _sym(symbol)
        mt5.symbol_select(symbol, True)
        # Pull 210 bars so EMA200 has enough history. Stats that only need
        # `bars` (momentum, session hi/lo, ATR) still use the last `bars` slice.
        need = max(6, bars, 210)
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M30, 0, need)
        if rates is None or len(rates) < 6:
            return None

        closes_full = [float(r["close"]) for r in rates]
        highs_full  = [float(r["high"])  for r in rates]
        lows_full   = [float(r["low"])   for r in rates]
        vols_full   = [float(r["tick_volume"]) for r in rates]

        # Keep the `bars`-sized slice for "session" / momentum stats.
        closes = closes_full[-bars:] if len(closes_full) > bars else closes_full
        highs  = highs_full [-bars:] if len(highs_full)  > bars else highs_full
        lows   = lows_full  [-bars:] if len(lows_full)   > bars else lows_full

        cur        = closes[-1]
        momentum   = cur - closes[0]
        session_hi = max(highs)
        session_lo = min(lows)

        # Candle maturity: fraction of current M30 bar elapsed (0.0-1.0).
        # MT5 bar["time"] is broker server time (e.g. GMT+3 on IC Markets),
        # NOT UTC. Using _time.time() (UTC) causes a 3h offset → negative age.
        # Fix: use mt5.symbol_info_tick().time which is also broker server time,
        # so both sides of the subtraction are in the same timezone.
        try:
            tick_now = mt5.symbol_info_tick(symbol)
            server_now = float(tick_now.time) if tick_now else float(rates[-1]["time"]) + 1800
        except Exception:
            server_now = float(rates[-1]["time"]) + 1800
        last_bar_open_ts = float(rates[-1]["time"])
        elapsed = server_now - last_bar_open_ts
        candle_age_pct = round(min(max(elapsed / 1800.0, 0.0), 1.0), 2)

        # Simple ATR (mean of high-low over window)
        atr = sum(h - l for h, l in zip(highs, lows)) / len(highs)

        # Short moving averages on close
        def _sma(n):
            return sum(closes[-n:]) / n if len(closes) >= n else None
        sma10 = _sma(10)
        sma30 = _sma(30)

        # Trend: only call "up" / "down" when the SMAs have meaningful
        # separation relative to current volatility. If sma10 and sma30 are
        # converged (|sep| < 10% of ATR) it's consolidation / regime change,
        # not a trend — mislabeling here was producing a permanent signal
        # contradiction in the LLM prompt (e.g. sma10=sma30=4753 labeled
        # "up" while momentum was -36), which the model could only resolve
        # by holding.
        if sma10 and sma30:
            sep = abs(sma10 - sma30)
            if sep > 0.1 * atr:
                trend = "up" if sma10 > sma30 else "down"
            else:
                trend = "flat"
        else:
            trend = "flat"

        # Position within today's session range, 0..1. 0 = at session low,
        # 1 = at session high. Lets the model spot mean-reversion setups in
        # flat regimes: near 1.0 favors a short toward the middle, near 0.0
        # favors a long.
        rng = session_hi - session_lo
        range_pos = round((cur - session_lo) / rng, 2) if rng > 0 else 0.5

        # Tier-1 additions: higher timeframes, RSI, S/R levels, session.
        rsi14 = _rsi(closes, 14)
        resistance, support = _swing_levels(highs, lows, lookback=3)
        trend_h4 = _htf_trend(symbol, mt5.TIMEFRAME_H4, need=30)
        trend_d1 = _htf_trend(symbol, mt5.TIMEFRAME_D1, need=30)
        session = _session_label(_dt.datetime.utcnow().hour)

        # Tier-2 additions: EMA200 long-term anchor, BB20/2 breakout, volume.
        ema200 = _ema(closes_full, 200)
        bb_up, bb_mid, bb_lo = _bbands(closes_full, period=20, stddev=2.0)
        if bb_up and bb_lo:
            if cur > bb_up:
                bb_break = "above"
            elif cur < bb_lo:
                bb_break = "below"
            else:
                bb_break = "inside"
        else:
            bb_break = None

        # Volume confirmation: last candle vs 20-bar tick-volume MA.
        if len(vols_full) >= 20:
            vol_ma20 = sum(vols_full[-20:]) / 20.0
            vol_ratio = round(vols_full[-1] / vol_ma20, 2) if vol_ma20 > 0 else None
        else:
            vol_ratio = None

        # Macro: DXY snapshot (cached 5min). Gold moves inverse to DXY.
        # Threshold raised from ±0.15% to ±0.30%: intraday DXY routinely
        # swings 0.3-0.5%, so the old floor nearly always flagged "strong"
        # or "weak" and the bias was effectively a coin-flip feature. 0.30%
        # aligns with a meaningful daily move instead of noise.
        dxy_price, dxy_change = _fetch_dxy()
        if dxy_change is not None:
            if dxy_change > 0.30:
                dxy_bias = "strong_usd"    # bearish gold
            elif dxy_change < -0.30:
                dxy_bias = "weak_usd"      # bullish gold
            else:
                dxy_bias = "neutral"
        else:
            dxy_bias = None

        # Long-term trend anchor: price vs EMA200.
        if ema200:
            if cur > ema200 * 1.001:
                trend_long = "up"
            elif cur < ema200 * 0.999:
                trend_long = "down"
            else:
                trend_long = "flat"
        else:
            trend_long = None

        out = {
            "symbol":     symbol,
            "timeframe":  "M30",
            "bars":       len(closes),
            "close":      round(cur, 2),
            "momentum":   round(momentum, 2),
            "session_hi": round(session_hi, 2),
            "session_lo": round(session_lo, 2),
            "atr":        round(atr, 2),
            "sma10":      round(sma10, 2) if sma10 else None,
            "sma30":      round(sma30, 2) if sma30 else None,
            "ema200":     round(ema200, 2) if ema200 else None,
            "trend":      trend,
            "trend_long": trend_long,
            "trend_h4":   trend_h4,
            "trend_d1":   trend_d1,
            "rsi14":      rsi14,
            "resistance": round(resistance, 2) if resistance else None,
            "support":    round(support, 2)    if support    else None,
            "session":    session,
            "range_pos":  range_pos,
            "bb_upper":   round(bb_up, 2)  if bb_up  else None,
            "bb_mid":     round(bb_mid, 2) if bb_mid else None,
            "bb_lower":   round(bb_lo, 2)  if bb_lo  else None,
            "bb_break":   bb_break,
            "vol_ratio":    vol_ratio,
            "candle_age_pct": candle_age_pct,
            "dxy":          round(dxy_price, 2) if dxy_price else None,
            "dxy_change_pct": round(dxy_change, 2) if dxy_change is not None else None,
            "dxy_bias":   dxy_bias,
            "last_closes": [round(c, 2) for c in closes[-6:]],
        }
        _SNAPSHOT_CACHE[(symbol, int(bars))] = (time.time(), out)
        return out
    except Exception as e:
        logger.error("[MT5] market_snapshot error: %s", e)
        return None


async def get_market_snapshot(
    symbol: Optional[str] = None,
    bars: int = 48,
) -> Optional[dict]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, get_market_snapshot_sync, symbol, bars)


# ---------------------------------------------------------------------------
# Startup / shutdown helpers
# ---------------------------------------------------------------------------

async def init_mt5_connection() -> bool:
    """Call at startup to pre-initialize MT5."""
    return await asyncio.get_event_loop().run_in_executor(None, _ensure_mt5)


def close_mt5() -> None:
    """Gracefully shut down the MT5 terminal connection."""
    global _MT5_INITIALIZED
    try:
        import MetaTrader5 as mt5
        mt5.shutdown()
    except Exception:
        pass
    _MT5_INITIALIZED = False
