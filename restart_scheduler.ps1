$ErrorActionPreference = "Stop"

$projectRoot = $PSScriptRoot
$stopScript = Join-Path $projectRoot "stop_scheduler.ps1"
$startScript = Join-Path $projectRoot "start_scheduler.ps1"

if (-not (Test-Path $stopScript)) {
    Write-Error "Missing stop script: $stopScript"
}

if (-not (Test-Path $startScript)) {
    Write-Error "Missing start script: $startScript"
}

Write-Output "Stopping scheduler (if running)..."
& $stopScript

Start-Sleep -Seconds 1

Write-Output "Starting scheduler..."
& $startScript
