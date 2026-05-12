$ErrorActionPreference = "Stop"

$projectRoot = $PSScriptRoot
$pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"
$guiScript = Join-Path $projectRoot "scheduler_gui.py"
$schedulerScript = Join-Path $projectRoot "worklog_reminder.py"
$iconFile = Join-Path $projectRoot "dist\app_icon.ico"
$pythonRoot = (& $pythonExe -c "import sys; print(sys.base_prefix)")
$tclLibrary = Join-Path $pythonRoot "tcl\tcl8.6"
$tkLibrary = Join-Path $pythonRoot "tcl\tk8.6"
$tkinterLibrary = Join-Path $pythonRoot "Lib\tkinter"
$tkinterPyd = Join-Path $pythonRoot "DLLs\_tkinter.pyd"
$tclDll = Join-Path $pythonRoot "DLLs\tcl86t.dll"
$tkDll = Join-Path $pythonRoot "DLLs\tk86t.dll"

if (-not (Test-Path $pythonExe)) {
    Write-Error "Python executable not found at $pythonExe"
}

if (-not (Test-Path $guiScript)) {
    Write-Error "GUI script not found at $guiScript"
}

if (-not (Test-Path $schedulerScript)) {
    Write-Error "Scheduler script not found at $schedulerScript"
}

if (-not (Test-Path $iconFile)) {
    Write-Error "Icon file not found at $iconFile"
}

if (-not (Test-Path $tclLibrary)) {
    Write-Error "Tcl library not found at $tclLibrary"
}

if (-not (Test-Path $tkLibrary)) {
    Write-Error "Tk library not found at $tkLibrary"
}

if (-not (Test-Path $tkinterLibrary)) {
    Write-Error "Tkinter Python library not found at $tkinterLibrary"
}

foreach ($tkAsset in @($tkinterPyd, $tclDll, $tkDll)) {
    if (-not (Test-Path $tkAsset)) {
        Write-Error "Tkinter asset not found at $tkAsset"
    }
}

Push-Location $projectRoot
try {
    $env:TCL_LIBRARY = $tclLibrary
    $env:TK_LIBRARY = $tkLibrary
    & $pythonExe -m PyInstaller `
        --noconfirm `
        --clean `
        --windowed `
        --name "MESDP Scheduler Control" `
        --icon "$iconFile" `
        --hidden-import tkinter `
        --hidden-import tkinter.ttk `
        --hidden-import tkinter.filedialog `
        --hidden-import tkinter.font `
        --hidden-import tkinter.constants `
        --add-binary "$tkinterPyd;." `
        --add-binary "$tclDll;." `
        --add-binary "$tkDll;." `
        --add-data "$iconFile;." `
        --add-data "$tkinterLibrary;tkinter" `
        --add-data "$($pythonRoot)\tcl;tcl" `
        "$guiScript"
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller build failed with exit code $LASTEXITCODE"
    }

    $distAppDir = Join-Path $projectRoot "dist\MESDP Scheduler Control"

    & $pythonExe -m PyInstaller `
        --noconfirm `
        --clean `
        --console `
        --name "MESDP Worklog Reminder" `
        --distpath "$distAppDir" `
        --workpath "$projectRoot\build\MESDP Worklog Reminder" `
        --specpath "$projectRoot\build\MESDP Worklog Reminder" `
        "$schedulerScript"
    if ($LASTEXITCODE -ne 0) {
        throw "Scheduler PyInstaller build failed with exit code $LASTEXITCODE"
    }

    $oldSchedulerOneFile = Join-Path $distAppDir "MESDP Worklog Reminder.exe"
    if (Test-Path $oldSchedulerOneFile) {
        Remove-Item -LiteralPath $oldSchedulerOneFile -Force
    }

    $runtimeFiles = @(
        "start_scheduler.ps1",
        "stop_scheduler.ps1",
        "restart_scheduler.ps1",
        "monitor_scheduler.ps1"
    )
    foreach ($runtimeFile in $runtimeFiles) {
        $sourcePath = Join-Path $projectRoot $runtimeFile
        if (-not (Test-Path $sourcePath)) {
            throw "Runtime helper script not found: $sourcePath"
        }
        Copy-Item -LiteralPath $sourcePath -Destination (Join-Path $distAppDir $runtimeFile) -Force
    }

    $blockedPackageNames = @(
        "app_config.json",
        "config.local.json",
        "smtp_test.py",
        "preview_worklog_email.html",
        "preview_sandbox_review.html",
        "scheduler.out.log",
        "scheduler.err.log",
        "scheduler.pid"
    )
    foreach ($blockedName in $blockedPackageNames) {
        Get-ChildItem -Path $distAppDir -Recurse -File -Filter $blockedName -ErrorAction SilentlyContinue |
            Remove-Item -Force -ErrorAction Stop
    }

    $readmePath = Join-Path $distAppDir "README_PORTABLE.txt"
    @"
CLL MESDP Ticketing Worklog - Portable Build

Run:
  MESDP Scheduler Control.exe

Notes:
  - This package intentionally does not include app_config.json, tokens, passwords, logs, or preview reports.
  - First-run setup is stored per user under %APPDATA%\CLL MESDP\app_config.json.
  - The background scheduler is launched by start_scheduler.ps1 using MESDP Worklog Reminder.exe.
"@ | Set-Content -Path $readmePath -Encoding UTF8

    $portableZip = Join-Path $projectRoot "dist\MESDP_Scheduler_Control_Portable.zip"
    if (Test-Path $portableZip) {
        Remove-Item -LiteralPath $portableZip -Force
    }
    Compress-Archive -Path (Join-Path $distAppDir "*") -DestinationPath $portableZip -Force

    Write-Output "Build complete."
    Write-Output "EXE: $projectRoot\dist\MESDP Scheduler Control\MESDP Scheduler Control.exe"
    Write-Output "Portable ZIP: $portableZip"
}
finally {
    Pop-Location
}
