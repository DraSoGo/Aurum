"""
Config — loads .env and exports only what the rewrite actually uses.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# .env lives at the project root, one level above backend/
# AI_TRADER_ENV selects a broker-specific env file:
#   - AI_TRADER_ENV=icmarkets -> .env.icmarkets  (demo / testing)
#   - AI_TRADER_ENV=oanda     -> .env.oanda      (LIVE / real money)
# Unset or unknown value falls back to the plain .env at the project root.
# Each broker env file sets its own MT5 creds, DB_PATH, MAGIC_NUMBER, and
# risk parameters — so stats, magic numbers, and lot sizing stay isolated.
_PROJECT_ROOT = Path(__file__).parent.parent
_BROKER_ENV = (os.getenv("AI_TRADER_ENV", "") or "").strip().lower()

_candidates = []
if _BROKER_ENV:
    _candidates.append(_PROJECT_ROOT / f".env.{_BROKER_ENV}")
    _candidates.append(Path(__file__).parent / f".env.{_BROKER_ENV}")
_candidates.append(_PROJECT_ROOT / ".env")
_candidates.append(Path(__file__).parent / ".env")

_ENV_PATH = next((p for p in _candidates if p.exists()), _candidates[-1])
load_dotenv(_ENV_PATH, override=True)

# Expose which env file was loaded so logs/startup can confirm it
LOADED_ENV_FILE = str(_ENV_PATH)
LOADED_ENV_NAME = _BROKER_ENV or "default"

# ---- MT5 ----
MT5_LOGIN    = int(os.getenv("MT5_LOGIN", "0") or 0)
MT5_PASSWORD = os.getenv("MT5_PASSWORD", "")
MT5_SERVER   = os.getenv("MT5_SERVER", "")
MT5_PATH     = os.getenv("MT5_PATH", r"C:\Program Files\MetaTrader 5\terminal64.exe")
MT5_SYMBOL   = os.getenv("MT5_SYMBOL", "XAUUSD")
LOT_SIZE     = float(os.getenv("LOT_SIZE", "0.01"))

# ---- AI ----
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()

# ---- Agent identities ----
CLAUDE_TRADER_NAME     = os.getenv("CLAUDE_TRADER_NAME", "Claude-Trader")
CLAUDE_TRADER_PASSWORD = os.getenv("CLAUDE_TRADER_PASSWORD",
                                   os.getenv("AI_TRADER_AGENT_PASSWORD", ""))
AI_TRADER_AGENT_PASSWORD = os.getenv("AI_TRADER_AGENT_PASSWORD", "")

AI_TRADER_BACKEND_URL      = os.getenv("AI_TRADER_BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
AI_TRADER_INTERVAL_SECONDS = int(os.getenv("AI_TRADER_INTERVAL_SECONDS", "1800"))

# ---- Self-tuner ----
SELF_TUNER_ENABLED           = os.getenv("SELF_TUNER_ENABLED", "false").strip().lower() in {"1","true","yes","on"}
SELF_TUNER_INTERVAL_MINUTES  = int(os.getenv("SELF_TUNER_INTERVAL_MINUTES", "15"))
SELF_TUNER_MIN_TRADES        = int(os.getenv("SELF_TUNER_MIN_TRADES", "10"))
SELF_TUNER_WINDOW            = int(os.getenv("SELF_TUNER_WINDOW", "50"))
SELF_TUNER_AGENT_KILL_WR     = float(os.getenv("SELF_TUNER_AGENT_KILL_WR", "0.25"))
SELF_TUNER_SINCE_ISO         = os.getenv("SELF_TUNER_SINCE_ISO", "").strip()

# ---- CORS ----
_cors_raw = os.getenv("CLAWTRADER_CORS_ORIGINS", "").strip()
CORS_ORIGINS = [o.strip() for o in _cors_raw.split(",") if o.strip()] or [
    "http://localhost:3000",
    "http://localhost:5173",
]

# ---- Paths ----
_PROJECT_ROOT  = Path(__file__).parent.parent
_db_path_env   = os.getenv("DB_PATH", "").strip()
if _db_path_env:
    DB_PATH = (
        Path(_db_path_env)
        if os.path.isabs(_db_path_env)
        else (_PROJECT_ROOT / _db_path_env)
    )
else:
    DB_PATH = _PROJECT_ROOT / "data" / "clawtrader.db"

MAGIC_NUMBER = int(os.getenv("MAGIC_NUMBER", "202400"))
