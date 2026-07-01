"""Logging, crash capture, and per-user data locations.

A windowed .app has no terminal, so a Python traceback or a Qt warning would
otherwise vanish. This routes everything to a rotating log file the user can find,
installs a ``sys.excepthook`` that logs unhandled exceptions (and lets the app keep
running instead of dying silently), and forwards Qt's own messages into the log.

Locations (created on demand):
  macOS   log:  ~/Library/Logs/DogTrialVideoClipper/clipper.log
          data: ~/Library/Application Support/DogTrialVideoClipper/
  Windows       %LOCALAPPDATA%/DogTrialVideoClipper/{logs,}
  other         ~/.dogtrialvideoclipper/{logs,}
"""
from __future__ import annotations

import logging
import os
import platform
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_APP = "DogTrialVideoClipper"
log = logging.getLogger("clipper")


def app_data_dir() -> Path:
    """Per-user directory for persistent app state (queue, recovery)."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / _APP
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / _APP
    else:
        base = Path.home() / f".{_APP.lower()}"
    base.mkdir(parents=True, exist_ok=True)
    return base


def log_dir() -> Path:
    if sys.platform == "darwin":
        d = Path.home() / "Library" / "Logs" / _APP
    else:
        d = app_data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def setup_logging(version: str = "") -> Path:
    """Attach a rotating file handler (+ stderr for dev). Returns the log path."""
    logfile = log_dir() / "clipper.log"
    root = logging.getLogger()
    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        fh = RotatingFileHandler(logfile, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(fh)
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        root.addHandler(sh)
        root.setLevel(logging.INFO)
    log.info("==== %s %s starting | %s | py%s ====",
             _APP, version or "?", platform.platform(), sys.version.split()[0])
    return logfile


def install_qt_message_handler() -> None:
    """Forward Qt's own debug/warning/critical messages into the log (they can
    precede a native crash, so they're worth capturing)."""
    from PySide6.QtCore import QtMsgType, qInstallMessageHandler

    level = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }

    def handler(mode, _context, message):
        logging.getLogger("Qt").log(level.get(mode, logging.INFO), "%s", message)

    qInstallMessageHandler(handler)


def install_excepthook(on_error=None) -> None:
    """Log any unhandled exception (main thread / Qt slots). ``on_error`` is called
    with (type, value, tb) so the UI can show a non-fatal notice. We deliberately
    don't re-raise, so a slot-level error is logged rather than killing the app."""
    def hook(exc_type, exc, tb):
        log.critical("Unhandled exception", exc_info=(exc_type, exc, tb))
        if on_error is not None:
            try:
                on_error(exc_type, exc, tb)
            except Exception:
                log.exception("on_error handler itself failed")

    sys.excepthook = hook
