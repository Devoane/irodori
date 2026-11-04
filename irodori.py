"""Irodori launcher for Linux, Windows and macOS.

    python irodori.py          start (first run creates .venv and installs requirements.txt)
    python irodori.py --stop   stop a running server

Settings: IRODORI_PORT (default 8765), IRODORI_NO_BROWSER=1. The server only
listens on 127.0.0.1. Tool paths: see backend/tools.py.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import venv
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
PORT = int(os.environ.get("IRODORI_PORT", "8765"))
URL = f"http://127.0.0.1:{PORT}/"
PIDFILE = Path(tempfile.gettempdir()) / f"irodori-{PORT}.pid"


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def in_venv() -> bool:
    return Path(sys.prefix).resolve() == VENV.resolve()


def ensure_venv():
    """Create the environment on first run, then continue inside it."""
    if in_venv():
        return
    if not venv_python().exists():
        print("Pierwsze uruchomienie: tworzę środowisko w .venv i instaluję zależności…", flush=True)
        venv.create(VENV, with_pip=True)
        subprocess.check_call([str(venv_python()), "-m", "pip", "install", "--disable-pip-version-check", "-q",
                               "-r", str(ROOT / "requirements.txt")])
    sys.exit(subprocess.call([str(venv_python()), str(ROOT / "irodori.py"), *sys.argv[1:]]))


def running() -> bool:
    try:
        with urllib.request.urlopen(URL + "api/health", timeout=1) as r:
            return json.load(r).get("app") == "irodori"
    except OSError:
        return False


def open_browser():
    if not os.environ.get("IRODORI_NO_BROWSER"):
        webbrowser.open(URL)


def stop():
    try:
        pid = int(PIDFILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("Irodori nie działa.")
        return
    try:
        os.kill(pid, signal.SIGTERM)
        print("Zatrzymano.")
    except OSError:
        print("Irodori nie działa.")
    PIDFILE.unlink(missing_ok=True)


def serve():
    if running():
        open_browser()
        return
    import uvicorn
    PIDFILE.write_text(str(os.getpid()), encoding="utf-8")
    threading.Thread(target=lambda: (time.sleep(1.5), open_browser()), daemon=True).start()
    try:
        uvicorn.run("server:app", app_dir=str(ROOT / "backend"), host="127.0.0.1", port=PORT, log_level="warning")
    finally:
        PIDFILE.unlink(missing_ok=True)


if __name__ == "__main__":
    if "--stop" in sys.argv:
        stop()
    else:
        ensure_venv()
        serve()
