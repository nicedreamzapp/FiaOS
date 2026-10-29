# FiaOS - Windows installer.
#
#   irm https://raw.githubusercontent.com/nicedreamzapp/FiaOS/main/windows/install.ps1 | iex
#
# or, from a checkout:  powershell -ExecutionPolicy Bypass -File windows\install.ps1 [-Dir C:\FiaOS] [-NoStart]
#
# Clones (or updates) FiaOS, builds windows\.venv with the Windows requirements, asks for the
# login password (typed, never on a command line) and writes it to C:\ProgramData\fia\fiaos.env,
# readable only by you, SYSTEM and Administrators. Then registers a Task Scheduler task that
# starts the server at logon in your desktop session, with no console window.
#
# Settings (params when run from a file, env vars when piped into iex):
#   -Dir      / FIAOS_DIR        install folder            (default %USERPROFILE%\FiaOS)
#   -EnvFile  / FIAOS_ENV_FILE   password + settings file  (default C:\ProgramData\fia\fiaos.env)
#   -TaskName / FIAOS_TASK       scheduled task name       (default FiaOS)
#   -Machine  / FIAOS_MACHINE    this machine's key        (default pc)
#   -NoStart  / FIAOS_NO_START=1 register the task but don't start it now
#   -ResetPassword               ask for a new password even if one is already set
# Without a console to type in (CCL_NONINTERACTIVE=1, -NonInteractive), a random password is
# generated and written to the env file; open that file to read it.

function Install-FiaOS {
  param(
    [string]$Dir = $(if ($env:FIAOS_DIR) { $env:FIAOS_DIR } else { Join-Path $env:USERPROFILE "FiaOS" }),
    [string]$EnvFile = $(if ($env:FIAOS_ENV_FILE) { $env:FIAOS_ENV_FILE } else { "C:\ProgramData\fia\fiaos.env" }),
    [string]$TaskName = $(if ($env:FIAOS_TASK) { $env:FIAOS_TASK } else { "FiaOS" }),
    [string]$Machine = $(if ($env:FIAOS_MACHINE) { $env:FIAOS_MACHINE } else { "pc" }),
    [switch]$NoStart = ($env:FIAOS_NO_START -eq "1"),
    [switch]$ResetPassword
  )
  $ErrorActionPreference = "Stop"
  $Repo = "https://github.com/nicedreamzapp/FiaOS"

  function Test-Interactive {
    if ($env:CCL_NONINTERACTIVE -eq "1") { return $false }
    if (-not [Environment]::UserInteractive) { return $false }
    if ([Environment]::GetCommandLineArgs() | Where-Object { $_ -like "-NonI*" }) { return $false }
    try { if ([Console]::IsInputRedirected) { return $false } } catch { }
    return $true
  }

  Write-Host ""
  Write-Host "== FiaOS - Windows installer =="
  if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "git not found. Install Git for Windows from https://git-scm.com/download/win"
  }

  # Python 3.11-3.13 first (requirements-windows.txt pins aiohttp 3.10, which has wheels up to 3.13),
  # 3.14 last; the py launcher, then python on PATH.
  $python = $null
  foreach ($v in "3.13", "3.12", "3.11", "3.14") {
    try { $p = (& py "-$v" -c "import sys; print(sys.executable)" 2>$null); if ($LASTEXITCODE -eq 0 -and $p) { $python = $p.Trim(); break } } catch { }
  }
  if (-not $python) {
    $c = Get-Command python -ErrorAction SilentlyContinue
    if ($c) {
      $ok = & $c.Source -c "import sys; print(int(sys.version_info >= (3, 11)))" 2>$null
      if ("$ok".Trim() -eq "1") { $python = $c.Source }
    }
  }
  if (-not $python) { throw "Python 3.11 or newer not found (3.13 recommended). Install it from https://www.python.org/downloads/" }
  Write-Host "-> Python: $python"

  if (Test-Path (Join-Path $Dir ".git")) {
    Write-Host "-> Updating $Dir"
    git -C $Dir pull --ff-only --quiet
    if ($LASTEXITCODE -ne 0) { Write-Host "   (local changes kept, skipping the pull)" }
  } elseif (Test-Path $Dir) {
    throw "$Dir exists and is not a git checkout. Move it aside or pass -Dir / set FIAOS_DIR."
  } else {
    Write-Host "-> Cloning into $Dir"
    git clone --quiet --depth 1 $Repo $Dir
    if ($LASTEXITCODE -ne 0) { throw "git clone failed" }
  }

  $win = Join-Path $Dir "windows"
  # windows\server.py serves windows\static, which is the repo's static folder.
  $static = Join-Path $win "static"
  if (Test-Path $static) { Remove-Item $static -Recurse -Force }
  Copy-Item -Recurse (Join-Path $Dir "static") $static

  $venvPy = Join-Path $win ".venv\Scripts\python.exe"
  if (-not (Test-Path $venvPy)) {
    Write-Host "-> Creating windows\.venv"
    & $python -m venv (Join-Path $win ".venv")
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
  }
  Write-Host "-> Installing Python packages"
  & $venvPy -m pip install -q --disable-pip-version-check -r (Join-Path $win "requirements-windows.txt")
  if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

  # The env file: keep what is there, fill in what is missing.
  $envDir = Split-Path -Parent $EnvFile
  New-Item -ItemType Directory -Force $envDir | Out-Null
  $lines = @()
  if (Test-Path $EnvFile) { $lines = @(Get-Content $EnvFile) }
  $hasPw = [bool]($lines | Where-Object { $_ -match '^\s*FIAOS_PASSWORD=.+' })
  if ($ResetPassword -or -not $hasPw) {
    if (Test-Interactive) {
      while ($true) {
        $a = Read-Host "Choose a FiaOS login password" -AsSecureString
        $b = Read-Host "Type it again" -AsSecureString
        $pa = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($a))
        $pb = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($b))
        if ($pa -ne $pb) { Write-Host "They did not match, try again."; continue }
        if ($pa.Length -lt 8) { Write-Host "Use at least 8 characters."; continue }
        $pw = $pa; break
      }
      $pa = $null; $pb = $null
    } else {
      $bytes = New-Object byte[] 18
      [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
      $pw = [Convert]::ToBase64String($bytes).Replace("+", "x").Replace("/", "y")
      Write-Host "-> No console to type a password in: generated a random one. It is in $EnvFile"
    }
    $lines = @($lines | Where-Object { $_ -notmatch '^\s*FIAOS_PASSWORD=' }) + "FIAOS_PASSWORD=$pw"
    $pw = $null
  }
  if (-not ($lines | Where-Object { $_ -match '^\s*FIAOS_MACHINE=' })) { $lines += "FIAOS_MACHINE=$Machine" }
  Set-Content -Path $EnvFile -Value $lines -Encoding ASCII
  # Only you, SYSTEM and Administrators can read it.
  $me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
  & icacls $EnvFile /inheritance:r /grant:r "${me}:F" "*S-1-5-18:F" "*S-1-5-32-544:F" | Out-Null

  # Hidden launcher: wscript runs the loop with no window, so nothing flashes at logon.
  $runner = Join-Path $win "run_fiaos.ps1"
  $vbs = Join-Path $win "run_hidden.vbs"
  $cmd = "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""$runner"" -EnvFile ""$EnvFile"""
  $vbsCmd = $cmd.Replace([string][char]34, [string][char]34 + [char]34)   # VBScript escapes " as ""
  Set-Content -Path $vbs -Encoding ASCII -Value @(
    "' Starts FiaOS with no console window (scheduled task ""$TaskName"").",
    "CreateObject(""WScript.Shell"").Run ""$vbsCmd"", 0, False"
  )

  $user = "$env:USERDOMAIN\$env:USERNAME"
  $action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$vbs`""
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
  $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Force | Out-Null
  Write-Host "-> Task '$TaskName' registered (starts at logon)"

  $port = 9000
  $pl = $lines | Where-Object { $_ -match '^\s*FIAOS_PORT=(\d+)' } | Select-Object -First 1
  if ($pl -match '=(\d+)') { $port = [int]$Matches[1] }
  if ($NoStart) {
    Write-Host "-> Not started now (-NoStart). It starts at your next logon."
  } else {
    Start-ScheduledTask -TaskName $TaskName
    $up = $false
    for ($i = 0; $i -lt 45; $i++) {
      Start-Sleep -Seconds 1
      try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 "http://127.0.0.1:$port/login" | Out-Null; $up = $true; break } catch { }
    }
    if ($up) { Write-Host "-> FiaOS is up: http://localhost:$port" }
    else { Write-Host "-> FiaOS did not answer on $port yet; see $(Join-Path $envDir 'fiaos.log')" }
  }
  Write-Host ""
  Write-Host "Done. Open http://localhost:$port and log in with your password."
  Write-Host "Uninstall: powershell -ExecutionPolicy Bypass -File `"$(Join-Path $win 'uninstall.ps1')`""
}

Install-FiaOS @args
