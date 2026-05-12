$ErrorActionPreference = "Stop"

$projectRoot = $PSScriptRoot
$parentRoot = Split-Path $projectRoot -Parent
$pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"
$scriptFile = Join-Path $projectRoot "worklog_reminder.py"
$schedulerExe = Join-Path $projectRoot "MESDP Worklog Reminder\MESDP Worklog Reminder.exe"
if (-not (Test-Path $schedulerExe)) {
    $schedulerExe = Join-Path $projectRoot "MESDP Worklog Reminder.exe"
}
$appExe = Join-Path $projectRoot "MESDP Scheduler Control.exe"
if (-not (Test-Path $appExe)) {
    $appExe = Join-Path $parentRoot "MESDP Scheduler Control.exe"
}
$logDir = Join-Path $env:APPDATA "CLL MESDP\logs"
$stdoutLog = Join-Path $logDir "scheduler.out.log"
$stderrLog = Join-Path $logDir "scheduler.err.log"
$pidFile = Join-Path $logDir "scheduler.pid"

if (-not (Test-Path $pythonExe) -and -not (Test-Path $schedulerExe) -and -not (Test-Path $appExe)) {
    Write-Error "No scheduler runtime found. Expected one of: $pythonExe, $schedulerExe, $appExe"
}

if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir | Out-Null
}

if (Test-Path $pidFile) {
    $pidText = (Get-Content $pidFile -Raw).Trim()
    $pidValue = 0
    if ([int]::TryParse($pidText, [ref]$pidValue)) {
        $pidProc = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
        if ($pidProc) {
            Write-Output "Scheduler is already running. PID: $pidValue"
            exit 0
        }
    }
}

try {
    $existing = Get-CimInstance Win32_Process -ErrorAction Stop |
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
} catch {
    Write-Output "Warning: Could not check existing scheduler process: $($_.Exception.Message)"
    $existing = @()
}

if ($existing) {
    Write-Output "Scheduler is already running."
    $existing | Select-Object ProcessId, Name, CreationDate | Format-Table -AutoSize
    try {
        $existing[0].ProcessId | Set-Content -Path $pidFile -Encoding ASCII -ErrorAction Stop
    } catch {
        Write-Output "Warning: Could not write PID file: $($_.Exception.Message)"
    }
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
} elseif (Test-Path $schedulerExe) {
    $proc = Start-Process -FilePath $schedulerExe `
        -ArgumentList @("--schedule") `
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

try {
    $proc.Id | Set-Content -Path $pidFile -Encoding ASCII -ErrorAction Stop
} catch {
    Write-Output "Warning: Could not write PID file: $($_.Exception.Message)"
}
Write-Output "Scheduler started in background. PID: $($proc.Id)"
Write-Output "Stdout log: $stdoutLog"
Write-Output "Stderr log: $stderrLog"
