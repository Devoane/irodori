"""Kaicho launcher for Linux and Windows.

    python kaicho.py          start (first run creates .venv and installs requirements.txt)
    python kaicho.py --stop   stop a running server

Settings: KAICHO_PORT (default 8765), KAICHO_NO_BROWSER=1. The server only
listens on 127.0.0.1. Tool paths: see backend/tools.py.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import venv
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
PORT = int(os.environ.get("KAICHO_PORT", "8765"))
URL = f"http://127.0.0.1:{PORT}/"
PIDFILE = ROOT / "data" / f".kaicho-{PORT}.pid"


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def in_venv() -> bool:
    return Path(sys.prefix).resolve() == VENV.resolve()


def ensure_venv():
    """Create the environment on first run, then continue inside it."""
    if in_venv():
        return
    if not venv_python().exists():
        print("First run: creating the .venv environment and installing dependencies…", flush=True)
        venv.create(VENV, with_pip=True, upgrade_deps=True)
        subprocess.check_call([str(venv_python()), "-m", "pip", "install", "--disable-pip-version-check", "-q",
                               "-r", str(ROOT / "requirements.txt")])
    sys.exit(subprocess.call([str(venv_python()), str(ROOT / "kaicho.py"), *sys.argv[1:]]))


def running() -> bool:
    try:
        with urllib.request.urlopen(URL + "api/health", timeout=1) as r:
            return json.load(r).get("app") == "kaicho"
    except OSError:
        return False


CHROMIUM = ["vivaldi", "vivaldi-stable", "google-chrome-stable", "google-chrome", "chromium", "chromium-browser",
            "brave-browser", "microsoft-edge"]
CHROMIUM_FLATPAK = ["com.vivaldi.Vivaldi", "com.google.Chrome", "org.chromium.Chromium", "com.brave.Browser", "com.microsoft.Edge"]
CHROMIUM_WINDOWS = ["Microsoft/Edge/Application/msedge.exe", "Google/Chrome/Application/chrome.exe",
                    "BraveSoftware/Brave-Browser/Application/brave.exe", "Vivaldi/Application/vivaldi.exe"]


def on_host() -> list[str]:
    """Inside a distrobox the browser lives on the host."""
    return ["distrobox-host-exec"] if os.environ.get("CONTAINER_ID") and shutil.which("distrobox-host-exec") else []


def host_ok(*cmd: str) -> bool:
    try:
        return subprocess.run([*on_host(), *cmd], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def chromium_args(profile: Path) -> list[str]:
    return [f"--app={URL}", f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check"]


def firefox_args(profile: Path) -> list[str]:
    profile.mkdir(parents=True, exist_ok=True)
    return ["--new-instance", "--profile", str(profile), URL]


def browser_command() -> list[str] | None:
    """A browser in its own window with its own profile, apart from the user's tabs, cookies and extensions."""
    native, flatpak = ROOT / "data" / "browser", Path.home() / ".var" / "app"
    if os.name == "nt":
        roots = [os.environ.get(k) for k in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")]
        found = [p for r in roots if r for rel in CHROMIUM_WINDOWS if (p := Path(r) / rel).is_file()]
        return [str(found[0]), *chromium_args(native)] if found else None
    for name in CHROMIUM:
        if host_ok("which", name):
            return [*on_host(), name, *chromium_args(native)]
    for app in CHROMIUM_FLATPAK:
        if host_ok("flatpak", "info", app):
            return [*on_host(), "flatpak", "run", app, *chromium_args(flatpak / app / "data" / "kaicho")]
    if host_ok("which", "firefox"):
        return [*on_host(), "firefox", *firefox_args(native)]
    if host_ok("flatpak", "info", "org.mozilla.firefox"):
        return [*on_host(), "flatpak", "run", "org.mozilla.firefox", *firefox_args(flatpak / "org.mozilla.firefox" / "data" / "kaicho")]
    return None


def open_browser():
    if os.environ.get("KAICHO_NO_BROWSER"):
        return
    cmd = browser_command()
    if cmd:
        window = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            if window.wait(timeout=5) == 0:     # the browser handed the window to its running instance
                return
        except subprocess.TimeoutExpired:
            return
    webbrowser.open(URL, new=1)


def stop():
    if not running():                   # an old PID could belong to another program by now
        print("Kaicho is not running.")
        PIDFILE.unlink(missing_ok=True)
        return
    try:
        pid = int(PIDFILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("Kaicho is not running.")
        return
    try:
        os.kill(pid, signal.SIGTERM)
        print("Stopped.")
    except OSError:
        print("Kaicho is not running.")
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
