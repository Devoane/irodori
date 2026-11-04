"""Slice a project with the Snapmaker Orca 2.4 CLI.

The 2.4.0 CLI segfaults on system profiles, so they are flattened first (no
`inherits`, non-empty different_settings_to_system, no wipe_tower_filament).
Settings given with --load-settings override the project, so the process
profile is overlaid with the project's own values.
"""
from __future__ import annotations

import json
import re
import subprocess
import zipfile
from collections import Counter
from pathlib import Path

from profiles import MACHINE, META_KEYS, PROCESS, resolve
from tools import orca_cmd


def _flat(sub: str, name: str, overlay: dict | None = None) -> dict:
    j = resolve(sub, name)
    j["inherits"] = ""
    j["from"] = "system"
    if not j.get("different_settings_to_system"):
        j["different_settings_to_system"] = [""]
    j.pop("wipe_tower_filament", None)
    for k, v in (overlay or {}).items():
        if k in j and k not in META_KEYS and isinstance(v, type(j[k])):
            j[k] = v
    return j


def slice_project(project: Path, out: Path, timeout: int = 3600) -> dict:
    """Slice project.3mf into out/ (plate_1.gcode + sliced.3mf). Returns a summary."""
    out.mkdir(parents=True, exist_ok=True)
    cfg = json.loads(zipfile.ZipFile(project).read("Metadata/project_settings.config"))
    (out / "machine.json").write_text(json.dumps(_flat("machine", MACHINE, {"min_layer_height": cfg["min_layer_height"]})),
                                      encoding="utf-8")
    (out / "process.json").write_text(json.dumps(_flat("process", PROCESS, cfg)), encoding="utf-8")
    fils = []
    for i, p in enumerate(cfg["filament_settings_id"]):
        f = out / f"filament{i}.json"
        f.write_text(json.dumps(_flat("filament", p)), encoding="utf-8")
        fils.append(str(f))
    cmd = orca_cmd() + ["--load-settings", f"{out}/machine.json;{out}/process.json",
                         "--load-filaments", ";".join(fils), "--slice", "0", "--outputdir", str(out),
                         "--export-3mf", "sliced.3mf", str(project)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, cwd=out)   # Orca drops debug files in cwd
    (out / "orca.log").write_text(r.stdout + r.stderr, encoding="utf-8")
    g = out / "plate_1.gcode"
    if r.returncode != 0 or not g.exists():
        msg = ""
        res = out / "result.json"
        if res.exists():
            msg = json.loads(res.read_text(encoding="utf-8")).get("error_string", "")
        log = r.stdout + r.stderr
        detail = re.search(r"message=(It seems object .*? floating regions[^,]*)", log) or \
            re.search(r"got error when validate: (.+)", log)
        raise RuntimeError(detail.group(1).strip() if detail else (msg or f"Orca zakończyła się kodem {r.returncode}"))
    text = g.read_text(encoding="utf-8", errors="ignore")
    info = dict(re.findall(r"^; (total filament change|estimated printing time \(normal mode\)|total filament used \[g\]) = (.*)$",
                           text, re.M))
    return {
        "tools": dict(sorted(Counter(re.findall(r"^T(\d+)", text, re.M)).items())),
        "changes": info.get("total filament change"),
        "time": info.get("estimated printing time (normal mode)"),
        "grams": info.get("total filament used [g]"),
    }
