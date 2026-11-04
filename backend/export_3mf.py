"""Write a Snapmaker Orca 2.4 project (.3mf) for the Snapmaker U1.

Same layout Orca 2.4 writes: an assembly object with one component pointing to
a mesh in 3D/Objects/, painted with `paint_color`; mixed filaments live in
project_settings.config -> mixed_filament_definitions.
"""
from __future__ import annotations

import io
import json
import uuid
import zipfile

import numpy as np
import trimesh
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from paint import encode_state, encode_tree, subdivide_states
from profiles import project_settings
from color import hex_to_rgb, linear_to_srgb, rgb_to_oklab, srgb_to_linear
from recipes import Recipe, _interleave, predict_many

BED_CENTER = (135.0, 135.0)
PLATE = (270.0, 270.0, 270.0)
LINE_WIDTH = 0.42
DUAL_LINE_WIDTH = 0.30
MIN_LAYER = 0.04
ROOF_MIN_AREA = 25.0  # mm² of flat roof at one height before that band gets thin roof layers

_BAYER8 = np.array([[0, 32, 8, 40, 2, 34, 10, 42], [48, 16, 56, 24, 50, 18, 58, 26],
                    [12, 44, 4, 36, 14, 46, 6, 38], [60, 28, 52, 20, 62, 30, 54, 22],
                    [3, 35, 11, 43, 1, 33, 9, 41], [51, 19, 59, 27, 49, 17, 57, 25],
                    [15, 47, 7, 39, 13, 45, 5, 37], [63, 31, 55, 23, 61, 29, 53, 21]])


def pattern_token(slot: int) -> str:
    return str(slot) if slot <= 9 else f"[{slot}]"


def mixed_definition_row(recipe: Recipe, stable_id: int) -> str:
    """One `mixed_filament_definitions` row in manual-pattern (cycle) mode.

    With component_a = 1 and component_b = 2 every pattern token is a literal
    physical slot. Comma-separated groups address successive perimeters:
    group 0 = outer wall, the last group repeats for the walls behind it.
    """
    groups = recipe.groups()
    counts: dict[int, int] = {}
    for g in groups:
        for s in g:
            counts[s] = counts.get(s, 0) + 1
    ids = sorted(counts)
    total = sum(counts.values())
    weights = [round(100 * counts[s] / total) for s in ids]
    weights[0] += 100 - sum(weights)
    pattern = ",".join("".join(pattern_token(s) for s in g) for g in groups)
    mix_b = int(round(100 * np.mean([g.count(2) / len(g) for g in groups])))  # as Orca derives it
    return (f"1,2,1,1,{mix_b},0,g{''.join(pattern_token(s) for s in ids)},w{'/'.join(map(str, weights))},"
            f"m2,z0,xa0,xb0,d0,o0,u{stable_id},{pattern}")


def vol_schedule(base: float, height: float, first: float) -> list[tuple[float, float]]:
    """Variable Optical Layers: a frozen 40-layer cycle of 12 low, 16 base and
    12 high layers (+-20 %), averaging the base height."""
    lo, hi = max(0.04, base * 0.8), min(0.32, base * 1.2)
    cycle = _interleave({1: 12, 2: 16, 3: 12}, 40)
    h_of = {1: lo, 2: base, 3: hi}
    out = [(0.0, first)]
    z = first
    i = 0
    while z < height - 1e-3:
        h = h_of[cycle[i % 40]]
        out.append((round(z, 5), h))
        z += h
        i += 1
    return out


def first_layer(layer_height: float) -> float:
    """A 0.20 mm first layer under fine layers (up to 0.16 mm) for bed adhesion."""
    return 0.2 if layer_height <= 0.16 + 1e-9 else layer_height


def adaptive_schedule(mesh: trimesh.Trimesh, face_state: np.ndarray, recipes: list[Recipe], base: float, first: float,
                      coarse: float = 0.0, fine: float = 0.0, top_depth: float = 0.6,
                      res: float = 0.005) -> list[tuple[float, float]] | None:
    """Thicker layers (`coarse`) where no face is mixed and thinner ones (`fine`)
    through the top shell under flat roofs. Orca layers are
    global per object, so a height applies to the whole Z band. None = uniform."""
    if not (coarse > base or 0 < fine < base):
        return None
    H = float(mesh.bounds[1][2])
    N = int(np.ceil(H / res)) + 2
    tz = mesh.triangles[:, :, 2]
    zmin, zmax = tz.min(axis=1), tz.max(axis=1)

    def band(sel, lo, hi):
        d = np.zeros(N + 1)
        np.add.at(d, np.clip((lo[sel] / res).astype(int), 0, N), 1)
        np.add.at(d, np.clip((hi[sel] / res).astype(int) + 1, 0, N), -1)
        return np.cumsum(d)[:N] > 0

    mixed = np.array([len(set(r.word) | set(r.inner or ())) > 1 for r in recipes])[np.asarray(face_state) - 1]
    need = np.where(band(mixed, zmin, zmax), 1, 0) if coarse > base else np.ones(N, dtype=int)
    if 0 < fine < base:
        # flat roofs only (within ~10 deg), and only heights where they add up to a real surface:
        # a stray near-flat facet on a curved model would force fine layers through its whole Z band
        roof = mesh.face_normals[:, 2] > 0.985
        level = np.round(zmax / 0.5).astype(np.int64)
        area = np.bincount(np.where(roof, level, 0), weights=np.where(roof, mesh.area_faces, 0))
        roof &= area[level] >= ROOF_MIN_AREA
        need = np.where(band(roof, zmax - top_depth, zmax), 2, need)
    heights = np.array([coarse if coarse > base else base, base, fine if 0 < fine < base else base])
    out = [(0.0, first)]
    z = first
    while z < H - 1e-3:
        i = min(int(z / res), N - 1)
        h = heights[need[i]]
        ahead = heights[need[i:min(N, int((z + h) / res) + 1)]]
        if ahead.min() < h:                 # do not run into a band that needs thinner layers
            dist = int(np.argmax(ahead < h)) * res
            h = round(dist, 3) if dist >= MIN_LAYER else float(ahead.min())
        h = max(MIN_LAYER, min(h, H - z)) if H - z >= MIN_LAYER else h
        out.append((round(z, 5), round(float(h), 4)))
        z += h
    if all(abs(h - base) < 1e-6 for _, h in out[1:]):
        return None
    return out


def _profile_points(prof: list[tuple[float, float]], top: float) -> str:
    """layer_heights_profile.txt. Orca interpolates between points, so both ends
    of every layer are pinned; the last point must sit exactly on the object top
    or the profile is dropped."""
    pts: list[tuple[float, float]] = []
    for zz, h in prof:
        for pz in (zz, min(zz + h - 0.001, top - 0.001)):
            if not pts or pz > pts[-1][0] + 1e-4:
                pts.append((pz, h))
    if top > pts[-1][0] + 1e-4:
        pts.append((top, prof[-1][1]))
    else:
        pts[-1] = (top, pts[-1][1])
    return "object_id=1|" + ";".join(f"{zz:.5f};{h:.4f}" for zz, h in pts) + "\n"


def place_mesh(mesh: trimesh.Trimesh, settings) -> tuple[trimesh.Trimesh, float]:
    """Print pose: optional rotation about the printer axes (X, then Y, then Z),
    centred, bottom on the plate. Returns (mesh, z_offset to cut at)."""
    mesh = mesh.copy()
    if settings.rotate and (settings.rot_x or settings.rot_y or settings.rot_z):
        mesh.apply_transform(trimesh.transformations.euler_matrix(
            *np.radians([settings.rot_x, settings.rot_y, settings.rot_z]), "sxyz"))
    if mesh.is_volume and mesh.volume < 0:
        mesh.invert()
    lo, hi = mesh.bounds
    mesh.apply_translation([-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]])
    dz = min(0.0, float(settings.z_offset))
    if dz <= -mesh.bounds[1][2]:
        raise ValueError("Przesunięcie Z chowa cały model pod stołem")
    return mesh, dz


TOWER_SIZE = (60.0, 90.0)   # mm incl. brim: generous, the tower grows with the number of mixes
TOWER_OFFSET = 10.0         # the tower's brim starts this far before wipe_tower_x / _y


def tower_spot(mesh: trimesh.Trimesh, bed: float = 270.0, margin: float = 5.0) -> tuple[float, float]:
    """wipe_tower_x/_y in a bed corner clear of the model, so the tower can grow into the
    bed instead of past its edge. Orca anchors the tower at its front-left corner."""
    lo, hi = mesh.bounds[:, :2] + np.array(BED_CENTER)
    w, d = TOWER_SIZE
    lo_x, hi_x = TOWER_OFFSET + margin, bed - margin - w + TOWER_OFFSET
    lo_y, hi_y = TOWER_OFFSET + margin, bed - margin - d + TOWER_OFFSET
    best, best_overlap = None, np.inf
    for x, y in ((lo_x, lo_y), (hi_x, lo_y), (lo_x, hi_y), (hi_x, hi_y)):
        a = np.array([x - TOWER_OFFSET, y - TOWER_OFFSET]) - margin
        b = a + np.array([w, d]) + 2 * margin
        overlap = float(np.prod(np.clip(np.minimum(b, hi) - np.maximum(a, lo), 0, None)))
        if overlap < best_overlap:
            best, best_overlap = (x, y), overlap
    return best


def cut_at_plate(mesh: trimesh.Trimesh, dz: float, face_state: np.ndarray, face_part: np.ndarray | None):
    """Lower the model by -dz and cut off what is below the plate (Orca's CLI puts
    sunk objects back on the bed). Each part is cut on its own and stays closed;
    faces keep the state of the nearest original face."""
    import manifold3d
    from scipy.spatial import cKDTree
    m = mesh.copy()
    m.apply_translation([0, 0, dz])
    parts = np.zeros(len(m.faces), dtype=np.int64) if face_part is None else np.asarray(face_part)
    pieces, part_of = [], []
    for p in np.unique(parts):
        sub = m.submesh([np.nonzero(parts == p)[0]], append=True)
        if sub.bounds[1][2] <= 0:
            continue
        mf = manifold3d.Manifold(manifold3d.Mesh(vert_properties=np.asarray(sub.vertices, np.float32),
                                                 tri_verts=np.asarray(sub.faces, np.uint32)))
        if mf.status() != manifold3d.Error.NoError or mf.is_empty():
            raise ValueError("Przesunięcie Z potrzebuje szczelnej siatki: włącz pogrubianie cienkich elementów")
        cut = mf.trim_by_plane([0, 0, 1], 0.0).to_mesh()
        pieces.append(trimesh.Trimesh(np.asarray(cut.vert_properties)[:, :3].astype(np.float64), cut.tri_verts, process=False))
        part_of.append(np.full(len(cut.tri_verts), p))
    out = trimesh.util.concatenate(pieces)
    _, idx = cKDTree(m.triangles_center).query(out.triangles_center, workers=-1)
    return out, np.asarray(face_state)[idx], None if face_part is None else np.concatenate(part_of)


WEAVE_NOISE = 0.5    # cost of the dots' contrast: sqrt(t(1-t)) * OKLab gap between the two spools
WEAVE_MAX_ERR = 0.08  # OKLab: a roof the weave cannot match keeps the layer mix
WEAVE_MAX_NOISE = 0.2  # dots more contrasting than red/yellow orange read as confetti


def _weave_plans(recipes: list[Recipe], phys_rgb: np.ndarray) -> list[tuple[list[int], np.ndarray] | None]:
    """(slots, cumulative shares) of the roof pattern for each recipe: one or two spools
    whose side-by-side mix (averaged like light, not like layers) looks closest to the
    recipe's colour on the walls, with high-contrast dots penalised. None = no weave."""
    n = len(phys_rgb)
    lin, phys_ok = srgb_to_linear(phys_rgb), rgb_to_oklab(phys_rgb)
    opts = [([a], np.array([1.0]), lin[a], 0.0) for a in range(n)]
    for a in range(n):
        for b in range(a + 1, n):
            gap = float(np.linalg.norm(phys_ok[a] - phys_ok[b]))
            for k in range(1, 16):
                t = k / 16
                opts.append(([a, b], np.array([1 - t, 1.0]), (1 - t) * lin[a] + t * lin[b], np.sqrt(t * (1 - t)) * gap))
    opt_ok = rgb_to_oklab(linear_to_srgb(np.array([o[2] for o in opts])))
    noise = np.array([o[3] for o in opts])
    counts = np.zeros((len(recipes), n))
    for i, r in enumerate(recipes):
        for slot, c in r.counts.items():
            counts[i, slot - 1] = c
    want = rgb_to_oklab(predict_many(counts, phys_rgb))
    err = np.linalg.norm(want[:, None, :] - opt_ok[None, :, :], axis=-1)
    cost = np.where(noise[None, :] <= WEAVE_MAX_NOISE, err + WEAVE_NOISE * noise[None, :], np.inf)
    best = np.argmin(cost, axis=1)
    return [([x + 1 for x in opts[j][0]], opts[j][1]) if err[i, j] <= WEAVE_MAX_ERR else None
            for i, j in enumerate(best)]


def _flat_roofs(mesh: trimesh.Trimesh) -> np.ndarray:
    """Faces of real flat roofs: connected near-horizontal regions (within ~10 deg) of at
    least ROOF_MIN_AREA that stay within 0.15 mm of one height. The near-flat cap of a
    dome or a rounded paw is not one; a weave there is an island in the layer mix."""
    up = mesh.face_normals[:, 2] > 0.985
    adj = mesh.face_adjacency
    adj = adj[up[adj[:, 0]] & up[adj[:, 1]]]
    n = len(mesh.faces)
    _, comp = connected_components(coo_matrix((np.ones(len(adj)), (adj[:, 0], adj[:, 1])), shape=(n, n)), directed=False)
    z = mesh.triangles[:, :, 2]
    k = np.where(up, comp, -1)
    sel = k >= 0
    area = np.bincount(k[sel], weights=mesh.area_faces[sel], minlength=comp.max() + 1)
    lo = np.full(comp.max() + 1, np.inf)
    hi = np.full(comp.max() + 1, -np.inf)
    np.minimum.at(lo, k[sel], z[sel].min(1))
    np.maximum.at(hi, k[sel], z[sel].max(1))
    good = (area >= ROOF_MIN_AREA) & (hi - lo <= 0.15)
    return sel & good[np.maximum(k, 0)]


def _interweave_trees(mesh: trimesh.Trimesh, face_state: np.ndarray, recipes: list[Recipe], phys_rgb: np.ndarray,
                      level: int, default_state: int) -> dict[int, str]:
    """Roof weave: flat roofs of mixed colours painted with an ordered-dither XY pattern
    of one or two spools (cell 1.8 mm at level 1, 0.9 mm at 10)."""
    cell = 0.9 + 0.1 * (10 - level)
    n_phys = len(phys_rgb)
    roof = _flat_roofs(mesh) & (face_state > n_phys)
    roof = np.nonzero(roof)[0]
    plans = _weave_plans(recipes, phys_rgb) if len(roof) else []
    out = {}
    for f in roof:
        plan = plans[face_state[f] - 1]
        if plan is None:
            continue
        slots, cum = plan
        if len(slots) == 1:
            out[int(f)] = encode_tree(slots[0], default_state)
            continue

        def state_at(p, slots=slots, cum=cum):
            rank = _BAYER8[int(np.floor(p[0] / cell)) % 8, int(np.floor(p[1] / cell)) % 8]
            return slots[int(np.searchsorted(cum, (rank + 0.5) / 64))]

        a, b, c = mesh.vertices[mesh.faces[f]]
        out[int(f)] = encode_tree(subdivide_states(a, b, c, state_at, cell / 2), default_state)
    return out


def _object_xml(oid: int, mesh: trimesh.Trimesh, states: np.ndarray, default_state: int, trees: dict[int, str]) -> str:
    verts = "\n".join(f'     <vertex x="{x:.9g}" y="{y:.9g}" z="{z:.9g}"/>' for x, y, z in mesh.vertices)
    codes = {}
    tris = []
    for i, ((a, b, c), st) in enumerate(zip(mesh.faces, states)):
        st = int(st)
        code = trees.get(i)
        if code is None and st != default_state:
            code = codes.get(st) or codes.setdefault(st, encode_state(st))
        attr = f' paint_color="{code}"' if code else ""
        tris.append(f'     <triangle v1="{a}" v2="{b}" v3="{c}"{attr}/>')
    return (f'  <object id="{oid}" p:UUID="{uuid.uuid4()}" type="model">\n   <mesh>\n    <vertices>\n{verts}\n'
            '    </vertices>\n    <triangles>\n' + "\n".join(tris) + '\n    </triangles>\n   </mesh>\n  </object>\n')


def _objects_xml(objects: list[str]) -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
            'xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" '
            'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" requiredextensions="p">\n'
            ' <metadata name="BambuStudio:3mfVersion">1</metadata>\n <resources>\n' + "".join(objects) +
            ' </resources>\n</model>\n')


def write_project(mesh: trimesh.Trimesh, face_state: np.ndarray, filaments: list[dict], recipes: list[Recipe],
                  name: str, settings, backing_slot: int = 1, face_part: np.ndarray | None = None) -> bytes:
    """face_state: 1-based filament id per face (physical 1..n, virtual n+1..).
    backing_slot: object filament, used behind the coloured band when
    colour does not run through the interior.
    face_part: source part per face. Each part is written as its own Orca part
    whose filament is the one most of its faces use, so overlapping parts
    (e.g. layered lithophanes) keep their colours instead of merging into one shell."""
    mesh, dz = place_mesh(mesh, settings)
    if dz < 0:
        mesh, face_state, face_part = cut_at_plate(mesh, dz, face_state, face_part)
    n = len(filaments)
    if n > 4:
        raise ValueError("Snapmaker U1 ma 4 głowice: projekt 3MF da się zapisać dla 2–4 filamentów")
    if len(recipes) > 255:
        raise ValueError("za dużo kolorów (limit Orca: 255)")
    face_state = np.asarray(face_state)
    phys_rgb = np.array([hex_to_rgb(f["hex"]) for f in filaments], dtype=np.float64)
    dual = any(r.inner is not None for r in recipes[n:])
    walls = int(settings.colored_walls)

    cfg = project_settings([f.get("profile") or "Generic PLA" for f in filaments],
                           [f["hex"] for f in filaments], settings.layer_height, walls)
    tx, ty = tower_spot(mesh)
    cfg["wipe_tower_x"], cfg["wipe_tower_y"] = [f"{tx:g}"], [f"{ty:g}"]
    first = first_layer(settings.layer_height)
    cfg["initial_layer_print_height"] = f"{first:g}"
    top = float(mesh.bounds[1][2])
    if settings.vol:
        prof = vol_schedule(settings.layer_height, top, first)
    elif settings.subdivide_mix:
        prof = None
    else:
        top_depth = max(float(cfg.get("top_shell_thickness") or 0), int(cfg.get("top_shell_layers") or 0) * settings.layer_height)
        prof = adaptive_schedule(mesh, face_state, recipes, settings.layer_height, first,
                                 settings.variable_layer, settings.detailed_top, top_depth)
    lowest = min([settings.layer_height] + [h for _, h in (prof or [])[1:]])
    if lowest < 0.08 - 1e-9:
        cfg["min_layer_height"] = [f"{MIN_LAYER:g}"] * len(cfg["min_layer_height"])
    cfg["mixed_filament_definitions"] = ";".join(mixed_definition_row(r, 1000 + i) for i, r in enumerate(recipes[n:]))
    cfg["dithering_step_painted_zones_only"] = "1"
    cfg["enable_support"] = "1" if settings.supports else "0"
    if settings.supports:
        cfg["support_type"] = "tree(auto)"
        # an explicit support filament: with "default" (0), dual-wall patterns crash Orca's wipe tower priming
        cfg["support_filament"] = cfg["support_interface_filament"] = str(backing_slot)
        if prof:
            cfg["support_style"] = "tree_hybrid"   # organic trees refuse variable layer heights
    width = DUAL_LINE_WIDTH if dual else LINE_WIDTH
    if dual:
        cfg["outer_wall_line_width"] = cfg["inner_wall_line_width"] = f"{DUAL_LINE_WIDTH:g}"
    # colour through the whole section (0 = unlimited);
    # otherwise only the requested wall band is coloured and the backing filament fills the rest
    cfg["mmu_segmented_region_max_width"] = "0" if settings.take_interior else f"{walls * width + 0.05:.2f}"
    if settings.interweave > 0:
        cfg["top_surface_line_width"] = "0.3"
        cfg["only_one_wall_top"] = "1"
    if settings.subdivide_mix and not settings.vol:
        cfg["dithering_local_z_mode"] = "1"
        cfg["dithering_local_z_whole_objects"] = "1"
        cfg["dithering_local_z_infill"] = "0"

    safe = "".join(ch for ch in name if ch.isalnum() or ch in "-_ ") or "model"
    obj_path = f"3D/Objects/{safe.replace(' ', '_')}_1.model"
    if face_part is None or len(np.unique(face_part)) < 2:
        face_part = np.zeros(len(mesh.faces), dtype=np.int64)
    objects, components, parts_cfg = [], [], []
    for k, part in enumerate(np.unique(face_part), start=1):
        sel = np.nonzero(face_part == part)[0]
        used, inv = np.unique(mesh.faces[sel], return_inverse=True)
        sub = trimesh.Trimesh(mesh.vertices[used], inv.reshape(-1, 3), process=False)
        states = face_state[sel]
        default = backing_slot if k == 1 and len(sel) == len(mesh.faces) else int(np.bincount(states).argmax())
        trees = _interweave_trees(sub, states, recipes, phys_rgb, settings.interweave, default) \
            if settings.interweave > 0 else {}
        objects.append(_object_xml(k, sub, states, default, trees))
        components.append(f'    <component p:path="/{obj_path}" objectid="{k}" p:UUID="{uuid.uuid4()}" '
                          'transform="1 0 0 0 1 0 0 0 1 0 0 0"/>\n')
        parts_cfg.append(f'    <part id="{k}" subtype="normal_part">\n'
                         f'      <metadata key="name" value="{safe}_{k}"/>\n'
                         '      <metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>\n'
                         f'      <metadata key="extruder" value="{default}"/>\n    </part>\n')
    oid = len(objects) + 1
    root = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
            'xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" '
            'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" requiredextensions="p">\n'
            ' <metadata name="Application">Irodori</metadata>\n'
            ' <metadata name="BambuStudio:3mfVersion">1</metadata>\n'
            f' <metadata name="Title">{safe}</metadata>\n'
            ' <metadata name="slic3rpe:MmPaintingVersion">1</metadata>\n'
            ' <resources>\n'
            f'  <object id="{oid}" p:UUID="{uuid.uuid4()}" type="model">\n   <components>\n' + "".join(components) +
            '   </components>\n  </object>\n </resources>\n'
            f' <build p:UUID="{uuid.uuid4()}">\n'
            f'  <item objectid="{oid}" p:UUID="{uuid.uuid4()}" transform="1 0 0 0 1 0 0 0 1 {BED_CENTER[0]} {BED_CENTER[1]} 0" printable="1"/>\n'
            ' </build>\n</model>\n')
    # no <plate> section: with one, the Orca 2.4 CLI crashes in expand_plate_extruders (issue #935)
    model_settings = ('<?xml version="1.0" encoding="UTF-8"?>\n<config>\n'
                      f'  <object id="{oid}">\n'
                      f'    <metadata key="name" value="{safe}"/>\n    <metadata key="extruder" value="{backing_slot}"/>\n'
                      + "".join(parts_cfg) + '  </object>\n</config>\n')
    content_types = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                     '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
                     ' <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
                     ' <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
                     ' <Default Extension="png" ContentType="image/png"/>\n'
                     ' <Default Extension="config" ContentType="text/xml"/>\n</Types>\n')
    rels = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
            ' <Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n'
            '</Relationships>\n')
    model_rels = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                  '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
                  f' <Relationship Target="/{obj_path}" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n'
                  '</Relationships>\n')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("3D/3dmodel.model", root)
        z.writestr("3D/_rels/3dmodel.model.rels", model_rels)
        z.writestr(obj_path, _objects_xml(objects))
        z.writestr("Metadata/project_settings.config", json.dumps(cfg, indent=4, ensure_ascii=False))
        z.writestr("Metadata/model_settings.config", model_settings)
        if prof:
            z.writestr("Metadata/layer_heights_profile.txt", _profile_points(prof, top))
    return buf.getvalue()


def predicted_glb(mesh: trimesh.Trimesh, face_rgb: np.ndarray) -> bytes:
    m = trimesh.Trimesh(mesh.vertices, mesh.faces, process=False)
    m.visual.face_colors = np.c_[np.clip(face_rgb, 0, 255), np.full(len(face_rgb), 255)].astype(np.uint8)
    return trimesh.Scene(m).export(file_type="glb")
