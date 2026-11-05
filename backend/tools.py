"""External tools (Snapmaker Orca, Blender) and their folders on Linux and Windows.

Every lookup can be overridden: KAICHO_ORCA, KAICHO_BLENDER (executables),
KAICHO_ORCA_CONFIG (Orca's settings folder), KAICHO_CONTAINER (a distrobox
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
    if os.environ.get("KAICHO_ORCA_CONFIG"):
        return Path(os.environ["KAICHO_ORCA_CONFIG"])
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", HOME / "AppData" / "Roaming")) / "Snapmaker_Orca"
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
    box = os.environ.get("KAICHO_CONTAINER")
    if box and shutil.which("distrobox"):
        return ["distrobox", "enter", box, "--", names[0]]
    raise RuntimeError(f"Program {what} not found. Install it or set its path in {env}.")


def orca_cmd() -> list[str]:
    return _find("KAICHO_ORCA", ["snapmaker-orca", "snapmaker_orca"],
                 _program_files("Snapmaker*Orca*", "snapmaker-orca.exe") + _program_files("Snapmaker*Orca*", "*Orca*.exe"),
                 "Snapmaker Orca")


def blender_cmd() -> list[str]:
    return _find("KAICHO_BLENDER", ["blender"],
                 _program_files("Blender Foundation", "Blender*", "blender.exe"),
                 "Blender (needed for FBX)")
