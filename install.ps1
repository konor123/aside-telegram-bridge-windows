# aside-telegram-bridge one-line installer for Windows
#   irm https://raw.githubusercontent.com/konor123/aside-telegram-bridge-windows/main/install.ps1 | iex
#
# Runs as a script block so that `return` ends the install without closing
# the PowerShell window it was pasted into.
& {
  $Repo = 'https://github.com/konor123/aside-telegram-bridge-windows'
  $Dest = if ($env:ASIDE_BRIDGE_DIR) { $env:ASIDE_BRIDGE_DIR } else { Join-Path $HOME 'aside-telegram-bridge' }

  function Fail([string]$Message) { Write-Host "x $Message" -ForegroundColor Red }
  function Note([string]$Message) { Write-Host "! $Message" -ForegroundColor Yellow }

  if ($env:OS -ne 'Windows_NT') {
    Fail 'This installer is for Windows. On macOS use install.sh.'
    return
  }

  if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Fail 'git is required. Install it, open a new terminal, and run this again:'
    Write-Host '    winget install --id Git.Git -e'
    return
  }

  # A real Python 3.9+. The python.exe in WindowsApps can be a stub that
  # only opens the Microsoft Store, so each candidate is asked to report
  # its own path rather than trusted for being on PATH.
  $Python = $null
  foreach ($candidate in @(@('py', '-3'), @('python'), @('python3'))) {
    $exe = $candidate[0]
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
    $pyArgs = @($candidate | Select-Object -Skip 1) + @('-c', 'import sys; print(sys.executable) if sys.version_info >= (3, 9) else None')
    try {
      $out = & $exe @pyArgs 2>$null
    } catch {
      continue
    }
    if ($LASTEXITCODE -eq 0 -and $out) {
      $path = ([string]($out | Select-Object -Last 1)).Trim()
      if ($path -and $path -ne 'None' -and (Test-Path -LiteralPath $path)) {
        $Python = $path
        break
      }
    }
  }
  if (-not $Python) {
    Fail 'Python 3.9 or newer is required. Install it, open a new terminal, and run this again:'
    Write-Host '    winget install --id Python.Python.3.12 -e'
    return
  }

  # The Mini App is part of the default install, so its prerequisite is
  # checked here rather than halfway through. Not fatal: the chat bridge
  # alone is still worth installing.
  $MiniappHint = "    & `"$Python`" `"$(Join-Path $Dest 'miniapp\setup-miniapp.py')`""
  if (Get-Command node -ErrorAction SilentlyContinue) {
    $major = 0
    try { $major = [int](& node -p "process.versions.node.split('.')[0]" 2>$null | Select-Object -Last 1) } catch { $major = 0 }
    if ($major -lt 20) {
      Note "Node $major found, but the Mini App needs 20+."
      Write-Host '  The chat bridge will still install. Upgrade Node, then run:'
      Write-Host $MiniappHint
    }
  } else {
    Note 'Node 20+ not found -- the Mini App needs it (winget install OpenJS.NodeJS.LTS).'
    Write-Host '  The chat bridge will still install. After installing Node, run:'
    Write-Host $MiniappHint
  }

  if (Test-Path -LiteralPath (Join-Path $Dest '.git')) {
    Write-Host "-> Existing install found at $Dest, updating..."
    & git -C $Dest pull --ff-only
    if ($LASTEXITCODE -ne 0) { Note "couldn't fast-forward (local edits?), continuing with current copy" }
  } else {
    Write-Host "-> Cloning into $Dest..."
    & git clone --depth 1 $Repo $Dest
    if ($LASTEXITCODE -ne 0) {
      Fail 'git clone failed'
      return
    }
  }

  Push-Location -LiteralPath $Dest
  try {
    & $Python setup.py
  } finally {
    Pop-Location
  }
}
