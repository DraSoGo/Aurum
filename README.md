# 🪙 Aurum: Autonomous Gold Trader

<p align="center">
  <i>An LLM-driven XAUUSD trading agent for MetaTrader 5.</i>
</p>

[![Python](https://img.shields.io/badge/python-3.11+-3776AB.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688.svg)](https://fastapi.tiangolo.com/)
[![MetaTrader 5](https://img.shields.io/badge/MetaTrader-5-0088CC.svg)](https://www.metatrader5.com/)
[![Claude](https://img.shields.io/badge/Anthropic-Claude-D97757.svg)](https://www.anthropic.com/)
[![License: GPL](https://img.shields.io/badge/License-GPLv3-yellow.svg)](https://opensource.org/licenses/GPL-3.0)

**Aurum** — Latin for *gold* — is a single-symbol XAUUSD trading bot that pairs an Anthropic Claude decision core with a MetaTrader 5 execution layer. It runs every 30 minutes, scores the current market against a breakout-follow-trend thesis, and only fires when seven independent gates agree. Claude reasons; the gates discipline.

---

## ✨ Features

- 🧠 **Claude-Native Decision Loop**: Each cycle, Aurum hands Claude (Sonnet 4.x) a JSON snapshot — price, EMA200, BB20/2, RSI14, ATR, M30/H4/D1 trend, DXY bias, range position, tick-volume ratio — and asks for `BUY / SELL / HOLD` plus stop-loss, take-profit, confidence, and reasoning.
- 🚪 **Seven-Stage Gate Stack**: `session → news → preflight → breakout → LLM → confidence floor → sizing`. Any gate can veto. Rejected setups never reach the broker.
- 🪙 **Adaptive Stop-Loss**: SL distance = `max(SL_FLOOR_POINTS, ATR × SL_ATR_MULT)`. Stops scale with volatility instead of being a fixed dollar value that gets stopped out on noise.
- 📐 **Risk-Pct Position Sizing**: `lots = (balance × risk_pct) / (sl_dist × $100)`. Confidence (50–100%) modulates `risk_pct` linearly within `[0.5×, 1.5×]` before sizing.
- 🛡️ **Multi-Layer Risk Gates**: per-trade SL cap, daily loss limit, rolling 7-day loss limit, consecutive-loss cooldown, free-margin floor, max-spread, per-broker MAGIC isolation.
- 📰 **Fail-Closed News Filter**: ForexFactory free JSON calendar, ±30-min blackout around high-impact USD/EUR/GBP releases. If the feed goes stale during the 12-16 UTC US macro window, the gate fails *closed* — no trade, not no filter.
- 🪞 **Shadow Decision Log**: Every cycle writes its full gate state + LLM output to `shadow_decisions`, even when no trade fires. The self-tuner reads this to score setup types and disable losers automatically.
- 🔧 **Rule-Based Self-Tuner**: Six rules. Adjusts SL floor, trail-stagnation, lot size, MIN_RR, kills losing agents, and auto-disables losing setup types. Bounded clamps + per-run step caps prevent runaway tuning.
- 🔁 **Idempotent Snapshot Cache**: One MT5 snapshot per cycle is reused across `run_cycle → _build_user_prompt → _get_claude_decision`. HTF trend lookups (H4/D1) cached for 30 min.
- 🏦 **Per-Broker Profiles**: `start.bat` selects `.env.oanda` (live), `test.bat` selects `.env.icmarkets` (demo). Each profile gets its own DB, MAGIC number, and tuner state — stats never bleed across accounts.
- 💾 **SQLite WAL**: Single-file durable store. No external DB to operate. 90-day shadow-log rotation runs on init.

---

## 🚀 Quick Start

### 1. Prerequisites

- Windows (MetaTrader 5 Python bridge is Windows-only)
- Python 3.11+
- A MetaTrader 5 install with a logged-in broker account (demo or live)
- An Anthropic API key

### 2. Clone and install

```bat
git clone https://github.com/YOUR_USER/Aurum.git
cd Aurum

python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure your broker profile

```bat
copy .env.example .env.icmarkets
notepad .env.icmarkets
```

Fill in:
- `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`, `MT5_PATH`
- `ANTHROPIC_API_KEY`
- API keys for TwelveData / FMP / AlphaVantage / GNews (free tiers fine)
- A unique `MAGIC_NUMBER` for this profile

For a live OANDA account, `copy .env.example .env.oanda` and clamp the risk gates harder — see comments inline.

### 4. Run

```bat
test.bat        :: paper / demo  -> .env.icmarkets
start.bat       :: REAL MONEY    -> .env.oanda
```

Aurum boots a FastAPI server on `http://127.0.0.1:8000`, schedules the AI agent + self-tuner, and starts cycling.

Verify:

```bat
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/api/ai-trader/status
```

---

## ⚙️ Configuration

All configuration is environment-variable driven. Aurum picks the env file via `AI_TRADER_ENV`:

| `AI_TRADER_ENV` | File loaded                | Set by      |
|-----------------|----------------------------|-------------|
| `icmarkets`     | `.env.icmarkets`           | `test.bat`  |
| `oanda`         | `.env.oanda`               | `start.bat` |
| _(unset)_       | `.env`                     | manual      |

See [`.env.example`](.env.example) for the full annotated knob list. Highlights:

```ini
# Stop-loss
SL_ATR_MULT=1.5            # SL = max(SL_FLOOR_POINTS, atr*1.5)
SL_FLOOR_POINTS=3.0
MAX_SL_POINTS=7.0

# Risk
RISK_PCT_PER_TRADE=0.01    # 1% account risk per trade
DAILY_LOSS_LIMIT_USD=50
WEEKLY_LOSS_LIMIT_USD=200
MIN_RR=2.0                 # reject any setup paying < 1:2

# Gating
MIN_CONFIDENCE_PCT=60      # Claude < 60% conf -> HOLD
BREAKOUT_GATE_ENABLED=true # skip LLM call when no setup
VOLUME_RATIO_MIN=1.2

# Cycle cadence
AI_TRADER_INTERVAL_SECONDS=1800
```

---

## 🧠 Technical Core

Aurum is built around a clear separation between **decision**, **discipline**, and **execution**:

- **Decision (Claude)**: `ai_agent.py` builds a structured market snapshot, sends it to Claude with a system prompt encoding the breakout-follow-trend strategy, and parses a strict JSON response (`action`, `confidence`, `stop_loss`, `take_profit`, `reasoning`, `next_check_minutes`).
- **Discipline (Gates)**: `risk_manager.py`, `news_filter.py`, and the breakout pre-gate inside `ai_agent.py` form a chain of vetoes. The first failing gate logs to `shadow_decisions` and exits — Claude is never even called when a deterministic check already says "no setup".
- **Execution (MT5)**: `mt5_trader.py` wraps `MetaTrader5` IPC. All position queries are filtered by `MAGIC_NUMBER` so two Aurum profiles on the same terminal never see each other's tickets. Order placement, snapshot fetch, and HTF trend lookups all use 30 s and 30 min TTL caches respectively to avoid hammering the broker between cycles.
- **Calibration (Self-Tuner)**: `self_tuner.py` runs every 15 minutes, reads the last `SELF_TUNER_WINDOW` closed trades + `SELF_TUNER_SETUP_LOOKBACK_DAYS` of shadow rows, and applies six rules. Each rule is bounded (`PARAM_BOUNDS`) and step-capped (`PARAM_STEP_CAP`) so a noisy week can't blow up the config.
- **Persistence (SQLite WAL)**: `database.py`. Five tables: `agents`, `signals`, `positions`, `profit_history`, `shadow_decisions`. Indices on hot lookup paths. 90-day shadow rotation on init. Connection pooling via 30 s busy_timeout.
- **HTTP surface (FastAPI)**: `routes.py`. Endpoints for agent login, signal post, signal close, recent feed, AI-trader status, MT5 price, MT5 positions. `/health` for liveness probes.

---

## 📂 Project Layout

```
.
├── backend/
│   ├── ai_agent.py          — Claude decision loop + breakout pre-gate + shadow logging
│   ├── mt5_trader.py        — MT5 IPC wrapper, snapshot/HTF caches, order placement
│   ├── risk_manager.py      — preflight gates, compute_lot, daily/weekly loss caps
│   ├── news_filter.py       — ForexFactory calendar, fail-closed during NY window
│   ├── self_tuner.py        — six-rule parameter calibrator
│   ├── routes.py            — FastAPI endpoints
│   ├── database.py          — SQLite schema + shadow log + 90-day rotation
│   ├── config.py            — env file selection + typed exports
│   ├── main.py              — FastAPI entry point + startup hooks
│   └── backtest_gate.py     — replay breakout gate against MT5 history (no LLM)
├── data/                    — SQLite DBs (gitignored)
├── logs/                    — rolling server logs (gitignored)
├── .env.example             — annotated env template
├── .gitignore
├── requirements.txt
├── start.bat                — live profile launcher (.env.oanda)
├── test.bat                 — demo profile launcher (.env.icmarkets)
└── README.md                — this file
```

---

## 🔬 Backtesting the Gate

Before changing `VOLUME_RATIO_MIN` or any breakout threshold, replay it against recent M30 history:

```bat
venv\Scripts\python backend\backtest_gate.py --bars 500 --vol-min 1.2
```

Output reports how often `PRIMARY` and `SECONDARY` setups would have triggered the LLM call over the last ~10 days. Pure indicator math — no API spend.

---

## 🛡️ Safety Notes

- **Live trading risks real money.** Aurum will execute real orders on whatever account `MT5_LOGIN` points at. The `start.bat` launcher tags the boot banner `*** LIVE MONEY ***` when `AI_TRADER_ENV=oanda`. Read it.
- **MAGIC isolation is mandatory.** Two profiles sharing the same `MAGIC_NUMBER` will close each other's tickets. Always pick a unique int per profile.
- **Self-tuner is not a strategy.** It calibrates an existing edge — it does not invent one. If the underlying breakout-follow-trend thesis stops working in your market, the tuner will faithfully calibrate a losing system into a less-bad losing system.
- **Daily / weekly loss limits are *soft*.** They block new entries but do not force-close open positions. The trail-stop loop is what protects an open trade.
- **News feed dependency.** ForexFactory is a free public feed and occasionally goes down. Aurum fails *closed* during the 12-16 UTC US macro window when the feed is stale > 6 h. Outside that window, a stale feed silently disables the filter — be aware.

---

## 🤝 Compatibility

- ✅ Windows 10 / 11 (MT5 Python bridge requirement)
- ✅ Python 3.11, 3.12, 3.13
- ✅ MetaTrader 5 ≥ build 3815
- ✅ Any MT5-supported broker (tested: IC Markets Raw Spread Demo, OANDA Global Live)
- ✅ Anthropic Claude Sonnet 4.x (`claude-sonnet-4-*`)

---

## 📝 License

This project is licensed under the GPL-3.0 License — see the [LICENSE](LICENSE) file for details.

No warranty. No liability. Trading is your responsibility.

---

<p align="center">
  <i>Aurum — gold trades; the gates decide.</i>
</p>
