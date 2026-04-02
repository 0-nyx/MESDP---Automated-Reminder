$ErrorActionPreference = "Stop"

$projectRoot = $PSScriptRoot
$pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"
$scriptFile = Join-Path $projectRoot "worklog_reminder.py"
$logDir = Join-Path $projectRoot "logs"
$stdoutLog = Join-Path $logDir "scheduler.out.log"
$stderrLog = Join-Path $logDir "scheduler.err.log"

if (-not (Test-Path $pythonExe)) {
    Write-Error "Python executable not found at $pythonExe"
}

if (-not (Test-Path $scriptFile)) {
    Write-Error "Script not found at $scriptFile"
}

if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir | Out-Null
}

$existing = Get-CimInstance Win32_Process |
    Where-Object {
        $_.CommandLine -and
        $_.CommandLine -like "*worklog_reminder.py*--schedule*" -and
        $_.CommandLine -like "*$projectRoot*"
    }

if ($existing) {
    Write-Output "Scheduler is already running."
    $existing | Select-Object ProcessId, Name, CreationDate | Format-Table -AutoSize
    exit 0
}

$argList = @("-X", "utf8", "`"$scriptFile`"", "--schedule")
$proc = Start-Process -FilePath $pythonExe `
    -ArgumentList $argList `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog `
    -PassThru

Write-Output "Scheduler started in background. PID: $($proc.Id)"
Write-Output "Stdout log: $stdoutLog"
Write-Output "Stderr log: $stderrLog"
