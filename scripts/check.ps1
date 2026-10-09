# Runs every quality gate: lint, type-check, tests (integration tests need `docker compose up -d postgres redis`).
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
function Step($name, [scriptblock]$cmd) {
    Write-Host "==> $name" -ForegroundColor Cyan
    & $cmd
    if ($LASTEXITCODE -ne 0) { throw "$name failed" }
}
Push-Location "$root\agent"
Step 'agent: ruff'   { .\.venv\Scripts\ruff.exe check app tests }
Step 'agent: mypy'   { .\.venv\Scripts\mypy.exe app }
Step 'agent: pytest' { .\.venv\Scripts\python.exe -m pytest -q }
Pop-Location
Push-Location "$root\backend"
Step 'backend: ruff'   { .\.venv\Scripts\ruff.exe check app tests }
Step 'backend: mypy'   { .\.venv\Scripts\mypy.exe app }
Step 'backend: pytest' { .\.venv\Scripts\python.exe -m pytest -q }
Pop-Location
Push-Location "$root\frontend"
Step 'frontend: lint'  { npx oxlint src }
Step 'frontend: test'  { npx vitest run }
Step 'frontend: build' { npm run build }
Pop-Location
Write-Host 'All checks passed.' -ForegroundColor Green
