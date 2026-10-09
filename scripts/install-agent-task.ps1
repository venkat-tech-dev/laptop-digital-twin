# Registers the telemetry agent as a scheduled task that starts at logon (current user) and restarts on failure.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = "$root\agent\.venv\Scripts\pythonw.exe"
if (-not (Test-Path $python)) { throw "Agent venv not found. Create it first (see README)." }
$action = New-ScheduledTaskAction -Execute $python -Argument '-m app.main' -WorkingDirectory "$root\agent"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'LaptopDigitalTwinAgent' -Action $action -Trigger $trigger -Settings $settings `
    -Description 'Laptop Digital Twin host telemetry agent' -Force | Out-Null
Start-ScheduledTask -TaskName 'LaptopDigitalTwinAgent'
Write-Host 'Registered and started scheduled task LaptopDigitalTwinAgent.'
