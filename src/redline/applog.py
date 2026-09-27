"""File log for the tray app: $REDLINE_HOME/redline.log (rotated at ~1 MB).

The packaged exe has no console, so without this a failure during startup or a
self-update leaves no trace at all.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading

from .config import redline_home

log = logging.getLogger("redline")


def log_path():
    return redline_home() / "redline.log"


def setup() -> None:
    if log.handlers:
        return
    try:
        p = log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        h = logging.handlers.RotatingFileHandler(p, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    except OSError:
        return
    h.setFormatter(logging.Formatter(f"%(asctime)s [{os.getpid()}] %(levelname)s %(message)s"))
    log.addHandler(h)
    log.setLevel(logging.INFO)

    def hook(exc_type, exc, tb):
        log.critical("uncaught exception", exc_info=(exc_type, exc, tb))
        if sys.__excepthook__:
            sys.__excepthook__(exc_type, exc, tb)

    def thread_hook(args):
        log.critical("uncaught exception in thread %s", getattr(args.thread, "name", "?"),
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = hook
    threading.excepthook = thread_hook
