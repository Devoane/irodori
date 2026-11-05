"""External tools (Snapmaker Orca, Blender) and their folders on Linux, Windows and macOS.

Every lookup can be overridden: IRODORI_ORCA, IRODORI_BLENDER (executables),
IRODORI_ORCA_CONFIG (Orca's settings folder), IRODORI_CONTAINER (a distrobox
that has the tools, for a server started outside it).
"""
from __future__ import annotations

import glob
import os
import shutil
import sys
from pathlib import Path

HOME = Path.home()


def orca_config_dir() -> Path:
    if os.environ.get("IRODORI_ORCA_CONFIG"):
        return Path(os.environ["IRODORI_ORCA_CONFIG"])
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", HOME / "AppData" / "Roaming")) / "Snapmaker_Orca"
    if sys.platform == "darwin":
        return HOME / "Library" / "Application Support" / "Snapmaker_Orca"
    return Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "Snapmaker_Orca"


def _program_files(*pattern: str) -> list[str]:
    roots = {os.environ.get(k) for k in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)", "LOCALAPPDATA")}
    return [p for r in roots if r for p in sorted(glob.glob(os.path.join(r, *pattern)), reverse=True)]


def _find(env: str, names: list[str], candidates: list[str], what: str) -> list[str]:
    if os.environ.get(env):
        return [os.environ[env]]
    for n in names:
        if shutil.which(n):
            return [shutil.which(n)]
    for c in candidates:
        if os.path.isfile(c):
            return [c]
    box = os.environ.get("IRODORI_CONTAINER")
    if box and shutil.which("distrobox"):
        return ["distrobox", "enter", box, "--", names[0]]
    raise RuntimeError(f"Program {what} not found. Install it or set its path in {env}.")


def orca_cmd() -> list[str]:
    return _find("IRODORI_ORCA", ["snapmaker-orca", "snapmaker_orca"],
                 _program_files("Snapmaker*Orca*", "snapmaker-orca.exe") + _program_files("Snapmaker*Orca*", "*Orca*.exe")
                 + sorted(glob.glob("/Applications/Snapmaker*Orca*.app/Contents/MacOS/*")),
                 "Snapmaker Orca")


def blender_cmd() -> list[str]:
    return _find("IRODORI_BLENDER", ["blender"],
                 _program_files("Blender Foundation", "Blender*", "blender.exe")
                 + ["/Applications/Blender.app/Contents/MacOS/Blender"],
                 "Blender (needed for FBX)")
