@echo off
setlocal EnableExtensions
cd /d "%~dp0"

REM ============================================================
REM  AI-Trader  -  OANDA LIVE  (REAL MONEY profile)
REM  Loads .env.oanda  ->  DB: data/clawtrader_oanda.db
REM ============================================================

set "AI_TRADER_ENV=oanda"
title AI-Trader [*** LIVE / OANDA ***]

echo.
echo ############################################################
echo #                                                          #
echo #        * * *   L I V E   M O N E Y   * * *               #
echo #                                                          #
echo #   Profile : OANDA_Global-Live-1   (login 7041507)        #
echo #   Env     : .env.oanda                                   #
echo #   DB      : data\clawtrader_oanda.db                     #
echo #   Magic   : 202500                                       #
echo #                                                          #
echo #   This will place REAL orders on your OANDA live         #
echo #   account. Losses are real. Risk gates:                  #
echo #     DAILY_LOSS_LIMIT_USD = $5                            #
echo #     MAX_LOT_SIZE         = 0.01                          #
echo #     MAX_CONSEC_LOSSES    = 2                             #
echo #                                                          #
echo ############################################################
echo.
set /p "CONFIRM=Type  GO LIVE  exactly to continue (anything else aborts): "
if /I not "%CONFIRM%"=="GO LIVE" (
    echo [start] Aborted. No server launched.
    goto :hold
)
echo.

REM ---- Locate a real Python (prefer the py launcher) ----
set "PY_CMD="
where py >nul 2>nul && set "PY_CMD=py -3"
if not defined PY_CMD (
    where python >nul 2>nul && set "PY_CMD=python"
)
if not defined PY_CMD (
    echo [start] ERROR: no Python found. Install Python 3.13 and re-run.
    goto :hold
)
echo [start] Using: %PY_CMD%

REM ---- Create venv on first run ----
if not exist "venv\Scripts\python.exe" (
    echo [start] Creating virtual environment...
    %PY_CMD% -m venv venv
    if errorlevel 1 (
        echo [start] ERROR: failed to create venv.
        goto :hold
    )
)

call "venv\Scripts\activate.bat"
if errorlevel 1 (
    echo [start] ERROR: failed to activate venv.
    goto :hold
)

"venv\Scripts\python.exe" -c "import fastapi" 1>nul 2>nul
if errorlevel 1 (
    echo [start] Installing Python dependencies...
    "venv\Scripts\python.exe" -m pip install --upgrade pip
    "venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [start] ERROR: pip install failed.
        goto :hold
    )
)

echo [start] Launching AI-Trader (OANDA LIVE) on http://127.0.0.1:8000
"venv\Scripts\python.exe" backend\main.py
set "EXITCODE=%ERRORLEVEL%"
echo.
echo [start] Server exited with code %EXITCODE%.

:hold
echo.
pause
endlocal
