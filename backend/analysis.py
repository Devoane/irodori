"""Analysis sessions (source -> printable mesh -> palette rows) and slicing jobs."""
from __future__ import annotations

import re
import shutil
import tempfile
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np

from adjust import adjust_colors, rainbow_colors
from bases import SIZES, STYLES, attach_base, preview_with_base
from calibration import load_correction
from export_3mf import PLATE, write_project
from geometry import build_print_mesh, face_adjacency_graph
from orca_cli import slice_project
from palette import (Row, Settings, Targets, build_row, diverse, load_inventory, load_library, preset_rows,
                     rank_sets)
from source import SourceModel, load_source

# interior behind the coloured band: Magenta for CMY+White, Yellow for CMY+Black
BACKING = {"cmyw": 2, "sys-cmyw": 2, "cmyk": 3, "sys-cmyk": 3}


@dataclass
class Job:
    id: str
    busy: bool = True
    message: str = ""
    error: str | None = None
    result: dict | None = None
    dir: Path | None = None


@dataclass
class Session:
    id: str
    files: dict[str, bytes]
    main: str
    settings: Settings
    src: SourceModel | None = None
    src_key: tuple | None = None
    warnings: list[str] = field(default_factory=list)
    mesh: object = None
    mesh_key: tuple | None = None
    face_rgb: np.ndarray | None = None
    area: np.ndarray | None = None
    adj: tuple | None = None
    focus: np.ndarray | None = None          # bool per print face
    target_rgb: np.ndarray | None = None     # face colours after brightness / saturation / rainbow
    face_part: np.ndarray | None = None      # Orca part per print face (painted 3MF parts, display base)
    preview_glb: bytes | None = None         # source appearance shown in the viewer
    rows: list[Row] = field(default_factory=list)
    stage: str = "queued"
    message: str = ""
    progress: float = 0.0
    diag: dict = field(default_factory=dict)
    error: str | None = None
    busy: bool = False
    version: int = 0                          # bumps when the print mesh or its colours change
    analysed_settings: Settings | None = None
    ranking: list[dict] = field(default_factory=list)   # [{"ids", "match"}] from rank_sets
    jobs: dict[str, Job] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


SESSIONS: dict[str, Session] = {}


def new_session(files: dict[str, bytes], main: str, settings: Settings) -> Session:
    s = Session(id=uuid.uuid4().hex[:12], files=files, main=main, settings=settings)
    SESSIONS[s.id] = s
    while len(SESSIONS) > 4:
        old = SESSIONS.pop(next(iter(SESSIONS)))
        for j in old.jobs.values():
            if j.dir:
                shutil.rmtree(j.dir, ignore_errors=True)
    return s


def start(s: Session, settings: Settings | None = None, focus: np.ndarray | None = None):
    if settings is not None:
        s.settings = settings
    s.focus = focus
    s.busy = True
    s.error = None
    threading.Thread(target=_run, args=(s,), daemon=True).start()


def _status(s: Session, stage: str, msg: str, progress: float | None = None):
    s.stage, s.message = stage, msg
    if progress is not None:
        s.progress = progress


def _run(s: Session):
    with s.lock:
        t0 = time.time()
        try:
            st = s.settings
            s.progress = 0.0
            src_key = (st.longest_side, st.bake_overlays)
            if s.src is None or s.src_key != src_key:
                _status(s, "load", "Wczytywanie modelu i próbkowanie tekstury…", 0.03)
                s.src, s.warnings = load_source(s.files, s.main, st.longest_side, st.bake_overlays)
                s.src_key = src_key
                s.mesh = None
            mesh_key = (src_key, st.max_triangles, st.repair, st.front_only, st.base, st.base_size)
            if s.mesh is None or s.mesh_key != mesh_key:
                _status(s, "geometry", "Przygotowanie geometrii do druku…", 0.12)
                n_before = len(s.mesh.faces) if s.mesh is not None else -1
                s.mesh, s.face_rgb = build_print_mesh(s.src, st.max_triangles, st.repair, st.front_only,
                                                      progress=lambda m: _status(s, "geometry", m))
                s.face_part = s.mesh.metadata.get("face_part") if s.src.is_exact else None
                s.preview_glb = s.src.preview_glb
                if st.base:
                    _status(s, "geometry", f"Dodawanie podstawki „{STYLES[st.base][0]}”…")
                    n_model = len(s.mesh.faces)
                    s.mesh, s.face_rgb, is_base, lift = attach_base(s.mesh, s.face_rgb, st.base, st.base_size)
                    part = s.face_part if s.face_part is not None else np.zeros(n_model, dtype=np.int64)
                    s.face_part = np.r_[part, np.full(int(is_base.sum()), part.max() + 1)]
                    s.preview_glb = preview_with_base(s.src.preview_glb, s.mesh, s.face_rgb, is_base, lift)
                s.mesh_key = mesh_key
                s.area = s.mesh.area_faces
                s.adj = face_adjacency_graph(s.mesh)
                s.version += 1
                if len(s.mesh.faces) != n_before:
                    s.focus = None
            _status(s, "targets", "Analiza kolorów modelu…", 0.35)
            s.target_rgb = target_colors(s.mesh, s.face_rgb, st)
            targets = _targets(s)
            exact = None
            if s.src.is_exact and st.toolheads == 4 and not st.front_only and not st.rainbow:
                uniq = np.unique(np.round(s.target_rgb).astype(np.int64), axis=0)
                if len(uniq) < 252 and len(uniq) + 4 <= st.max_colors:
                    exact = uniq.astype(np.float64)
            plans, s.ranking = _plans(s, st, targets)
            rows = []
            for i, (title, fil, fam, extra) in enumerate(plans):
                _status(s, "rows", f"Budowanie palety {i + 1}/{len(plans)}: {title}", 0.6 + 0.38 * i / len(plans))
                row = build_row(title, fil, targets, st, fam, correction=load_correction([f["id"] for f in fil]),
                                exact_rgb=exact, adjacency=s.adj, face_area=s.area)
                row.grouped = extra.get("grouped", 0)
                row.alternatives = extra.get("alternatives", {})
                row.cmy_brand = extra.get("cmy_brand", "")
                rows.append(row)
            search = sorted((r for r in rows if r.family == "search"), key=lambda r: -r.metrics["match"])[:st.samples]
            rows = [r for r in rows if r.family != "search"] + search
            rows.sort(key=lambda r: (r.family != "loaded", -r.metrics["match"]))
            search = [r for r in rows if r.family == "search"]
            for i, r in enumerate(search):
                r.title += f" {i + 1}"
            s.rows = rows
            s.analysed_settings = replace(st)
            s.diag = _diagnostics(s, st, time.time() - t0)
            _status(s, "done", f"Gotowe w {time.time() - t0:.1f} s", 1.0)
        except Exception as e:
            traceback.print_exc()
            s.error = str(e)
            _status(s, "error", f"Błąd: {e}")
        finally:
            s.busy = False


def target_colors(mesh, face_rgb: np.ndarray, st: Settings) -> np.ndarray:
    """The colours the palette has to match: the rainbow replaces the source,
    brightness / saturation adjust it."""
    rgb = rainbow_colors(mesh.triangles_center, st.rainbow_v, st.rainbow_h, st.rainbow_ccw, st.rainbow_offset,
                         st.rainbow_white) if st.rainbow else face_rgb
    return adjust_colors(rgb, st.brightness, st.saturation)


def _diagnostics(s: Session, st: Settings, seconds: float) -> dict:
    lo, hi = s.mesh.bounds
    size = hi - lo
    return {
        "dimensions": [round(float(x), 1) for x in size],
        "fits": bool(size[0] <= PLATE[0] and size[1] <= PLATE[1] and size[2] <= PLATE[2]),
        "plate": list(PLATE),
        "triangles": int(len(s.mesh.faces)),
        "source_triangles": int(len(s.src.faces)),
        "watertight": bool(s.mesh.is_watertight),
        "repaired": bool(s.mesh.metadata.get("repaired", False)),
        "samples": int(len(s.src.sample_points)),
        "exact_colors": int(len(np.unique(np.round(s.src.face_rgb).astype(np.int64), axis=0))) if s.src.is_exact else None,
        "base": f"{STYLES[st.base][0]} · {SIZES[st.base_size][0]}" if st.base else None,
        "source_kind": Path(s.main).suffix.lower().lstrip("."),
        "rows": len(s.rows),
        "ranked_sets": len(s.ranking),
        "seconds": round(seconds, 1),
    }


def _plans(s: Session, st: Settings, targets: Targets):
    """(title, filaments, family) rows to build, and the set ranking shown beside them."""
    _, by_id = load_library()
    inv = load_inventory()
    owned = [i for i in inv["owned"] if i in by_id] if st.source != "library" else None
    plans = []
    loaded = inv["loaded"][:st.toolheads]
    ready = st.toolheads <= 4 and len(loaded) == st.toolheads and all(i in by_id for i in loaded)
    if st.source == "loaded":
        if st.toolheads > 4:
            raise ValueError("„Założone w drukarce” działa dla 2–4 głowic (Snapmaker U1)")
        if not ready:
            raise ValueError(f"Ustaw filamenty T1–T{st.toolheads} w oknie „Moje filamenty”")
        owned = sorted(set(owned) | set(loaded))
    # the spools in the printer are always a row, next to the library proposals
    if ready:
        plans.append(("Założone w drukarce", [by_id[i] for i in loaded], "loaded", {}))
    if st.toolheads == 4:
        plans += [p for p in preset_rows(st) if owned is None or set(f["id"] for f in p[1]) <= set(owned)]
    ranking = []
    if not (st.source == "library" and st.toolheads == 4 and st.cmyk_optimized) and \
            (owned is None or len(owned) >= st.toolheads):
        _status(s, "search", "Szukanie najlepszych zestawów filamentów…", 0.4)

        def prog(m):
            k = re.match(r".*?(\d+)/(\d+)", m)
            _status(s, "search", m, 0.4 + 0.2 * int(k[1]) / int(k[2]) if k else None)

        taken = [[f["id"] for f in p[1]] for p in plans]
        ranking = rank_sets(targets, st, owned, progress=prog, seeds=taken)
        # the CMY families come on top of the requested samples; precise mode builds
        # twice as many full palettes and keeps the best (the estimate is only a guide)
        best = diverse(ranking, st.samples * (2 if st.precise else 1), st.toolheads, taken)
        label = "Z moich szpul" if owned is not None else "Propozycja"
        plans += [(label, [by_id[x] for x in r["ids"]], "search", {"grouped": r["grouped"]}) for r in best]
    if not plans:
        raise ValueError("Brak zestawów do policzenia: zaznacz posiadane filamenty w oknie „Moje filamenty”")
    return plans, ranking


def set_row(s: Session, ids: list[str], like: Row | None = None, loaded: bool = False) -> Row:
    """Full palette for one set picked from the ranking, the spools in the printer
    (`loaded`), or a preset row with another neutral filament (`like` keeps its
    title, family and alternatives)."""
    _, by_id = load_library()
    fil = [by_id[i] for i in ids]
    if like is not None:
        title, family = like.title, like.family
    elif loaded:
        title, family = "Założone w drukarce", "loaded"
    else:
        est = next((r["match"] for r in s.ranking if r["ids"] == ids), None)
        title, family = "Z rankingu" + (f" (szac. {est}%)" if est is not None else ""), "search"
    row = build_row(title, fil, _targets(s), s.analysed_settings or s.settings, family,
                    correction=load_correction(ids), adjacency=s.adj, face_area=s.area)
    if like is not None:
        row.alternatives, row.cmy_brand = like.alternatives, like.cmy_brand
    return row


def _targets(s: Session) -> Targets:
    """With a priority-brush region, scoring uses only that region; the rest keeps a
    small weight so the remaining capacity still colours it."""
    area = s.area
    rgb = s.face_rgb if s.target_rgb is None else s.target_rgb
    if s.focus is not None and s.focus.any() and len(s.focus) == len(area):
        return Targets(rgb, np.where(s.focus, area, 0.03 * area), score_weights=np.where(s.focus, area, 0.0))
    return Targets(rgb, area)


def custom_row(s: Session, base: Row, hexes: list[str]) -> Row:
    fil = []
    for f, h in zip(base.filaments, hexes):
        nf = dict(f)
        if h.upper() != f["hex"].upper():
            nf.update(hex=h.upper(), name=f"Własny {h.upper()}", brand="Własny", id=f"custom-{h.upper()}")
        fil.append(nf)
    return build_row("Moje kolory", fil, _targets(s), s.analysed_settings or s.settings, "custom",
                     correction=load_correction([f["id"] for f in fil]), adjacency=s.adj, face_area=s.area)


def backing_slot(row: Row) -> int:
    n = len(row.filaments)
    if row.family in BACKING and n == 4:
        return BACKING[row.family]
    used = np.bincount(row.labels[row.labels < n], minlength=n)
    return int(np.argmax(used)) + 1


def project_bytes(s: Session, row: Row, settings: Settings) -> bytes:
    return write_project(s.mesh, row.labels + 1, row.filaments, row.recipes, Path(s.main).stem, settings,
                         backing_slot(row), s.face_part)


def start_gcode(s: Session, row: Row, settings: Settings) -> Job:
    job = Job(id=uuid.uuid4().hex[:10], message="Zapisywanie projektu…")
    s.jobs[job.id] = job

    def run():
        try:
            job.dir = Path(tempfile.mkdtemp(prefix="irodori_gcode_"))
            project = job.dir / "project.3mf"
            project.write_bytes(project_bytes(s, row, settings))
            job.message = "Cięcie w Snapmaker Orca (może potrwać kilka minut)…"
            job.result = slice_project(project, job.dir / "out")
            job.message = "Gotowe"
        except Exception as e:
            traceback.print_exc()
            job.error = str(e)
            job.message = f"Błąd: {e}"
        finally:
            job.busy = False

    threading.Thread(target=run, daemon=True).start()
    return job
