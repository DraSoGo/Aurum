@echo off
setlocal EnableExtensions
cd /d "%~dp0"

REM ============================================================
REM  AI-Trader  -  IC MARKETS DEMO  (testing profile)
REM  Loads .env.icmarkets  ->  DB: data/clawtrader_icmarkets.db
REM ============================================================

set "AI_TRADER_ENV=icmarkets"
title AI-Trader [DEMO / IC Markets]

echo.
echo ============================================================
echo   AI-Trader  -  IC MARKETS DEMO  (test profile)
echo   env file : .env.icmarkets
echo   DB       : data\clawtrader_icmarkets.db
echo   magic    : 202400
echo ============================================================
echo.

REM ---- Locate a real Python (prefer the py launcher) ----
set "PY_CMD="
where py >nul 2>nul && set "PY_CMD=py -3"
if not defined PY_CMD (
    where python >nul 2>nul && set "PY_CMD=python"
)
if not defined PY_CMD (
    echo [test] ERROR: no Python found. Install Python 3.13 and re-run.
    goto :hold
)
echo [test] Using: %PY_CMD%

REM ---- Create venv on first run ----
if not exist "venv\Scripts\python.exe" (
    echo [test] Creating virtual environment...
    %PY_CMD% -m venv venv
    if errorlevel 1 (
        echo [test] ERROR: failed to create venv.
        goto :hold
    )
)

call "venv\Scripts\activate.bat"
if errorlevel 1 (
    echo [test] ERROR: failed to activate venv.
    goto :hold
)

"venv\Scripts\python.exe" -c "import fastapi" 1>nul 2>nul
if errorlevel 1 (
    echo [test] Installing Python dependencies...
    "venv\Scripts\python.exe" -m pip install --upgrade pip
    "venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [test] ERROR: pip install failed.
        goto :hold
    )
)

echo [test] Launching AI-Trader (IC Markets DEMO) on http://127.0.0.1:8000
"venv\Scripts\python.exe" backend\main.py
set "EXITCODE=%ERRORLEVEL%"
echo.
echo [test] Server exited with code %EXITCODE%.

:hold
echo.
pause
endlocal
