<#
.SYNOPSIS
  Install LibreHardwareMonitor (LHM) and start it at logon with its local web server enabled, so the
  telemetry agent can read CPU package temperature / power / PL1 / PL2 / core voltage, GPU
  temperature / clock / power and fan RPM.

.DESCRIPTION
  LHM reads embedded-controller and MSR sensors through a kernel driver, so it must run as
  administrator. This script (run it from an elevated PowerShell):
    1. downloads the latest LHM release from GitHub (LibreHardwareMonitor/LibreHardwareMonitor),
    2. extracts it to %ProgramFiles%\LibreHardwareMonitor,
    3. writes LHM settings: remote web server on 127.0.0.1:8085, start minimised to tray,
    4. registers a scheduled task "LDT LibreHardwareMonitor" that starts LHM at logon with highest
       privileges.
  The agent picks the sensors up automatically (HARDWARE_SENSOR_PROVIDER=auto, LHM_URL default).

  LHM is third-party open-source software (MPL-2.0). Review it before installing.

.EXAMPLE
  # elevated PowerShell
  powershell -ExecutionPolicy Bypass -File scripts\install-lhm.ps1
  powershell -ExecutionPolicy Bypass -File scripts\install-lhm.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:ProgramFiles 'LibreHardwareMonitor'),
    [int]$Port = 8085,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$TaskName = 'LDT LibreHardwareMonitor'

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this script from an elevated (Run as administrator) PowerShell: LHM needs administrator rights.'
}

if ($Uninstall) {
    Get-Process LibreHardwareMonitor -ErrorAction SilentlyContinue | Stop-Process -Force
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    if (Test-Path $InstallDir) { Remove-Item -Recurse -Force $InstallDir }
    Write-Host 'LibreHardwareMonitor removed.'
    return
}

Write-Host 'Looking up the latest LibreHardwareMonitor release...'
$release = Invoke-RestMethod -Uri 'https://api.github.com/repos/LibreHardwareMonitor/LibreHardwareMonitor/releases/latest' `
    -Headers @{ 'User-Agent' = 'ldt-install-lhm' }
$asset = $release.assets | Where-Object { $_.name -match '^LibreHardwareMonitor.*\.zip$' } | Select-Object -First 1
if (-not $asset) { throw "No LibreHardwareMonitor zip asset in release $($release.tag_name)" }

$zip = Join-Path $env:TEMP $asset.name
Write-Host "Downloading $($asset.name) ($($release.tag_name))..."
Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zip -UseBasicParsing

Get-Process LibreHardwareMonitor -ErrorAction SilentlyContinue | Stop-Process -Force
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Expand-Archive -Path $zip -DestinationPath $InstallDir -Force
Remove-Item $zip -Force
$exe = Get-ChildItem -Path $InstallDir -Recurse -Filter 'LibreHardwareMonitor.exe' | Select-Object -First 1
if (-not $exe) { throw 'LibreHardwareMonitor.exe not found after extraction' }

# LHM keeps its options in LibreHardwareMonitor.config next to the executable.
$config = Join-Path $exe.DirectoryName 'LibreHardwareMonitor.config'
@"
<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <appSettings>
    <add key="startMinMenuItem" value="true" />
    <add key="minTrayMenuItem" value="true" />
    <add key="minCloseMenuItem" value="true" />
    <add key="runWebServerMenuItem" value="true" />
    <add key="listenerIp" value="127.0.0.1" />
    <add key="listenerPort" value="$Port" />
  </appSettings>
</configuration>
"@ | Set-Content -Path $config -Encoding UTF8

$action = New-ScheduledTaskAction -Execute $exe.FullName -WorkingDirectory $exe.DirectoryName
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
$taskPrincipal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -RunLevel Highest -LogonType Interactive
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $taskPrincipal -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 4
try {
    $data = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/data.json" -TimeoutSec 5
    Write-Host "LibreHardwareMonitor is serving sensors on http://127.0.0.1:$Port/data.json"
} catch {
    Write-Warning "LHM started but the web server did not answer yet. Open LHM -> Options -> Remote Web Server -> Run (port $Port)."
}
Write-Host 'Done. The telemetry agent picks up LHM sensors within ~30 seconds.'
