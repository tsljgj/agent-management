"""PyInstaller entry point for the Windows tray build (agentman-tray.exe)."""

import sys

from agentman.cli import main

if __name__ == "__main__":
    sys.exit(main(["tray", *sys.argv[1:]]))
