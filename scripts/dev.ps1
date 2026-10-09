# Starts the local development stack: Postgres/Redis (Docker), backend, host agent, frontend.
# Each long-running process opens in its own PowerShell window.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

if (-not (Test-Path "$root\.env")) { throw "Missing .env - copy .env.example to .env and set AGENT_INGEST_KEY / POSTGRES_PASSWORD." }

docker compose -f "$root\docker-compose.yml" up -d postgres redis

foreach ($pkg in 'backend', 'agent') {
    if (-not (Test-Path "$root\$pkg\.venv")) {
        py -3.12 -m venv "$root\$pkg\.venv"
        & "$root\$pkg\.venv\Scripts\python.exe" -m pip install -q -e "$root\$pkg[dev]"
    }
}
if (-not (Test-Path "$root\frontend\node_modules")) { Push-Location "$root\frontend"; npm install; Pop-Location }

Push-Location "$root\backend"; & .\.venv\Scripts\alembic.exe upgrade head; Pop-Location

Start-Process powershell -ArgumentList '-NoExit', '-Command', "Set-Location '$root\backend'; .\.venv\Scripts\python.exe -m app.main"
Start-Sleep -Seconds 3
Start-Process powershell -ArgumentList '-NoExit', '-Command', "Set-Location '$root\agent'; .\.venv\Scripts\python.exe -m app.main"
Start-Process powershell -ArgumentList '-NoExit', '-Command', "Set-Location '$root\frontend'; npm run dev"
Write-Host "Backend http://127.0.0.1:8000/docs  |  UI http://127.0.0.1:5173"
