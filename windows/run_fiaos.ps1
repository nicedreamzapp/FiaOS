# FiaOS launcher — Windows shop PC.
# Reads credentials from C:\ProgramData\fia\fiaos.env so no secret ever appears
# on a command line, then runs the server in the interactive session (screen
# capture and SendInput only work from inside the logged-in desktop).
$ErrorActionPreference = 'Continue'
$envFile = 'C:\ProgramData\fia\fiaos.env'
$log = 'C:\ProgramData\fia\fiaos.log'

Get-Content $envFile | ForEach-Object {
    if ($_ -match '^\s*([A-Z_]+)=(.*)$') {
        [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')
    }
}

Set-Location "$PSScriptRoot"
while ($true) {
    $start = Get-Date
    if ((Test-Path $log) -and ((Get-Item $log).Length -gt 5MB)) {
        Move-Item $log "$log.1" -Force -ErrorAction SilentlyContinue
    }
    Add-Content $log "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] starting server.py"
    & "$PSScriptRoot\.venv\Scripts\python.exe" -u server.py 2>&1 |
        ForEach-Object { Add-Content $log $_ }
    $ran = [int]((Get-Date) - $start).TotalSeconds
    Add-Content $log "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] server exited after ${ran}s; restarting in 5s"
    Start-Sleep -Seconds 5
}
