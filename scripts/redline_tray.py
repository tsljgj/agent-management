"""PyInstaller entry point for the Windows tray build (redline.exe).

`redline.exe`            -> tray app
`redline.exe <url>`      -> browser callback (we are $BROWSER during a login)
`redline.exe --self-test`
"""

import sys

from redline.cli import main

if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) == 1 and args[0].strip("\"'").startswith(("http://", "https://")):
        sys.exit(main(args))
    sys.exit(main(["tray", *args]))
