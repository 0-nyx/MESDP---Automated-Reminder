$projectRoot = $PSScriptRoot
$parentRoot = Split-Path $projectRoot -Parent

$running = Get-CimInstance Win32_Process |
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

if (-not $running) {
    Write-Output "No running scheduler process found."
    exit 0
}

$running | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    Write-Output "Stopped scheduler PID: $($_.ProcessId)"
}
