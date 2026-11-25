# One-click Windows setup for WoW Forever Market Terminal
$ErrorActionPreference = "Stop"

Write-Host "=== WoW Forever Market Terminal Setup ===" -ForegroundColor Cyan

# 1. Preflight checks
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Python 3.10+ is required. Install from https://www.python.org/downloads/ (check 'Add to PATH')."
}
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    throw "Node.js 18+ (npm) is required. Install from https://nodejs.org/."
}

# 2. Create Python venv & install deps
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "[1/4] Creating Python virtual environment (.venv)..." -ForegroundColor Yellow
    python -m venv .venv
}
Write-Host "[2/4] Installing Python dependencies..." -ForegroundColor Yellow
.\.venv\Scripts\python.exe -m pip install --upgrade pip --quiet
.\.venv\Scripts\python.exe -m pip install -r requirements.txt --quiet

# 3. Install Electron & TradingView lightweight-charts
Write-Host "[3/4] Installing Electron & lightweight-charts..." -ForegroundColor Yellow
Push-Location "telemetry\desktop"
npm install
Pop-Location

# 4. Initialize SQLite DB, sync Forever DB2 + ClassicDB, and create .env template
Write-Host "[4/4] Initializing local DB & syncing WoW Forever catalog..." -ForegroundColor Yellow
.\.venv\Scripts\python.exe telemetry\ah_poller.py init
.\.venv\Scripts\python.exe telemetry\db_ingest.py sync-all

if (-not (Test-Path "telemetry\.env")) {
    Copy-Item "telemetry\.env.example" "telemetry\.env"
    Write-Host "`nCreated telemetry\.env — open it and paste your Battle.net Client ID & Secret!" -ForegroundColor Green
}

Write-Host "`nSetup complete!" -ForegroundColor Green
Write-Host "Next steps:"
Write-Host "  1. Edit telemetry\.env with your Battle.net API credentials (https://develop.battle.net/access/clients)"
Write-Host "  2. Double-click telemetry\Launch-Market-Terminal.vbs"
