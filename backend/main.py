"""
AI-Trader — FastAPI entry point.

Starts:
  - SQLite schema (init_database)
  - Claude-Trader autonomous agent on its scheduled interval
  - Self-tuner loop (rule-based parameter calibration)

Shuts down:
  - MT5 IPC connection
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

# ---- Logging ----------------------------------------------------------------

_LOG_DIR = os.path.join(os.path.dirname(__file__), "..", "logs")
os.makedirs(_LOG_DIR, exist_ok=True)

# Force UTF-8 on stdout/stderr so log messages containing non-ASCII chars
# (e.g. '→', '≥') don't crash the StreamHandler on Windows cp1252 consoles.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_file_handler = RotatingFileHandler(
    os.path.join(_LOG_DIR, "server.log"),
    maxBytes=10 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
)
_stream_handler = logging.StreamHandler()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[_file_handler, _stream_handler],
)

logger = logging.getLogger(__name__)

# ---- App --------------------------------------------------------------------

from database import init_database
from routes import create_app

init_database()
app = create_app()


@app.on_event("startup")
async def _startup() -> None:
    try:
        from config import LOADED_ENV_NAME, LOADED_ENV_FILE, MT5_SERVER, MT5_LOGIN, MAGIC_NUMBER, DB_PATH
        banner = "=" * 70
        is_live = LOADED_ENV_NAME.lower() in {"oanda", "live", "prod", "production"}
        tag = "*** LIVE MONEY ***" if is_live else "DEMO / TESTING"
        logger.info(banner)
        logger.info("AI-Trader starting up  [broker profile: %s]  %s", LOADED_ENV_NAME, tag)
        logger.info("  env file      : %s", LOADED_ENV_FILE)
        logger.info("  MT5 server    : %s  (login %s)", MT5_SERVER, MT5_LOGIN)
        logger.info("  DB path       : %s", DB_PATH)
        logger.info("  magic number  : %s", MAGIC_NUMBER)
        logger.info(banner)
    except Exception as exc:
        logger.warning("startup banner failed: %s", exc)

    # Autonomous AI trading agent (Claude-Trader)
    try:
        from ai_agent import start_ai_traders
        asyncio.create_task(start_ai_traders())
        logger.info("AI Traders scheduled")
    except Exception as exc:
        logger.exception("Failed to schedule AI traders: %s", exc)

    # Self-tuner (runtime parameter calibration from DB stats)
    try:
        from self_tuner import run_self_tuner_loop
        asyncio.create_task(run_self_tuner_loop())
        logger.info("Self-Tuner scheduled")
    except Exception as exc:
        logger.warning("Self-Tuner failed to schedule: %s", exc)


@app.on_event("shutdown")
async def _shutdown() -> None:
    try:
        from mt5_trader import close_mt5
        close_mt5()
        logger.info("MT5 connection closed")
    except Exception as exc:
        logger.warning("MT5 shutdown warning: %s", exc)


# ---- Run --------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
