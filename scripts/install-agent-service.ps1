<#
.SYNOPSIS
  Install / update / remove the Laptop Digital Twin endpoint agent as a Windows service.

.DESCRIPTION
  Run from an elevated PowerShell. The service:
    * starts automatically at boot (delayed auto-start), independent of any user logon or browser,
    * runs as LocalSystem (needed for boot-performance events and storage reliability counters),
    * restarts automatically after a crash (5 s, 30 s, 60 s; failure counter resets daily),
    * keeps its queue, credentials, logs and health.json in %ProgramData%\LaptopDigitalTwin\agent.
  Configuration is read from the repository .env (AGENT_BACKEND_URL, AGENT_INGEST_KEY, ...).
  The previous logon scheduled task (install-agent-task.ps1) is removed so only one agent runs.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install-agent-service.ps1
  powershell -ExecutionPolicy Bypass -File scripts\install-agent-service.ps1 -Uninstall
#>
[CmdletBinding()]
param([switch]$Uninstall)

$ErrorActionPreference = 'Stop'
$ServiceName = 'LaptopDigitalTwinAgent'
$root = Split-Path -Parent $PSScriptRoot
$agent = Join-Path $root 'agent'
$entry = Join-Path $agent 'service_entry.py'
$venvPython = Join-Path $agent '.venv\Scripts\python.exe'

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this script from an elevated (Run as administrator) PowerShell.'
}
if (-not (Test-Path $venvPython)) { throw "Agent virtual environment not found: $venvPython (see README)." }

# The SCM must start the base interpreter (a venv launcher spawns a child process the SCM cannot track).
$basePython = & $venvPython -c "import sys; print(getattr(sys, '_base_executable', sys.executable))"

if ($Uninstall) {
    if (Get-Service $ServiceName -ErrorAction SilentlyContinue) {
        & $basePython $entry stop 2>$null | Out-Null
        & $basePython $entry remove
    }
    Write-Host 'Service removed. Local data remains in %ProgramData%\LaptopDigitalTwin\agent.'
    return
}

# Remove the legacy per-user logon task so two agents never run at once.
Unregister-ScheduledTask -TaskName 'LaptopDigitalTwinAgent' -Confirm:$false -ErrorAction SilentlyContinue

if (Get-Service $ServiceName -ErrorAction SilentlyContinue) {
    & $basePython $entry stop 2>$null | Out-Null
    & $basePython $entry --startup delayed update
} else {
    & $basePython $entry --startup delayed install
}
if ($LASTEXITCODE -ne 0) { throw 'Service registration failed.' }

# Recovery: restart after 5 s, 30 s, 60 s; reset the failure counter after 24 h. Also on non-crash failures.
sc.exe failure $ServiceName reset= 86400 actions= restart/5000/restart/30000/restart/60000 | Out-Null
sc.exe failureflag $ServiceName 1 | Out-Null

Start-Service $ServiceName
Start-Sleep -Seconds 5
Get-Service $ServiceName | Format-Table Name, Status, StartType -AutoSize
$health = Join-Path $env:ProgramData 'LaptopDigitalTwin\agent\health.json'
if (Test-Path $health) { Write-Host "Agent health: $health" }
Write-Host 'Logs: %ProgramData%\LaptopDigitalTwin\agent\logs\agent.log'
