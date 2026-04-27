"""
backtest_gate.py — replay breakout gate against recent MT5 M30 history.
Answers: how often would PRIMARY / SECONDARY setup have triggered the LLM?

Usage:
    python backend/backtest_gate.py [--bars 500] [--vol-min 1.2]

No LLM is called. Pure indicator math on closed bars.
"""
from __future__ import annotations
import argparse
import os
import sys

# Load .env for MT5 creds and gate params --------------------------------
from pathlib import Path
env_name = os.getenv("AI_TRADER_ENV", "icmarkets")
env_file  = Path(__file__).parent.parent / f".env.{env_name}"
if env_file.exists():
    from dotenv import load_dotenv
    load_dotenv(env_file, override=True)
    print(f"[env] loaded {env_file}")
else:
    print(f"[env] {env_file} not found — using system env")

# MT5 connect
import MetaTrader5 as mt5

MT5_PATH   = os.getenv("MT5_PATH", "")
MT5_LOGIN  = int(os.getenv("MT5_LOGIN", "0"))
MT5_PASS   = os.getenv("MT5_PASSWORD", "")
MT5_SERVER = os.getenv("MT5_SERVER", "")
MT5_SYMBOL = os.getenv("MT5_SYMBOL", "XAUUSD")

def _connect() -> bool:
    kwargs = {}
    if MT5_PATH:
        kwargs["path"] = MT5_PATH
    if not mt5.initialize(**kwargs):
        print("[MT5] initialize failed:", mt5.last_error())
        return False
    if MT5_LOGIN:
        ok = mt5.login(MT5_LOGIN, password=MT5_PASS, server=MT5_SERVER)
        if not ok:
            print("[MT5] login failed:", mt5.last_error())
            return False
    return True

# Indicator helpers (mirrors mt5_trader.py) -----------------------------

def _ema(values: list, period: int):
    if len(values) < period:
        return None
    sma = sum(values[:period]) / period
    k   = 2.0 / (period + 1)
    ema = sma
    for v in values[period:]:
        ema = (v - ema) * k + ema
    return ema


def _bbands(closes: list, period: int = 20, stddev: float = 2.0):
    if len(closes) < period:
        return None, None, None
    w   = closes[-period:]
    mid = sum(w) / period
    sd  = (sum((c - mid) ** 2 for c in w) / period) ** 0.5
    return mid + stddev * sd, mid, mid - stddev * sd


def _sma(closes, n):
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def _rsi(closes: list, period: int = 14):
    if len(closes) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    ag, al = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        g = d if d > 0 else 0.0
        l = -d if d < 0 else 0.0
        ag = (ag * (period - 1) + g) / period
        al = (al * (period - 1) + l) / period
    if al == 0:
        return 100.0
    return round(100.0 - 100.0 / (1.0 + ag / al), 1)


# Gate logic (mirrors ai_agent.py run_cycle gate block) -----------------

def gate_check(
    closes_full: list,
    highs_full:  list,
    lows_full:   list,
    vols_full:   list,
    bars_window: int,
    vol_min:     float,
) -> dict:
    closes = closes_full[-bars_window:]
    highs  = highs_full [-bars_window:]
    lows   = lows_full  [-bars_window:]

    cur = closes[-1]

    # EMA200 + trend_long
    ema200 = _ema(closes_full, 200)
    if ema200:
        if cur > ema200 * 1.001:
            trend_long = "up"
        elif cur < ema200 * 0.999:
            trend_long = "down"
        else:
            trend_long = "flat"
    else:
        trend_long = None

    # BB
    bb_up, bb_mid, bb_lo = _bbands(closes_full, 20, 2.0)
    if bb_up and bb_lo:
        if cur > bb_up:
            bb_break = "above"
        elif cur < bb_lo:
            bb_break = "below"
        else:
            bb_break = "inside"
    else:
        bb_break = None

    # Volume ratio
    if len(vols_full) >= 20:
        vol_ma = sum(vols_full[-20:]) / 20.0
        vol_ratio = round(vols_full[-1] / vol_ma, 2) if vol_ma > 0 else 0.0
    else:
        vol_ratio = 0.0

    # RSI + range_pos
    rsi14 = _rsi(closes, 14)
    if rsi14 is None:
        rsi14 = 50.0
    session_hi = max(highs)
    session_lo = min(lows)
    rng = session_hi - session_lo
    range_pos = round((cur - session_lo) / rng, 2) if rng > 0 else 0.5

    # Gate logic
    primary = (
        (bb_break == "above" and trend_long == "up")
        or (bb_break == "below" and trend_long == "down")
    ) and vol_ratio >= vol_min

    secondary = trend_long == "flat" and (
        (range_pos >= 0.75 and rsi14 >= 70)
        or (range_pos <= 0.25 and rsi14 <= 30)
    )

    return {
        "primary":    primary,
        "secondary":  secondary,
        "bb_break":   bb_break,
        "vol_ratio":  vol_ratio,
        "trend_long": trend_long,
        "rsi14":      rsi14,
        "range_pos":  range_pos,
        "ema200":     round(ema200, 2) if ema200 else None,
        "price":      round(cur, 2),
    }


# Main ------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars",    type=int,   default=500,
                    help="Total M30 bars to pull (default 500 ≈ 10 days)")
    ap.add_argument("--window",  type=int,   default=48,
                    help="Session window bars (default 48 = 24h M30)")
    ap.add_argument("--vol-min", type=float, default=float(os.getenv("VOLUME_RATIO_MIN", "1.2")),
                    help="Minimum vol_ratio for primary gate (default 1.2)")
    args = ap.parse_args()

    NEED = max(args.bars, 210)  # EMA200 needs 200+

    print(f"\n[gate-test] bars={args.bars} window={args.window} vol_min={args.vol_min}")
    print(f"[gate-test] symbol={MT5_SYMBOL}\n")

    if not _connect():
        sys.exit(1)

    mt5.symbol_select(MT5_SYMBOL, True)
    rates = mt5.copy_rates_from_pos(MT5_SYMBOL, mt5.TIMEFRAME_M30, 0, NEED)
    mt5.shutdown()

    if rates is None or len(rates) < 210:
        print("[gate-test] not enough bars:", len(rates) if rates else 0)
        sys.exit(1)

    print(f"[gate-test] got {len(rates)} bars from MT5\n")

    import datetime as _dt
    closes_all = [float(r["close"])       for r in rates]
    highs_all  = [float(r["high"])        for r in rates]
    lows_all   = [float(r["low"])         for r in rates]
    vols_all   = [float(r["tick_volume"]) for r in rates]
    times_all  = [_dt.datetime.utcfromtimestamp(r["time"]) for r in rates]

    total      = 0
    primary_n  = 0
    secondary_n= 0
    skip_n     = 0

    rows_trigger = []

    # Slide over the last `bars` region (skip first 210 for indicator warmup)
    start_idx = 210
    for i in range(start_idx, len(closes_all)):
        # Provide full history up to bar i for indicators
        c_full = closes_all[:i+1]
        h_full = highs_all[:i+1]
        l_full = lows_all[:i+1]
        v_full = vols_all[:i+1]

        # For session stats use last `window` bars
        total += 1
        r = gate_check(c_full, h_full, l_full, v_full, args.window, args.vol_min)

        if r["primary"]:
            primary_n += 1
            rows_trigger.append((times_all[i], "PRIMARY", r))
        elif r["secondary"]:
            secondary_n += 1
            rows_trigger.append((times_all[i], "SECONDARY", r))
        else:
            skip_n += 1

    print("=" * 70)
    print(f"  TOTAL bars evaluated : {total}")
    print(f"  PRIMARY  setups      : {primary_n}  ({100*primary_n/total:.1f}%)")
    print(f"  SECONDARY setups     : {secondary_n}  ({100*secondary_n/total:.1f}%)")
    print(f"  SKIP (no setup)      : {skip_n}  ({100*skip_n/total:.1f}%)")
    print(f"  LLM would have fired : {primary_n + secondary_n} times")
    print("=" * 70)

    if rows_trigger:
        print(f"\nLast 20 triggers:\n")
        for ts, kind, r in rows_trigger[-20:]:
            print(
                f"  {ts} UTC | {kind:9s} | "
                f"bb={r['bb_break']:7s} vol={r['vol_ratio']:.2f} "
                f"trend={r['trend_long']:5s} rsi={r['rsi14']:5.1f} "
                f"rp={r['range_pos']:.2f} price={r['price']}"
            )
    else:
        print("\n  No triggers in evaluated range.")
        print("  Gate is too strict, or recent market is inside-BB + low-vol.")
        print("  Try: --vol-min 1.0  or  --bars 1000")

    print()


if __name__ == "__main__":
    main()
