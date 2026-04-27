# ===================================================================
# Aurum VPS Setup Script — run on Windows Server 2022 VPS
#
# Usage:
#   1. RDP into VPS as Administrator
#   2. Change Administrator password (Ctrl+Alt+End -> Change Password)
#   3. Open PowerShell AS ADMINISTRATOR
#   4. Paste this entire script
#   5. After it finishes, edit C:\Aurum\.env.icmarkets with real creds
#   6. Run:  cd C:\Aurum ; .\test.bat
# ===================================================================

$ErrorActionPreference = "Stop"

Write-Host "=== Aurum VPS Setup ===" -ForegroundColor Cyan

# --- 1. Allow script execution -----------------------------------
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force

# --- 2. Install Python 3.11 + Git via winget ---------------------
Write-Host "`n[1/6] Installing Python 3.11 + Git..." -ForegroundColor Yellow
winget install --id Python.Python.3.11 -e --silent --accept-source-agreements --accept-package-agreements
winget install --id Git.Git              -e --silent --accept-source-agreements --accept-package-agreements

# Refresh PATH for current session
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")

# --- 3. Clone Aurum ----------------------------------------------
Write-Host "`n[2/6] Cloning Aurum..." -ForegroundColor Yellow
if (Test-Path "C:\Aurum") {
    Write-Host "C:\Aurum already exists — pulling latest"
    Set-Location C:\Aurum
    git pull
} else {
    Set-Location C:\
    git clone https://github.com/DraSoGo/Aurum.git
    Set-Location C:\Aurum
}

# --- 4. Create venv + install deps -------------------------------
Write-Host "`n[3/6] Building Python venv + installing deps..." -ForegroundColor Yellow
python -m venv venv
.\venv\Scripts\python.exe -m pip install --upgrade pip
.\venv\Scripts\python.exe -m pip install -r requirements.txt

# --- 5. Bootstrap .env.icmarkets from template ------------------
Write-Host "`n[4/6] Creating .env.icmarkets from template..." -ForegroundColor Yellow
if (-Not (Test-Path ".env.icmarkets")) {
    Copy-Item .env.example .env.icmarkets
    Write-Host "Created .env.icmarkets — EDIT THIS FILE before running test.bat" -ForegroundColor Magenta
} else {
    Write-Host ".env.icmarkets already exists — leaving alone"
}

# --- 6. Create logs + data dirs ----------------------------------
Write-Host "`n[5/6] Creating runtime dirs..." -ForegroundColor Yellow
New-Item -ItemType Directory -Force -Path C:\Aurum\logs | Out-Null
New-Item -ItemType Directory -Force -Path C:\Aurum\data | Out-Null

# --- 7. Auto-start task on boot ----------------------------------
Write-Host "`n[6/6] Registering auto-start task..." -ForegroundColor Yellow
$taskName = "Aurum-AutoStart"
$action   = New-ScheduledTaskAction -Execute "C:\Aurum\test.bat" -WorkingDirectory "C:\Aurum"
$trigger  = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) `
    -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -RunLevel Highest

if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Description "Auto-start Aurum on boot"

# --- 8. Open Windows Firewall for FastAPI port 8000 (localhost only) ----
# We DO NOT expose port 8000 externally. Bot is RDP-only access.
# If you want remote dashboard access, manually open the rule with
# scope = your home IP only.

Write-Host "`n=== SETUP COMPLETE ===" -ForegroundColor Green
Write-Host ""
Write-Host "NEXT STEPS:" -ForegroundColor Cyan
Write-Host "  1. Login MetaTrader 5 to your broker (IC Markets demo)" -ForegroundColor White
Write-Host "  2. Open M30 XAUUSD chart in MT5" -ForegroundColor White
Write-Host "  3. Edit C:\Aurum\.env.icmarkets with:" -ForegroundColor White
Write-Host "       - MT5_LOGIN, MT5_PASSWORD, MT5_SERVER" -ForegroundColor White
Write-Host "       - MT5_PATH (verify actual MT5 install path)" -ForegroundColor White
Write-Host "       - ANTHROPIC_API_KEY" -ForegroundColor White
Write-Host "       - TWELVE_DATA_API_KEY, FMP_API_KEY, etc." -ForegroundColor White
Write-Host "       - Unique MAGIC_NUMBER" -ForegroundColor White
Write-Host "  4. Run:  cd C:\Aurum ;  .\test.bat" -ForegroundColor White
Write-Host "  5. Verify health:  curl http://127.0.0.1:8000/health" -ForegroundColor White
Write-Host ""
Write-Host "Auto-start on boot is registered as Scheduled Task 'Aurum-AutoStart'." -ForegroundColor Cyan
Write-Host "Disable it with:  Disable-ScheduledTask -TaskName Aurum-AutoStart" -ForegroundColor Cyan
