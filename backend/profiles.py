"""Snapmaker Orca system profiles (inherits chains resolved) and U1 project settings."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from tools import orca_config_dir

SYSTEM_DIR = orca_config_dir() / "system" / "Snapmaker"
TEMPLATE = Path(__file__).resolve().parent / "templates" / "u1_project_settings_2.4.0.json"

MACHINE = "Snapmaker U1 (0.4 nozzle)"
PROCESS = "0.10mm Color Mixing @Snapmaker U1 (0.4 nozzle)"
GENERIC_FILAMENT = "Generic PLA"

# preset identity, never copied between presets/projects
META_KEYS = {"type", "name", "inherits", "from", "setting_id", "instantiation", "compatible_printers",
             "compatible_printers_condition", "compatible_prints", "compatible_prints_condition", "description",
             "filament_id", "is_custom_defined", "version", "renamed_from", "different_settings_to_system",
             "inherits_group", "print_settings_id", "printer_settings_id"}

# per-filament arrays in project_settings.config without a "filament_" prefix
_FILAMENT_ARRAY_KEYS = {
    "nozzle_temperature", "nozzle_temperature_initial_layer", "nozzle_temperature_range_low",
    "nozzle_temperature_range_high", "hot_plate_temp", "hot_plate_temp_initial_layer", "cool_plate_temp",
    "cool_plate_temp_initial_layer", "eng_plate_temp", "eng_plate_temp_initial_layer", "textured_plate_temp",
    "textured_plate_temp_initial_layer", "pressure_advance", "enable_pressure_advance", "fan_max_speed",
    "fan_min_speed", "slow_down_layer_time", "slow_down_min_speed", "overhang_fan_speed", "temperature_vitrification",
    "close_fan_the_first_x_layers", "full_fan_speed_layer", "additional_cooling_fan_speed", "reduce_fan_stop_start_freq",
    "fan_cooling_layer_time", "idle_temperature", "chamber_temperature", "default_filament_colour",
}


@lru_cache(maxsize=1)
def _index() -> dict:
    idx = {}
    for sub in ("machine", "process", "filament"):
        for f in (SYSTEM_DIR / sub).glob("*.json"):
            try:
                j = json.loads(f.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if "name" in j:
                idx[(sub, j["name"])] = j
    return idx


@lru_cache(maxsize=256)
def resolve(sub: str, name: str) -> dict:
    j = _index().get((sub, name))
    if j is None:
        return {}
    return (resolve(sub, j["inherits"]) if j.get("inherits") else {}) | j


def project_settings(filament_profiles: list[str], filament_colours: list[str], layer_height: float,
                     wall_loops: int) -> dict:
    """Complete project_settings.config for a U1 project: an Orca 2.4 template
    overlaid with the Color Mixing process and each filament's profile."""
    cfg = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    n = len(filament_profiles)
    for k, v in resolve("process", PROCESS).items():
        if k not in META_KEYS and k in cfg:
            cfg[k] = v
    fil_cfgs = [resolve("filament", p) or resolve("filament", GENERIC_FILAMENT) for p in filament_profiles]
    tmpl_n = len(cfg["filament_settings_id"])
    for k, v in list(cfg.items()):
        if not (isinstance(v, list) and len(v) == tmpl_n):
            continue
        if k.startswith("filament_") or k in _FILAMENT_ARRAY_KEYS or any(k in fc for fc in fil_cfgs):
            vals = []
            for fc in fil_cfgs:
                fv = fc.get(k, v[0])
                vals.append(fv[0] if isinstance(fv, list) and fv else fv)   # first extruder variant
            cfg[k] = vals
    cfg.update({
        "filament_settings_id": list(filament_profiles),
        "filament_colour": list(filament_colours),
        "default_filament_colour": [""] * n,
        "different_settings_to_system": [""] * (n + 2),
        "inherits_group": [""] * (n + 2),
        "print_settings_id": PROCESS,
        "printer_settings_id": MACHINE,
        "layer_height": f"{layer_height:g}",
        "initial_layer_print_height": f"{max(layer_height, 0.1):g}",
        "wall_loops": str(int(wall_loops)),
    })
    return cfg


def mark_changes(cfg: dict, written: set[str], filament_keys: tuple[str, ...]) -> None:
    """Fill different_settings_to_system. Orca's GUI keeps only the listed keys from a project and takes every
    other setting from the named system preset, so an empty list silently drops layer height, supports and tower.
    The CLI ignores the list, which is why it hides this. `written` holds the keys the export changed itself."""
    process, machine = resolve("process", PROCESS), resolve("machine", MACHINE)
    plain = lambda v: [str(x) for x in v] if isinstance(v, list) else str(v)   # noqa: E731
    skip = META_KEYS | set(machine) | {"wipe_tower_x", "wipe_tower_y", "mixed_filament_definitions"}
    keys = {k for k in process if k in cfg and plain(cfg[k]) != plain(process[k])} | written
    print_keys = sorted(k for k in keys - skip if not k.startswith("filament_"))
    printer_keys = ["min_layer_height"] if plain(cfg["min_layer_height"]) != plain(machine.get("min_layer_height")) else []
    cfg["different_settings_to_system"] = [";".join(print_keys), *[";".join(filament_keys)] * len(cfg["filament_settings_id"]),
                                           ";".join(printer_keys)]
