# ===================================================================
# Aurum VPS Setup Script — Windows Server 2022
#
# winget is NOT included on Server 2022 — uses direct MSI/EXE installers.
#
# Usage:
#   1. RDP into VPS as Administrator
#   2. Open PowerShell AS ADMINISTRATOR
#   3. Run:
#        iex (irm https://raw.githubusercontent.com/DraSoGo/Aurum/main/vps_setup.ps1)
#   4. After it finishes, edit C:\Aurum\.env.icmarkets with real creds
#   5. Run:  cd C:\Aurum ; .\test.bat
# ===================================================================

$ErrorActionPreference = "Stop"

Write-Host "=== Aurum VPS Setup ===" -ForegroundColor Cyan

Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# --- 1. Install Python 3.11 -------------------------------------
Write-Host "`n[1/7] Installing Python 3.11..." -ForegroundColor Yellow
$pythonUrl = "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe"
$pythonExe = "$env:TEMP\python-installer.exe"
if (-Not (Get-Command python -ErrorAction SilentlyContinue)) {
    Invoke-WebRequest -Uri $pythonUrl -OutFile $pythonExe -UseBasicParsing
    Start-Process -FilePath $pythonExe -ArgumentList `
        "/quiet","InstallAllUsers=1","PrependPath=1","Include_test=0","Include_launcher=1" `
        -Wait
    Remove-Item $pythonExe -Force
} else {
    Write-Host "Python already installed: $(python --version)"
}

# --- 2. Install Git -----------------------------------------------
Write-Host "`n[2/7] Installing Git..." -ForegroundColor Yellow
$gitUrl = "https://github.com/git-for-windows/git/releases/download/v2.45.2.windows.1/Git-2.45.2-64-bit.exe"
$gitExe = "$env:TEMP\git-installer.exe"
if (-Not (Get-Command git -ErrorAction SilentlyContinue)) {
    Invoke-WebRequest -Uri $gitUrl -OutFile $gitExe -UseBasicParsing
    Start-Process -FilePath $gitExe -ArgumentList "/VERYSILENT","/NORESTART","/NOCANCEL","/SP-" -Wait
    Remove-Item $gitExe -Force
} else {
    Write-Host "Git already installed: $(git --version)"
}

# Refresh PATH
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")

# --- 3. Verify tools ---------------------------------------------
Write-Host "`n[3/7] Verifying installs..." -ForegroundColor Yellow
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
$gitCmd = (Get-Command git    -ErrorAction SilentlyContinue).Source
if (-Not $python) { throw "Python install failed — re-run script or install manually" }
if (-Not $gitCmd) { throw "Git install failed — re-run script or install manually" }
Write-Host "  python: $python"
Write-Host "  git:    $gitCmd"

# --- 4. Clone Aurum ----------------------------------------------
Write-Host "`n[4/7] Cloning Aurum to C:\Aurum..." -ForegroundColor Yellow
if (Test-Path "C:\Aurum") {
    Write-Host "C:\Aurum exists — pulling latest"
    Set-Location C:\Aurum
    git pull
} else {
    Set-Location C:\
    git clone https://github.com/DraSoGo/Aurum.git
    Set-Location C:\Aurum
}

# --- 5. venv + deps ----------------------------------------------
Write-Host "`n[5/7] Building venv + installing requirements..." -ForegroundColor Yellow
python -m venv venv
.\venv\Scripts\python.exe -m pip install --upgrade pip
.\venv\Scripts\python.exe -m pip install -r requirements.txt

# --- 6. Bootstrap .env.icmarkets ---------------------------------
Write-Host "`n[6/7] Creating .env.icmarkets template..." -ForegroundColor Yellow
if (-Not (Test-Path ".env.icmarkets")) {
    Copy-Item .env.example .env.icmarkets
    Write-Host "Created .env.icmarkets — EDIT THIS FILE before running test.bat" -ForegroundColor Magenta
} else {
    Write-Host ".env.icmarkets already exists — leaving alone"
}
New-Item -ItemType Directory -Force -Path C:\Aurum\logs | Out-Null
New-Item -ItemType Directory -Force -Path C:\Aurum\data | Out-Null

# --- 7. Auto-start task on boot ---------------------------------
Write-Host "`n[7/7] Registering Aurum-AutoStart scheduled task..." -ForegroundColor Yellow
$taskName  = "Aurum-AutoStart"
$action    = New-ScheduledTaskAction -Execute "C:\Aurum\test.bat" -WorkingDirectory "C:\Aurum"
$trigger   = New-ScheduledTaskTrigger -AtStartup
$settings  = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) `
    -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -RunLevel Highest

if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Description "Auto-start Aurum on boot"

Write-Host "`n=== SETUP COMPLETE ===" -ForegroundColor Green
Write-Host ""
Write-Host "NEXT STEPS:" -ForegroundColor Cyan
Write-Host "  1. Open MetaTrader 5 (preinstalled)" -ForegroundColor White
Write-Host "  2. File -> Login to Trade Account -> enter broker creds" -ForegroundColor White
Write-Host "  3. Open M30 XAUUSD chart" -ForegroundColor White
Write-Host "  4. Right-click MT5 icon -> Properties -> copy Target path" -ForegroundColor White
Write-Host "  5. notepad C:\Aurum\.env.icmarkets    -> fill creds:" -ForegroundColor White
Write-Host "       MT5_LOGIN, MT5_PASSWORD, MT5_SERVER, MT5_PATH" -ForegroundColor White
Write-Host "       ANTHROPIC_API_KEY" -ForegroundColor White
Write-Host "       TWELVE_DATA_API_KEY, FMP_API_KEY, etc." -ForegroundColor White
Write-Host "       MAGIC_NUMBER (unique int)" -ForegroundColor White
Write-Host "  6. cd C:\Aurum ; .\test.bat" -ForegroundColor White
Write-Host "  7. curl http://127.0.0.1:8000/health" -ForegroundColor White
Write-Host ""
Write-Host "Auto-start registered: Scheduled Task 'Aurum-AutoStart'" -ForegroundColor Cyan
