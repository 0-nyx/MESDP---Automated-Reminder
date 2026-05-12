$projectRoot = $PSScriptRoot
$parentRoot = Split-Path $projectRoot -Parent
$logDir = Join-Path $env:APPDATA "CLL MESDP\logs"
$pidFile = Join-Path $logDir "scheduler.pid"

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
    Write-Output "Warning: Could not query scheduler process: $($_.Exception.Message)"
    $running = @()
}

if (-not $running) {
    if (Test-Path $pidFile) {
        $pidText = (Get-Content $pidFile -Raw).Trim()
        $pidValue = 0
        if ([int]::TryParse($pidText, [ref]$pidValue)) {
            $pidProc = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
            if ($pidProc) {
                Stop-Process -Id $pidValue -Force -ErrorAction SilentlyContinue
                Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
                Write-Output "Stopped scheduler PID: $pidValue"
                exit 0
            }
        }
    }
    Write-Output "No running scheduler process found."
    exit 0
}

$running | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    Write-Output "Stopped scheduler PID: $($_.ProcessId)"
}
Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
