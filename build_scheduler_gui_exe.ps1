$ErrorActionPreference = "Stop"

$projectRoot = $PSScriptRoot
$pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"
$guiScript = Join-Path $projectRoot "scheduler_gui.py"

if (-not (Test-Path $pythonExe)) {
    Write-Error "Python executable not found at $pythonExe"
}

if (-not (Test-Path $guiScript)) {
    Write-Error "GUI script not found at $guiScript"
}

Push-Location $projectRoot
try {
    & $pythonExe -m PyInstaller --noconfirm --clean --windowed --name "MESDP Scheduler Control" "$guiScript"
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller build failed with exit code $LASTEXITCODE"
    }
    Write-Output "Build complete."
    Write-Output "EXE: $projectRoot\dist\MESDP Scheduler Control\MESDP Scheduler Control.exe"
}
finally {
    Pop-Location
}
