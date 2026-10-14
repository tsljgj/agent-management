# Build dist\redline.exe (single file, no console window).
# Usage (PowerShell, from the repo root):  .\scripts\build_windows.ps1
$ErrorActionPreference = "Stop"

# "Stop" does not cover native programs (pip, python, PyInstaller): check their exit codes,
# or a failed step still ends with "built dist\redline.exe" next to a stale exe.
function Assert-ExitCode([string]$step) {
  if ($LASTEXITCODE -ne 0) { throw "$step failed (exit code $LASTEXITCODE)" }
}

python -m pip install --upgrade pip
Assert-ExitCode "pip upgrade"
python -m pip install ".[tray]" pyinstaller
Assert-ExitCode "pip install"

New-Item -ItemType Directory -Force build | Out-Null
python -c "from redline.icon import save_ico; save_ico('build/redline.ico')"
Assert-ExitCode "icon"

python -m PyInstaller --noconfirm --clean --onefile --noconsole `
  --name redline `
  --icon build/redline.ico `
  --add-data "src/redline/assets;redline/assets" `
  --hidden-import pystray._win32 `
  --collect-submodules webview `
  --paths src `
  scripts/redline_tray.py
Assert-ExitCode "PyInstaller"

Write-Host "built dist\redline.exe"
