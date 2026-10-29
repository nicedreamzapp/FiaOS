# FiaOS - Windows uninstaller.
#
#   powershell -ExecutionPolicy Bypass -File windows\uninstall.ps1 [-Purge]
#   irm https://raw.githubusercontent.com/nicedreamzapp/FiaOS/main/windows/uninstall.ps1 | iex
#
# Stops the FiaOS server that runs from this install, removes its scheduled task and the hidden
# launcher. Your install folder and C:\ProgramData\fia\fiaos.env (password, settings, log) are
# kept unless you pass -Purge (or set FIAOS_PURGE=1).
# Same settings as install.ps1: -Dir/FIAOS_DIR, -EnvFile/FIAOS_ENV_FILE, -TaskName/FIAOS_TASK.

function Uninstall-FiaOS {
  param(
    [string]$Dir = $(if ($env:FIAOS_DIR) { $env:FIAOS_DIR } else { Join-Path $env:USERPROFILE "FiaOS" }),
    [string]$EnvFile = $(if ($env:FIAOS_ENV_FILE) { $env:FIAOS_ENV_FILE } else { "C:\ProgramData\fia\fiaos.env" }),
    [string]$TaskName = $(if ($env:FIAOS_TASK) { $env:FIAOS_TASK } else { "FiaOS" }),
    [switch]$Purge = ($env:FIAOS_PURGE -eq "1")
  )
  $ErrorActionPreference = "Continue"
  Write-Host "== FiaOS - uninstall =="
  $win = (Join-Path $Dir "windows")

  if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "-> Removed task '$TaskName'"
  } else { Write-Host "-> No task '$TaskName'" }

  # Only processes started from THIS install: the launcher loop first, then the server.
  $mine = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -and $_.CommandLine.IndexOf($win, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and $_.ProcessId -ne $PID
  }
  $mine | Where-Object { $_.Name -match '^(powershell|pwsh|wscript)\.exe$' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  $mine | Where-Object { $_.Name -match '^python' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  if ($mine) { Write-Host "-> Stopped $(@($mine).Count) FiaOS process(es) from $win" }

  Remove-Item (Join-Path $win "run_hidden.vbs") -Force -ErrorAction SilentlyContinue

  if ($Purge) {
    Start-Sleep -Seconds 1
    if (Test-Path $Dir) { Remove-Item $Dir -Recurse -Force; Write-Host "-> Deleted $Dir" }
    $envDir = Split-Path -Parent $EnvFile
    foreach ($f in @($EnvFile, (Join-Path $envDir "fiaos.log"), (Join-Path $envDir "fiaos.log.1"))) {
      if (Test-Path $f) { Remove-Item $f -Force; Write-Host "-> Deleted $f" }
    }
    if ((Test-Path $envDir) -and -not (Get-ChildItem $envDir -Force)) { Remove-Item $envDir -Force }
  } else {
    Write-Host "-> Kept $Dir and $EnvFile (run with -Purge to delete them)"
  }
  Write-Host "Done."
}

Uninstall-FiaOS @args
