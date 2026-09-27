# Build dist\redline.exe (single file, no console window).
# Usage (PowerShell, from the repo root):  .\scripts\build_windows.ps1
$ErrorActionPreference = "Stop"

python -m pip install --upgrade pip
python -m pip install ".[tray]" pyinstaller

New-Item -ItemType Directory -Force build | Out-Null
python -c "from redline.icon import save_ico; save_ico('build/redline.ico')"

python -m PyInstaller --noconfirm --clean --onefile --noconsole `
  --name redline `
  --icon build/redline.ico `
  --add-data "src/redline/assets;redline/assets" `
  --hidden-import pystray._win32 `
  --collect-submodules webview `
  --paths src `
  scripts/redline_tray.py

Write-Host "built dist\redline.exe"
