param(
    [switch]$Follow,
    [int]$Tail = 40
)

$projectRoot = $PSScriptRoot
$stdoutLog = Join-Path $projectRoot "logs\scheduler.out.log"
$stderrLog = Join-Path $projectRoot "logs\scheduler.err.log"

$running = Get-CimInstance Win32_Process |
    Where-Object {
        $_.CommandLine -and
        $_.CommandLine -like "*worklog_reminder.py*--schedule*" -and
        $_.CommandLine -like "*$projectRoot*"
    }

if ($running) {
    Write-Output "Scheduler process status: RUNNING"
    $running | Select-Object ProcessId, Name, CreationDate | Format-Table -AutoSize
} else {
    Write-Output "Scheduler process status: NOT RUNNING"
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
