"""Kaichō: local web server (127.0.0.1 only)."""
from __future__ import annotations

import base64
import json
import math
import re
import struct
from dataclasses import asdict, fields
from pathlib import Path
from urllib.parse import quote

import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

import analysis
from bases import SIZES, STYLES
import calibration
import calibration_card
from color import hex_to_rgb, rgb_to_hex
from export_3mf import predicted_glb
from guard import LocalOnly, csp, safe_name
from palette import Settings, load_inventory, load_library, save_inventory

ROOT = Path(__file__).resolve().parent.parent
MODEL_EXT = (".glb", ".3mf", ".fbx", ".obj")
app = FastAPI(title="Kaichō", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(LocalOnly, policy=csp(ROOT / "frontend" / "index.html"))


def _settings(raw: str | None, base: Settings | None = None) -> Settings:
    s = Settings() if base is None else Settings(**asdict(base))
    names = {f.name for f in fields(Settings)}
    values = json.loads(raw) if raw else {}
    if not isinstance(values, dict):
        raise ValueError("Invalid settings")
    for k, v in values.items():
        if k in names:
            cur = getattr(s, k)
            try:
                v = bool(v) if isinstance(cur, bool) else type(cur)(v)
            except (TypeError, ValueError, OverflowError):
                raise ValueError("Invalid settings") from None
            if isinstance(v, (int, float)) and not abs(v) <= 1e9:      # also refuses NaN and infinity
                raise ValueError("Invalid settings")
            setattr(s, k, v)
    s.toolheads = int(np.clip(s.toolheads, 2, 8))
    if s.capacity not in (0, 64, 255):
        s.capacity = s.toolheads                     # physical filaments only
    s.samples = int(np.clip(s.samples, 1, 8))
    if s.source not in ("library", "owned", "loaded"):
        s.source = "library"
    s.color_threshold = int(np.clip(s.color_threshold, 0, 10))
    s.interweave = int(np.clip(s.interweave, 0, 10))
    s.max_triangles = int(np.clip(s.max_triangles, 1000, 1_000_000))
    s.colored_walls = int(np.clip(s.colored_walls, 1, 1000))
    s.max_recipe_len = int(np.clip(s.max_recipe_len, 2, 40))
    s.max_run = int(np.clip(s.max_run, 1, 4))
    s.backing = float(np.clip(s.backing, 0.0, 0.6))
    s.longest_side = float(np.clip(s.longest_side, 0, 1000))
    s.layer_height = round(float(np.clip(s.layer_height, 0.04, 0.28)), 2)
    if not s.layer_height < s.variable_layer <= 0.28:
        s.variable_layer = 0.0
    if not 0 < s.detailed_top < s.layer_height:
        s.detailed_top = 0.0
    s.brightness = int(np.clip(s.brightness, -100, 100))
    s.saturation = int(np.clip(s.saturation, -100, 100))
    s.rainbow_v = float(np.clip(s.rainbow_v, 0, 10))
    s.rainbow_h = float(np.clip(s.rainbow_h, 0, 10))
    s.rainbow_offset = float(np.mod(s.rainbow_offset, 360))
    s.rainbow_white = float(np.clip(s.rainbow_white, 0, 75))
    s.z_offset = float(np.clip(s.z_offset, -1000, 0))
    s.rot_x, s.rot_y, s.rot_z = (float(np.mod(a, 360)) for a in (s.rot_x, s.rot_y, s.rot_z))
    if s.repair not in ("auto", "on", "off"):
        s.repair = "auto"
    if s.base not in STYLES:
        s.base = ""
    s.base_size = int(np.clip(s.base_size, 0, len(SIZES) - 1))
    if s.layer_height < 0.06:
        s.vol = False                                # too thin for a -20 % layer
    if s.vol:
        s.subdivide_mix = False
    if s.vol or s.subdivide_mix:
        s.variable_layer = s.detailed_top = 0.0
    return s


def _session(sid: str) -> analysis.Session:
    s = analysis.SESSIONS.get(sid)
    if s is None:
        raise HTTPException(404, "Session expired, load the model again")
    return s


def _row(s: analysis.Session, idx: int):
    if not (0 <= idx < len(s.rows)):
        raise HTTPException(404, "No such palette row")
    return s.rows[idx]


def _download(data: bytes, name: str, media: str) -> Response:
    name = re.sub(r'[\\/:*?"<>|]|%22', "_", name)     # browsers send a quote in a file name as %22
    quoted = quote(name)
    disposition = f'attachment; filename="{name}"' if quoted == name else f"attachment; filename*=UTF-8''{quoted}"
    return Response(data, media_type=media, headers={"Content-Disposition": disposition})


@app.get("/api/health")
def api_health():
    return {"app": "kaicho"}


@app.post("/api/analyze")
async def api_analyze(files: list[UploadFile] = File(...), settings: str = Form("{}")):
    blobs = {safe_name(f.filename): await f.read() for f in files}
    main = next((n for n in blobs if n.lower().endswith(MODEL_EXT)), None)
    if main is None:
        raise HTTPException(400, "Choose a GLB, FBX, OBJ or 3MF file")
    s = analysis.new_session(blobs, main, _settings(settings))
    analysis.start(s)
    return {"session": s.id}


@app.post("/api/reanalyze/{sid}")
async def api_reanalyze(sid: str, settings: str = Form("{}"), focus: str = Form("")):
    s = _session(sid)
    if s.busy:
        raise HTTPException(409, "Analysis in progress")
    mask = None
    if focus and s.area is not None:
        bits = np.frombuffer(base64.b64decode(focus), dtype=np.uint8)
        mask = np.unpackbits(bits, bitorder="little")[: len(s.area)].astype(bool)
    analysis.start(s, _settings(settings, s.settings), focus=mask)
    return {"session": s.id}


@app.get("/api/status/{sid}")
def api_status(sid: str):
    s = _session(sid)
    out = {"stage": s.stage, "message": s.message, "busy": s.busy, "error": s.error, "version": s.version,
           "warnings": s.warnings, "progress": round(s.progress, 3)}
    if s.stage == "done" and not s.busy:
        out["rows"] = [r.to_json() for r in s.rows]
        out["faces"] = int(len(s.mesh.faces))
        out["settings"] = asdict(s.analysed_settings)
        out["diag"] = s.diag
    return out


@app.get("/api/source/{sid}.glb")
def api_source(sid: str):
    s = _session(sid)
    if s.src is None:
        raise HTTPException(409, "The model is still loading")
    return Response(s.preview_glb or s.src.preview_glb, media_type="model/gltf-binary")


@app.get("/api/mesh/{sid}.bin")
def api_mesh(sid: str):
    """uint32 nV, nF | float32 vertices | uint32 faces | uint8 source rgb per face."""
    s = _session(sid)
    if s.mesh is None:
        raise HTTPException(409, "The geometry is still being prepared")
    v = np.ascontiguousarray(s.mesh.vertices, dtype=np.float32)
    f = np.ascontiguousarray(s.mesh.faces, dtype=np.uint32)
    c = np.ascontiguousarray(np.clip(np.round(s.face_rgb), 0, 255), dtype=np.uint8)
    return Response(struct.pack("<II", len(v), len(f)) + v.tobytes() + f.tobytes() + c.tobytes(),
                    media_type="application/octet-stream")


@app.get("/api/labels/{sid}/{idx}.bin")
def api_labels(sid: str, idx: int):
    r = _row(_session(sid), idx)
    return Response(np.ascontiguousarray(r.labels, dtype=np.uint16).tobytes(), media_type="application/octet-stream")


@app.post("/api/recolor/{sid}/{idx}")
async def api_recolor(sid: str, idx: int, colors: str = Form(...), mode: str = Form("mine")):
    """New spool colors. mode "repaint" or "match" edits a suggested row in place: repaint keeps its mixes on the
    same faces, match rebuilds them for the model. "mine" replaces the "My colors" row, and "new" adds one."""
    s = _session(sid)
    if s.busy:
        raise HTTPException(409, "Analysis in progress")
    base = _row(s, idx)
    if mode not in ("repaint", "match", "mine", "new"):
        raise HTTPException(400, "Invalid data")
    try:
        hexes = json.loads(colors)
    except ValueError:
        raise HTTPException(400, "Invalid data") from None
    if not isinstance(hexes, list) or len(hexes) != len(base.filaments):
        raise HTTPException(400, "Wrong number of colors")
    for h in hexes:
        if not isinstance(h, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", h):
            raise HTTPException(400, "Invalid data")
    if mode == "new":
        s.rows.append(analysis.custom_row(s, base, hexes))
        return {"index": len(s.rows) - 1, "row": s.rows[-1].to_json()}
    if mode == "mine":
        row = analysis.custom_row(s, base, hexes)
        row.title, row.family = base.title, base.family
    else:
        origin = base.origin or base
        if [h.upper() for h in hexes] == [f["hex"].upper() for f in origin.filaments]:
            row = origin
        else:
            row = analysis.repainted_row(s, origin, hexes) if mode == "repaint" else analysis.custom_row(s, origin, hexes)
            row.title, row.family, row.cmy_brand, row.alternatives, row.grouped, row.origin = \
                origin.title, origin.family, origin.cmy_brand, origin.alternatives, origin.grouped, origin
    s.rows[idx] = row
    return {"index": idx, "row": row.to_json()}


@app.post("/api/reset/{sid}/{idx}")
def api_reset(sid: str, idx: int):
    """Back to the row as it was suggested: its own mixes and labels."""
    s = _session(sid)
    if s.busy:
        raise HTTPException(409, "Analysis in progress")
    row = _row(s, idx)
    s.rows[idx] = row.origin or row
    return {"index": idx, "row": s.rows[idx].to_json()}


@app.get("/api/export/{sid}/{idx}.3mf")
def api_export(sid: str, idx: int, settings: str = ""):
    s = _session(sid)
    r = _row(s, idx)
    data = analysis.project_bytes(s, r, _settings(settings, s.analysed_settings or s.settings))
    return _download(data, f"{Path(s.main).stem}_kaicho_{len(r.recipes)}colors.3mf",
                     "application/vnd.ms-package.3dmanufacturing-3dmodel+xml")


@app.get("/api/export/{sid}/{idx}.glb")
def api_export_glb(sid: str, idx: int):
    s = _session(sid)
    r = _row(s, idx)
    return _download(predicted_glb(s.mesh, r.entry_rgb[r.labels]), f"{Path(s.main).stem}_predicted.glb",
                     "model/gltf-binary")


@app.post("/api/gcode/{sid}/{idx}")
def api_gcode(sid: str, idx: int, settings: str = Form("")):
    s = _session(sid)
    r = _row(s, idx)
    job = analysis.start_gcode(s, r, _settings(settings, s.analysed_settings or s.settings))
    return {"job": job.id}


@app.get("/api/gcode/{sid}/job/{jid}")
def api_gcode_status(sid: str, jid: str):
    job = _session(sid).jobs.get(jid)
    if job is None:
        raise HTTPException(404, "No such job")
    return {"busy": job.busy, "message": job.message, "error": job.error, "result": job.result}


@app.get("/api/gcode/{sid}/job/{jid}/{kind}")
def api_gcode_file(sid: str, jid: str, kind: str):
    s = _session(sid)
    job = s.jobs.get(jid)
    if job is None or job.busy or job.error:
        raise HTTPException(404, "The G-code is not ready")
    stem = Path(s.main).stem
    if kind == "gcode":
        return FileResponse(job.dir / "out" / "plate_1.gcode", filename=f"{stem}_kaicho.gcode")
    if kind == "3mf":
        return FileResponse(job.dir / "out" / "sliced.3mf", filename=f"{stem}_kaicho.gcode.3mf")
    raise HTTPException(404, "Unknown file")


@app.get("/api/library")
def api_library():
    lib, _ = load_library()
    return {"filaments": lib["filaments"], "presets": lib["presets"], "calibrations": calibration.list_calibrations(),
            "inventory": load_inventory(), "bases": [{"id": k, "name": v[0]} for k, v in STYLES.items()],
            "base_sizes": [n for n, _, _ in SIZES]}


@app.put("/api/inventory")
async def api_inventory(request: Request):
    raw = await request.json()
    try:
        custom = []
        for c in raw.get("custom", [])[:500]:
            h = rgb_to_hex(hex_to_rgb(c["hex"]))
            td = c.get("td")
            td = float(td) if td not in (None, "") else None
            if td is not None and not (math.isfinite(td) and 0 <= td <= 100):
                raise ValueError("Invalid data")
            custom.append({"id": f"custom-{h[1:]}", "brand": "Custom", "name": str(c.get("name") or h)[:40], "hex": h,
                           "td": td, "profile": str(c.get("profile") or "")[:120]})
        ids = {f["id"] for f in json.loads((ROOT / "data" / "filaments.json").read_text(encoding="utf-8"))["filaments"]}
        ids |= {c["id"] for c in custom}
        inv = {"owned": [i for i in dict.fromkeys(raw.get("owned", [])) if i in ids], "custom": custom,
               "loaded": [i for i in raw.get("loaded", []) if i in ids][:4]}
    except (AttributeError, KeyError, TypeError):
        raise ValueError("Invalid data") from None
    save_inventory(inv)
    return inv


@app.get("/api/ranking/{sid}")
def api_ranking(sid: str):
    s = _session(sid)
    _, by_id = load_library()
    return [{"ids": r["ids"], "match": r["match"], "filaments": [{k: by_id[i].get(k) for k in ("id", "brand", "name", "hex")}
                                                                  for i in r["ids"] if i in by_id]} for r in s.ranking]


@app.post("/api/set/{sid}")
async def api_set(sid: str, ids: str = Form(...), replace: int = Form(-1), loaded: int = Form(0)):
    """A full palette for a filament set. replace >= 0: swap a preset row's
    neutral filament (A/B/C) in place; with loaded=1 the row is the printer's spools."""
    s = _session(sid)
    if s.busy:
        raise HTTPException(409, "Analysis in progress")
    _, by_id = load_library()
    ids = ids.split(",")
    like = _row(s, replace) if replace >= 0 and not loaded else None
    known = set(by_id) | ({f["id"] for f in like.filaments} if like else set())
    if any(i not in known for i in ids) or len(ids) != (s.analysed_settings or s.settings).toolheads:
        raise HTTPException(400, "Unknown filament set")
    if loaded:
        row = analysis.set_row(s, ids, loaded=True)
    elif replace >= 0:
        row = analysis.set_row(s, ids, like=like)
    else:
        row = analysis.set_row(s, ids)
    if replace >= 0:
        _row(s, replace)
        s.rows[replace] = row
        return {"index": replace, "row": row.to_json()}
    s.rows.append(row)
    return {"index": len(s.rows) - 1, "row": row.to_json()}


def _library_filaments(ids: list[str]) -> list[dict]:
    _, by_id = load_library()
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise HTTPException(400, "Calibration works only for filaments from the library")
    return [by_id[i] for i in ids]


@app.get("/api/calibration/card.3mf")
def api_card(filaments: str, layer_height: float = 0.10):
    if not 0.04 <= layer_height <= 0.28:
        raise ValueError("Invalid data")
    data = calibration_card.card_3mf(_library_filaments(filaments.split(",")), layer_height)
    return _download(data, "kaicho_calibration_card.3mf", "application/vnd.ms-package.3dmanufacturing-3dmodel+xml")


@app.get("/api/calibration/layout")
def api_card_layout(filaments: str):
    return calibration_card.layout(len(_library_filaments(filaments.split(","))))


@app.post("/api/calibration/fit")
async def api_fit(filaments: str = Form(...), samples: str = Form(...)):
    """samples: measured [r, g, b] per tile, in layout order."""
    ids = filaments.split(",")
    fil = _library_filaments(ids)
    lay = calibration_card.layout(len(fil))
    try:
        meas = np.array(json.loads(samples), dtype=np.float64)
    except (TypeError, ValueError):
        raise HTTPException(400, "Wrong number of samples") from None
    if meas.shape != (len(lay["tiles"]), 3) or not np.isfinite(meas).all():
        raise HTTPException(400, "Wrong number of samples")
    fr = np.array([t["fractions"] for t in lay["tiles"]])
    nominal = np.array([hex_to_rgb(f["hex"]) for f in fil])
    pure = [next((i for i, t in enumerate(lay["tiles"]) if t["pure"] == k), -1) for k in range(len(fil))]
    coef, info = calibration.fit(fr, meas, nominal, pure)
    info["saved"] = calibration.acceptable(info)
    if info["saved"]:
        calibration.save(ids, coef, info, fr, meas)
    info["names"] = [f["name"] for f in fil]
    return info


@app.exception_handler(ValueError)
async def value_error(_, exc: ValueError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


app.mount("/", StaticFiles(directory=ROOT / "frontend", html=True), name="frontend")
