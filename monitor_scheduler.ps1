param(
    [switch]$Follow,
    [int]$Tail = 40
)

$projectRoot = $PSScriptRoot
$parentRoot = Split-Path $projectRoot -Parent
$logDir = Join-Path $env:APPDATA "CLL MESDP\logs"
$stdoutLog = Join-Path $logDir "scheduler.out.log"
$stderrLog = Join-Path $logDir "scheduler.err.log"
$pidFile = Join-Path $logDir "scheduler.pid"

$queryWarning = ""
try {
    $running = Get-CimInstance Win32_Process -ErrorAction Stop |
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
    $queryWarning = "Warning: Could not query process command lines: $($_.Exception.Message)"
    $running = @()
}

if ($running) {
    Write-Output "Scheduler process status: RUNNING"
    $running | Select-Object ProcessId, Name, CreationDate | Format-Table -AutoSize
} else {
    $pidRunning = $False
    if (Test-Path $pidFile) {
        $pidText = (Get-Content $pidFile -Raw).Trim()
        $pidValue = 0
        if ([int]::TryParse($pidText, [ref]$pidValue)) {
            $pidProc = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
            if ($pidProc) {
                $pidRunning = $True
                Write-Output "Scheduler process status: RUNNING"
                $pidProc | Select-Object Id, ProcessName, StartTime | Format-Table -AutoSize
            }
        }
    }
    if (-not $pidRunning) {
        Write-Output "Scheduler process status: NOT RUNNING"
    }
}

if ($queryWarning) {
    Write-Output $queryWarning
}

Write-Output ""
Write-Output "Last stdout lines:"
if (Test-Path $stdoutLog) {
    Get-Content $stdoutLog -Tail $Tail
} else {
    Write-Output "No stdout log found: $stdoutLog"
}

Write-Output ""
Write-Output "Last stderr lines:"
if (Test-Path $stderrLog) {
    Get-Content $stderrLog -Tail $Tail
} else {
    Write-Output "No stderr log found: $stderrLog"
}

if ($Follow) {
    Write-Output ""
    Write-Output "Following stdout log (Ctrl+C to stop):"
    if (-not (Test-Path $stdoutLog)) {
        New-Item -Path $stdoutLog -ItemType File | Out-Null
    }
    Get-Content $stdoutLog -Wait -Tail 20
}
