# Build dist\agentman-tray.exe (single file, no console window).
# Usage (PowerShell, from the repo root):  .\scripts\build_windows.ps1
$ErrorActionPreference = "Stop"

python -m pip install --upgrade pip
python -m pip install ".[tray]" pyinstaller

New-Item -ItemType Directory -Force build | Out-Null
python -c "from agentman.icon import save_ico; save_ico('build/agentman.ico')"

python -m PyInstaller --noconfirm --clean --onefile --noconsole `
  --name agentman-tray `
  --icon build/agentman.ico `
  --add-data "src/agentman/assets;agentman/assets" `
  --hidden-import pystray._win32 `
  --collect-submodules webview `
  --paths src `
  scripts/agentman_tray.py

Write-Host "built dist\agentman-tray.exe"
