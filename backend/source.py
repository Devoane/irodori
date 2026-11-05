"""Load coloured source models: textured GLB, FBX (via Blender), painted 3MF.

Everything becomes a Z-up, millimetre SourceModel: geometry, a dense colour
field (sample points + sRGB) and, for painted 3MF, exact per-face colours.
"""
from __future__ import annotations

import io
import json
import re
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import trimesh
from trimesh.visual.color import uv_to_interpolated_color

from color import blend_multi, hex_to_rgb
from paint import expand_painted_mesh
from tools import blender_cmd


@dataclass
class SourceModel:
    vertices: np.ndarray
    faces: np.ndarray
    sample_points: np.ndarray            # (S, 3)
    sample_rgb: np.ndarray               # (S, 3) sRGB 0..255
    preview_glb: bytes                   # source appearance for the viewer
    face_rgb: np.ndarray | None = None   # exact per-face colours of a painted 3MF
    face_part: np.ndarray | None = None  # 3MF part per face; overlapping parts must stay separate in Orca
    face_broken: np.ndarray | None = None  # body id per face of an open / overlapping 3MF body, else -1

    @property
    def is_exact(self) -> bool:
        return self.face_rgb is not None



_Y_UP_TO_Z_UP = np.array([[1, 0, 0, 0], [0, 0, -1, 0], [0, 1, 0, 0], [0, 0, 0, 1]], dtype=np.float64)


def _mesh_colors_at(mesh: trimesh.Trimesh, face_idx: np.ndarray, bary: np.ndarray) -> np.ndarray:
    """RGBA at surface points: sRGB 0..255 and alpha 0..1 (glTF alphaMode applied)."""
    vis = mesh.visual
    tri = mesh.faces[face_idx]
    mat = getattr(vis, "material", None)
    if vis.kind == "texture" and getattr(vis, "uv", None) is not None:
        img = getattr(mat, "baseColorTexture", None) or getattr(mat, "image", None)
        factor = np.ones(4)
        bcf = getattr(mat, "baseColorFactor", None)
        if bcf is not None:
            factor = np.asarray(bcf, dtype=np.float64) / (255.0 if np.max(bcf) > 1.0 else 1.0)
        if img is not None:
            uv = np.mod((vis.uv[tri] * bary[:, :, None]).sum(axis=1), 1.0)
            c = uv_to_interpolated_color(uv, img).astype(np.float64)
            rgba = np.c_[c[:, :3] * factor[:3], c[:, 3] / 255.0 * factor[3]]
        else:
            rgba = np.tile(np.r_[factor[:3] * 255.0, factor[3]], (len(face_idx), 1))
    elif vis.kind == "vertex":
        vc = vis.vertex_colors[:, :4].astype(np.float64)
        rgba = (vc[tri] * bary[:, :, None]).sum(axis=1)
        rgba[:, 3] /= 255.0
    elif vis.kind == "face":
        rgba = vis.face_colors[face_idx, :4].astype(np.float64)
        rgba[:, 3] /= 255.0
    else:
        main = getattr(mat, "main_color", None)
        col = np.asarray(main[:3] if main is not None else (200, 200, 200), dtype=np.float64)
        rgba = np.tile(np.r_[col, 1.0], (len(face_idx), 1))
    mode = getattr(mat, "alphaMode", None) or "OPAQUE"
    if mode == "OPAQUE":
        rgba[:, 3] = 1.0
    elif mode == "MASK":
        rgba[:, 3] = rgba[:, 3] >= (getattr(mat, "alphaCutoff", None) or 0.5)
    return rgba


def bake_overlays(points: np.ndarray, rgb: np.ndarray, alpha: np.ndarray, reach: float) -> np.ndarray:
    """Blend see-through layers (decals, eyes, labels on their own surface) with
    the opaque surface behind them, so both carry the combined colour.
    reach: how far (mm) an overlay may float above the surface it covers."""
    from scipy.spatial import cKDTree
    over = alpha < 0.99
    if not over.any() or over.all():
        return rgb
    out = rgb.copy()
    solid = np.nonzero(~over)[0]
    d, j = cKDTree(points[solid]).query(points[over], distance_upper_bound=reach, workers=-1)
    hit = np.isfinite(d)
    oi = np.nonzero(over)[0][hit]
    a = alpha[oi, None]
    out[oi] = a * rgb[oi] + (1 - a) * rgb[solid[j[hit]]]
    vis = np.nonzero(over & (alpha > 0.02))[0]
    if len(vis):
        d, j = cKDTree(points[vis]).query(points[solid], distance_upper_bound=reach, workers=-1)
        hit = np.isfinite(d)
        si, src = solid[hit], vis[j[hit]]
        a = alpha[src, None]
        out[si] = a * rgb[src] + (1 - a) * rgb[si]
    return out


def _sample_surface(mesh: trimesh.Trimesh, n: int, rng: np.random.Generator):
    areas = mesh.area_faces
    total = areas.sum()
    if total <= 0:
        return np.zeros((0, 3)), np.zeros((0, 4))
    # every centroid plus area-proportional random points
    nf = len(mesh.faces)
    extra = max(0, n - nf)
    idx_r = rng.choice(nf, size=extra, p=areas / total) if extra else np.zeros(0, dtype=np.int64)
    r1 = np.sqrt(rng.random(extra))
    r2 = rng.random(extra)
    bary_r = np.stack([1 - r1, r1 * (1 - r2), r1 * r2], axis=1)
    idx = np.concatenate([np.arange(nf), idx_r])
    bary = np.concatenate([np.full((nf, 3), 1 / 3), bary_r])
    tri = mesh.vertices[mesh.faces[idx]]
    pts = (tri * bary[:, :, None]).sum(axis=1)
    rgba = _mesh_colors_at(mesh, idx, bary)
    return pts, np.c_[np.clip(rgba[:, :3], 0, 255), np.clip(rgba[:, 3], 0, 1)]


AUTO_FALLBACK = 50.0
AUTO_FITS = (1.0, 256.0)


def auto_longest_side(native_mm: float) -> float:
    """Automatic size: the model's own size when it fits on the plate, otherwise 50 mm."""
    return native_mm if AUTO_FITS[0] <= native_mm <= AUTO_FITS[1] else AUTO_FALLBACK


def load_glb(data: bytes, longest_side_mm: float = 0.0, max_samples: int = 4_000_000,
             unit_mm: float = 1000.0, bake: bool = True) -> SourceModel:
    """longest_side_mm 0 = automatic. unit_mm: millimetres per model unit (glTF: metres).
    bake: blend see-through layers (see bake_overlays); off ignores transparency."""
    scene = trimesh.load(io.BytesIO(data), file_type="glb", force="scene", process=False)
    meshes = []
    for node in scene.graph.nodes_geometry:
        T, gname = scene.graph[node]
        g = scene.geometry.get(gname)
        if not isinstance(g, trimesh.Trimesh) or len(g.faces) == 0:
            continue
        m = g.copy()
        m.apply_transform(T)
        meshes.append(m)
    if not meshes:
        raise ValueError("The GLB has no triangle mesh")
    # Y-up -> Z-up, scale to longest side, drop onto the plate
    bounds = np.array([m.bounds for m in meshes])
    lo = bounds[:, 0].min(axis=0)
    hi = bounds[:, 1].max(axis=0)
    native = max(float((hi - lo).max()), 1e-9)
    if longest_side_mm <= 0:
        longest_side_mm = auto_longest_side(native * unit_mm)
    scale = longest_side_mm / native
    for m in meshes:
        m.apply_transform(_Y_UP_TO_Z_UP @ np.diag([scale, scale, scale, 1.0]))
    lo = np.min([m.bounds[0] for m in meshes], axis=0)
    hi = np.max([m.bounds[1] for m in meshes], axis=0)
    shift = np.array([-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]])
    for m in meshes:
        m.apply_translation(shift)

    rng = np.random.default_rng(1)
    total_faces = sum(len(m.faces) for m in meshes)
    n_total = int(min(max_samples, max(400_000, 2 * total_faces)))
    total_area = sum(m.area for m in meshes)
    pts, rgb = [], []
    for m in meshes:
        n = max(len(m.faces), int(n_total * m.area / max(total_area, 1e-12)))
        p, c = _sample_surface(m, n, rng)
        pts.append(p)
        rgb.append(c)
    verts = np.concatenate([m.vertices for m in meshes])
    offs = np.cumsum([0] + [len(m.vertices) for m in meshes])[:-1]
    faces = np.concatenate([m.faces + o for m, o in zip(meshes, offs)])
    pts, rgba = np.concatenate(pts), np.concatenate(rgb)
    rgb = bake_overlays(pts, rgba[:, :3], rgba[:, 3], max(0.5, 0.01 * longest_side_mm)) if bake else rgba[:, :3]
    return SourceModel(vertices=verts, faces=faces, sample_points=pts, sample_rgb=rgb,
                       preview_glb=trimesh.Scene(meshes).export(file_type="glb"))



_RE_OBJECT = re.compile(rb'<object\b([^>]*)>(.*?)</object>', re.S)
_RE_ATTR = re.compile(rb'([\w:]+)="([^"]*)"')
_RE_VERTEX = re.compile(rb'<vertex\s+x="([^"]+)"\s+y="([^"]+)"\s+z="([^"]+)"')
_RE_TRI = re.compile(rb'<triangle\b([^>]*?)/?>')
_RE_COMPONENT = re.compile(rb'<component\b([^>]*?)/?>')
_RE_ITEM = re.compile(rb'<item\b([^>]*?)/?>')


def _attrs(s: bytes) -> dict[str, str]:
    return {k.decode(): v.decode() for k, v in _RE_ATTR.findall(s)}


def _transform(s: str | None) -> np.ndarray:
    M = np.eye(4)
    if s:
        v = [float(x) for x in s.split()]
        if len(v) == 12:
            M[:3, :3] = np.array(v[:9]).reshape(3, 3).T
            M[:3, 3] = v[9:12]
    return M


def _parse_objects(xml: bytes) -> dict[str, dict]:
    objs = {}
    for head, body in _RE_OBJECT.findall(xml):
        a = _attrs(head)
        oid = a.get("id")
        entry = {"mesh": None, "components": []}
        if b"<mesh" in body:
            vs = np.array(_RE_VERTEX.findall(body), dtype=np.float64)
            tris = []
            paint = []
            for t in _RE_TRI.findall(body):
                ta = _attrs(t)
                tris.append((int(ta["v1"]), int(ta["v2"]), int(ta["v3"])))
                paint.append(ta.get("paint_color") or ta.get("slic3rpe:mmu_segmentation") or None)
            entry["mesh"] = (vs.reshape(-1, 3), np.array(tris, dtype=np.int64).reshape(-1, 3), paint)
        for c in _RE_COMPONENT.findall(body):
            ca = _attrs(c)
            entry["components"].append((ca.get("p:path"), ca.get("objectid"), _transform(ca.get("transform"))))
        objs[oid] = entry
    return objs


def _broken_bodies(fs: np.ndarray, first_id: int) -> np.ndarray:
    """Body id (from first_id) per face of one 3MF mesh for bodies Orca reports as
    non-manifold (an edge not shared by exactly two faces), -1 elsewhere."""
    e = np.sort(np.concatenate([fs[:, [0, 1]], fs[:, [1, 2]], fs[:, [2, 0]]]), axis=1)
    _, inv, cnt = np.unique(e, axis=0, return_inverse=True, return_counts=True)
    bad_face = np.zeros(len(fs), dtype=bool)
    bad_face[np.arange(len(e))[cnt[inv.ravel()] != 2] % len(fs)] = True
    if not bad_face.any():
        return np.full(len(fs), -1)
    body = trimesh.graph.connected_component_labels(trimesh.graph.face_adjacency(fs), node_count=len(fs))
    return np.where(np.isin(body, body[bad_face]), body + first_id, -1)


def _model_settings_extruders(zf: zipfile.ZipFile) -> tuple[dict[str, int], dict[str, int]]:
    obj_ext, part_ext = {}, {}
    try:
        xml = zf.read("Metadata/model_settings.config").decode("utf-8", "ignore")
    except KeyError:
        return obj_ext, part_ext
    for om in re.finditer(r'<object id="(\d+)">(.*?)</object>', xml, re.S):
        oid, body = om.group(1), om.group(2)
        head = body.split("<part", 1)[0]
        m = re.search(r'key="extruder" value="(\d+)"', head)
        if m:
            obj_ext[oid] = int(m.group(1))
        for pm in re.finditer(r'<part id="(\d+)"[^>]*>(.*?)</part>', body, re.S):
            m = re.search(r'key="extruder" value="(\d+)"', pm.group(2))
            if m:
                part_ext[f"{oid}/{pm.group(1)}"] = int(m.group(1))
    return obj_ext, part_ext


MAX_UNPACKED = 4 << 30          # a 3MF that inflates past this is refused (zip bomb)


def load_painted_3mf(data: bytes) -> SourceModel:
    zf = zipfile.ZipFile(io.BytesIO(data))
    info = zf.infolist()
    if len(info) > 20_000 or sum(i.file_size for i in info) > MAX_UNPACKED:
        raise ValueError("The 3MF is too large when unpacked")
    names = zf.namelist()
    root = zf.read("3D/3dmodel.model")
    files = {"/3D/3dmodel.model": _parse_objects(root)}
    for n in names:
        if n.startswith("3D/Objects/") and n.endswith(".model"):
            files["/" + n] = _parse_objects(zf.read(n))
    try:
        ps = json.loads(zf.read("Metadata/project_settings.config"))
    except (KeyError, ValueError):
        ps = {}
    colours = [c for c in ps.get("filament_colour", []) if c]
    if not colours:
        raise ValueError("The 3MF has no filament_colour in project_settings.config")
    palette = _state_colours(colours, ps.get("mixed_filament_definitions", ""))
    obj_ext, part_ext = _model_settings_extruders(zf)

    V, F, P, ST, PART, BROKEN = [], [], [], [], [], []
    voff = 0

    def emit(path, oid, M, default_state, top_oid):
        nonlocal voff
        obj = files.get(path, {}).get(oid)
        if obj is None:
            return
        if obj["mesh"] is not None:
            vs, fs, paint = obj["mesh"]
            V.append((np.c_[vs, np.ones(len(vs))] @ M.T)[:, :3])
            F.append(fs + voff)
            P.extend(paint)
            ST.extend([part_ext.get(f"{top_oid}/{oid}", default_state)] * len(fs))
            PART.extend([len(V) - 1] * len(fs))
            BROKEN.append(_broken_bodies(fs, voff))
            voff += len(vs)
        for cpath, coid, CM in obj["components"]:
            emit(cpath or path, coid, M @ CM, default_state, top_oid)

    for it in _RE_ITEM.findall(root):
        a = _attrs(it)
        if a.get("printable", "1") != "0":
            emit("/3D/3dmodel.model", a["objectid"], _transform(a.get("transform")), obj_ext.get(a["objectid"], 1),
                 a["objectid"])
    if not F:
        raise ValueError("The 3MF has no meshes")
    verts, faces, states, origin = expand_painted_mesh(np.concatenate(V), np.concatenate(F), P, np.array(ST))
    face_rgb = palette[np.clip(states, 1, len(palette)) - 1]
    mesh = trimesh.Trimesh(verts, faces, process=False)
    mesh.merge_vertices(digits_vertex=5)
    lo, hi = mesh.bounds
    mesh.apply_translation([-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]])
    pv = trimesh.Trimesh(mesh.vertices, mesh.faces, process=False)
    pv.visual.face_colors = np.c_[face_rgb, np.full(len(face_rgb), 255)].astype(np.uint8)
    return SourceModel(vertices=mesh.vertices, faces=mesh.faces, sample_points=mesh.triangles_center,
                       sample_rgb=face_rgb, preview_glb=trimesh.Scene(pv).export(file_type="glb"), face_rgb=face_rgb,
                       face_part=np.asarray(PART)[origin] if len(V) > 1 else None,
                       face_broken=np.concatenate(BROKEN)[origin])


def _state_colours(colours: list[str], mixed: str) -> np.ndarray:
    """sRGB per paint state: physical filaments, then the project's enabled mixes
    (manual pattern, or component a/b with mix_b_percent)."""
    phys = np.array([hex_to_rgb(c) for c in colours])
    out = list(phys)
    for row in filter(None, mixed.split(";")):
        tok = row.split(",")
        try:
            a, b, enabled, _, mix_b = (int(x) for x in tok[:5])
        except ValueError:
            continue
        if not enabled or "d1" in tok[5:]:
            continue
        rest = tok[5:]
        if rest and rest[0] in ("0", "1"):          # pointillism flag
            rest = rest[1:]
        tokens = [int(m[0] or m[1]) for g in rest if g[:1].isdigit() or g[:1] == "["
                  for m in re.findall(r"\[(\d+)\]|(\d)", g)]
        ids = [a if d == 1 else b if d == 2 else d for d in tokens] if tokens else [a] * (100 - mix_b) + [b] * mix_b
        ids = [i for i in ids if 1 <= i <= len(phys)]
        if not ids:
            continue
        slots = sorted(set(ids))
        out.append(blend_multi(phys[[s - 1 for s in slots]], [ids.count(s) for s in slots]))
    return np.array(out)



_BLENDER_FBX_TO_GLB = """
import bpy, sys
src, out, folder = sys.argv[-3:]
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.fbx(filepath=src)
bpy.ops.file.find_missing_files(directory=folder)
missing = sorted({i.filepath for i in bpy.data.images if i.source == 'FILE' and not i.has_data and not i.packed_file})
print('FSD_MISSING=' + '|'.join(missing))
bpy.ops.export_scene.gltf(filepath=out, export_format='GLB')
"""


def fbx_to_glb(files: dict[str, bytes], main: str) -> tuple[bytes, list[str]]:
    """Convert an FBX (plus any texture files sent with it) to GLB with Blender.
    Returns (glb, texture paths Blender could not resolve)."""
    with tempfile.TemporaryDirectory(prefix="irodori_fbx_") as tmp:
        for name, data in files.items():
            (Path(tmp) / Path(name).name).write_bytes(data)
        script = Path(tmp) / "convert.py"
        script.write_text(_BLENDER_FBX_TO_GLB, encoding="utf-8")
        out = Path(tmp) / "out.glb"
        r = subprocess.run(blender_cmd() + ["-b", "--factory-startup", "--disable-autoexec", "--python", str(script), "--",
                                             str(Path(tmp) / Path(main).name), str(out), tmp],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
        if not out.exists():
            tail = (r.stdout + r.stderr).strip().splitlines()[-5:]
            raise ValueError("Blender could not convert the FBX: " + " / ".join(tail))
        missing = []
        for line in r.stdout.splitlines():
            if line.startswith("FSD_MISSING="):
                missing = [m for m in line.split("=", 1)[1].split("|") if m]
        return out.read_bytes(), missing


_RE_MTL_FILE = re.compile(r"^\s*(?:map_\w+|bump|disp|decal|refl)\s+(?:-\S+\s+\S+\s+)*(.+?)\s*$", re.M)


def obj_to_glb(files: dict[str, bytes], main: str) -> tuple[bytes, list[str]]:
    """OBJ with its MTL libraries and textures -> GLB. Returns (glb, missing files).
    Files are matched by name, so textures may come from any folder."""
    missing = []
    with tempfile.TemporaryDirectory(prefix="irodori_obj_") as tmp:
        for name, data in files.items():
            (Path(tmp) / Path(name).name).write_bytes(data)
        text = files[main].decode("utf-8", "ignore")
        for lib in re.findall(r"^\s*mtllib\s+(.+?)\s*$", text, re.M):
            mtl = Path(tmp) / Path(lib.replace("\\", "/")).name
            if not mtl.exists():
                missing.append(Path(lib).name)
                continue
            body = mtl.read_text(encoding="utf-8", errors="ignore")
            for ref in _RE_MTL_FILE.findall(body):
                ref = re.sub(r"^(?:[-\d.]+\s+)+", "", ref)      # values of 3-argument options like -s 1 1 1
                name = Path(ref.replace("\\", "/")).name
                if not (Path(tmp) / name).exists():
                    missing.append(name)
                body = body.replace(ref, name)          # textures sit next to the MTL here
            mtl.write_text(body, encoding="utf-8")
        scene = trimesh.load(Path(tmp) / Path(main).name, force="scene", process=False)
        if not scene.geometry:
            raise ValueError("The OBJ has no triangle mesh")
        return scene.export(file_type="glb"), sorted(set(missing))


def load_source(files: dict[str, bytes], main: str, longest_side_mm: float = 0.0,
                bake: bool = True) -> tuple[SourceModel, list[str]]:
    """Returns (model, warnings). longest_side_mm 0 = automatic."""
    fn = main.lower()
    if fn.endswith(".glb"):
        return load_glb(files[main], longest_side_mm, bake=bake), []
    if fn.endswith(".3mf"):
        return load_painted_3mf(files[main]), []
    if fn.endswith(".fbx"):
        glb, missing = fbx_to_glb(files, main)
        warn = [f"Missing texture: {Path(m).name}" for m in missing]
        return load_glb(glb, longest_side_mm, bake=bake), warn
    if fn.endswith(".obj"):
        glb, missing = obj_to_glb(files, main)
        # OBJ has no unit; most exporters write millimetres, Auto falls back to 50 mm when it does not fit
        return load_glb(glb, longest_side_mm, unit_mm=1.0, bake=bake), [f"Brak pliku: {m}" for m in missing]
    raise ValueError("Supported formats: GLB, FBX (with textures), OBJ (with MTL and textures), painted 3MF (Bambu/Orca)")
