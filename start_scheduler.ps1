$ErrorActionPreference = "Stop"

$projectRoot = $PSScriptRoot
$parentRoot = Split-Path $projectRoot -Parent
$pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"
$scriptFile = Join-Path $projectRoot "worklog_reminder.py"
$appExe = Join-Path $projectRoot "MESDP Scheduler Control.exe"
if (-not (Test-Path $appExe)) {
    $appExe = Join-Path $parentRoot "MESDP Scheduler Control.exe"
}
$logDir = Join-Path $env:APPDATA "CLL MESDP\logs"
$stdoutLog = Join-Path $logDir "scheduler.out.log"
$stderrLog = Join-Path $logDir "scheduler.err.log"

if (-not (Test-Path $pythonExe) -and -not (Test-Path $appExe)) {
    Write-Error "No scheduler runtime found. Expected either: $pythonExe or $appExe"
}

if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir | Out-Null
}

$existing = Get-CimInstance Win32_Process |
    Where-Object {
        $_.CommandLine -and
        (
            $_.CommandLine -like "*worklog_reminder.py*--schedule*" -or
            $_.CommandLine -like "*MESDP Scheduler Control.exe*--schedule*"
        ) -and (
            $_.CommandLine -like "*$projectRoot*" -or
            $_.CommandLine -like "*$parentRoot*"
        )
    }

if ($existing) {
    Write-Output "Scheduler is already running."
    $existing | Select-Object ProcessId, Name, CreationDate | Format-Table -AutoSize
    exit 0
}

if ((Test-Path $pythonExe) -and (Test-Path $scriptFile)) {
    $argList = "-X utf8 `"$scriptFile`" --schedule"
    $proc = Start-Process -FilePath $pythonExe `
        -ArgumentList $argList `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog `
        -PassThru
} else {
    $argList = @("--schedule")
    $proc = Start-Process -FilePath $appExe `
        -ArgumentList $argList `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog `
        -PassThru
}

Write-Output "Scheduler started in background. PID: $($proc.Id)"
Write-Output "Stdout log: $stdoutLog"
Write-Output "Stderr log: $stderrLog"
