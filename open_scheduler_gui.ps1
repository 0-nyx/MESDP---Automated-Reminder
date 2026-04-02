$ErrorActionPreference = "Stop"

$projectRoot = $PSScriptRoot
$pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"
$guiFile = Join-Path $projectRoot "scheduler_gui.py"

if (-not (Test-Path $pythonExe)) {
    Write-Error "Python executable not found at $pythonExe"
}

if (-not (Test-Path $guiFile)) {
    Write-Error "GUI file not found at $guiFile"
}

& $pythonExe $guiFile
