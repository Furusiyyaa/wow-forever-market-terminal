# WoW Forever — Market Terminal

A lightweight, TradingView-style desktop Auction House tracker and open-world gold farm ranker for **World of Warcraft: Forever**.

- **Real TradingView charts** (`lightweight-charts`) — Area, Line, and 4h/Daily OHLC Candlesticks + AH Supply Volume histogram.
- **Official Blizzard Game Data API** (`dynamic-forever-eu` / `dynamic-forever-us`) — hourly dumps with `If-Modified-Since` / `304` caching (zero scraping, zero ToS risk).
- **Local SQLite WAL database** — builds your own price & volume history automatically after every hourly scan.
- **Forever `1.60.1` Static Catalog + Open-World Farm Ranker** — searches all ~19,400 Forever items and ranks open-world mobs by live expected gold per kill.
- **Zero background bloat** — closing the app cleanly stops the local Python API server.

---

## Quick Start (Windows)

### Prerequisites
1. **Python 3.10+** — https://www.python.org/downloads/ *(check "Add python.exe to PATH" during install)*
2. **Node.js 18+ (LTS)** — https://nodejs.org/
3. **Free Battle.net API Client** — https://develop.battle.net/access/clients
   - Click **+ Create Client** (Redirect URL: `http://localhost`).
   - Copy your **Client ID** and **Client Secret**.

### 1. Run One-Click Setup
Open PowerShell in this folder and run:
```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```
This creates `.venv`, installs Python & Electron dependencies, initializes the local SQLite database, downloads the latest WoW Forever DB2 item catalog (`wago.tools`) + ClassicDB mob loot tables, and creates `telemetry\.env`.

### 2. Add Your Battle.net API Credentials
Open `telemetry\.env` and paste your credentials:
```ini
BLIZZARD_CLIENT_ID=your_client_id_here
BLIZZARD_CLIENT_SECRET=your_client_secret_here
BLIZZARD_REGION=eu

# Example: EU PvP Horde (House 113)
FOREVER_RULESET_ID=1
FOREVER_HOUSE_ID=113
```

Not sure of your server's `FOREVER_RULESET_ID` / `FOREVER_HOUSE_ID`? Run:
```powershell
.\.venv\Scripts\python.exe telemetry\ah_poller.py discover --region eu
```
*(Use `--region us` for US servers.)*

### 3. Launch the Market Terminal
Double-click **`telemetry\Launch-Market-Terminal.vbs`** *(or right-click → Send to Desktop to create a shortcut)*.

Click **Start Feed** in the top-right to enable automatic hourly price history recording!

---

## Manual CLI Commands
```powershell
# Poll live AH snapshot once
.\.venv\Scripts\python.exe telemetry\ah_poller.py poll

# Refresh Forever DB2 catalog after a game patch
.\.venv\Scripts\python.exe telemetry\db_ingest.py sync-db2

# Print top open-world gold farms in terminal
.\.venv\Scripts\python.exe telemetry\farm_ranker.py
```
