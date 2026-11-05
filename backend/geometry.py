"""Printable geometry: triangle budget, colour refinement and face colours."""
from __future__ import annotations

import numpy as np
import trimesh
from scipy.spatial import cKDTree


from color import linear_to_srgb, srgb_to_linear
from source import SourceModel


def solidify(mesh: trimesh.Trimesh, target_faces: int = 1_000_000,
             max_cells: int = 400, min_pitch: float = 0.3) -> trimesh.Trimesh:
    """Rebuild an open / multi-shell mesh as one watertight solid.

    Inside = generalised winding number > 0.5; thin single-sided shells are
    kept by thickening a band around the surface. Colours are re-sampled later
    from the untouched source field, so appearance is not affected.
    """
    import igl
    from scipy.ndimage import gaussian_filter
    from skimage.measure import marching_cubes

    lo, hi = mesh.bounds
    size = hi - lo
    # marching cubes yields ~2.2 * area / pitch^2 triangles: pick the pitch from the budget
    pitch = max(min_pitch, float(size.max()) / max_cells, float(np.sqrt(2.4 * mesh.area / max(target_faces, 1))))
    pad = 3 * pitch
    lo = lo - pad
    dims = np.ceil((size + 2 * pad) / pitch).astype(int) + 1
    axes = [lo[i] + pitch * np.arange(dims[i]) for i in range(3)]
    grid = np.stack(np.meshgrid(*axes, indexing="ij"), axis=-1).reshape(-1, 3)
    wn = igl.fast_winding_number(np.ascontiguousarray(mesh.vertices, dtype=np.float64),
                                 np.ascontiguousarray(mesh.faces, dtype=np.int64), grid)
    field = np.clip(np.abs(wn), 0, 1)
    # thicken thin shells: everything within ~0.8 voxel of the surface is solid
    # (the true distance to the triangles: random surface samples left pits and bumps on flat faces)
    dense, _ = trimesh.sample.sample_surface(mesh, int(min(4e6, 6 * mesh.area / pitch ** 2)) + 1, seed=0)
    tree = cKDTree(dense)
    near = np.zeros(len(grid), dtype=bool)
    cand = np.nonzero(field < 0.5)[0]
    d, _ = tree.query(grid[cand], k=1, distance_upper_bound=2 * pitch, workers=-1)
    cand = cand[np.isfinite(d)]
    if len(cand):
        d2 = igl.point_mesh_squared_distance(grid[cand], np.ascontiguousarray(mesh.vertices, dtype=np.float64),
                                             np.ascontiguousarray(mesh.faces, dtype=np.int64))[0]
        near[cand[d2 <= (0.85 * pitch) ** 2]] = True
    field = gaussian_filter(np.where(near, 1.0, field).reshape(dims), 0.6)
    verts, faces, _, _ = marching_cubes(field, level=0.5, spacing=(pitch, pitch, pitch))
    verts += lo
    out = trimesh.Trimesh(verts, faces[:, ::-1], process=True)
    # drop noise specks
    bodies = out.split(only_watertight=True)
    if len(bodies):
        vols = np.array([b.volume for b in bodies])
        keep = [b for b, v in zip(bodies, vols) if v > max(vols.max() * 1e-4, pitch ** 3 * 8)]
        keep = keep or [bodies[int(np.argmax(vols))]]      # a coarse grid can leave only small bodies
        out = trimesh.util.concatenate(keep) if len(keep) > 1 else keep[0]
    if out.volume < 0:
        out.invert()
    return out


def mend_bodies(mesh: trimesh.Trimesh, rgb: np.ndarray, part: np.ndarray | None, broken: np.ndarray, progress=None):
    """Painted sources keep their triangles; only bodies that were open or overlapping in
    the source (Orca's "non-manifold edges") are rebuilt as solids, coloured from their
    nearest original face. Zero-area slivers are dropped."""
    ids = np.unique(broken[broken >= 0])
    if not len(ids):
        return mesh, rgb, part
    keep = broken < 0
    pieces = [(mesh.submesh([np.nonzero(keep)[0]], append=True), np.nonzero(keep)[0])]
    for k, b in enumerate(ids):
        idx = np.nonzero(broken == b)[0]
        piece = mesh.submesh([idx], append=True)
        if piece.area < 0.05:
            continue
        if progress:
            progress(f"Repairing leaky solids {k + 1}/{len(ids)}…")
        try:
            solid = solidify(piece, target_faces=40_000, min_pitch=0.12)
            trimesh.smoothing.filter_taubin(solid, iterations=40)
        except (IndexError, ValueError):     # nothing solid left: keep the body as it was
            solid = piece
        _, near = cKDTree(piece.triangles_center).query(solid.triangles_center, workers=-1)
        pieces.append((solid, idx[near]))
    out = trimesh.util.concatenate([m for m, _ in pieces])
    src = np.concatenate([f for _, f in pieces])
    out.metadata["mended"] = len(ids)
    return out, rgb[src], None if part is None else part[src]


def build_print_mesh(src: SourceModel, max_triangles: int = 1_000_000, repair: str = "auto",
                     front_only: bool = False, progress=None):
    """(mesh, face_rgb) for printing and preview.

    Painted 3MF sources keep their painted triangles. Textured sources are
    repaired if needed, fitted to the triangle budget, refined so colour
    boundaries can follow the texture, and coloured from the dense field.
    Front Only colours every face with what the front (-Y) view shows at its X/Z.
    """
    if src.is_exact:
        mesh = trimesh.Trimesh(src.vertices, src.faces, process=False)
        rgb = front_colors(mesh, src) if front_only else src.face_rgb.astype(np.float64)
        part = src.face_part
        if repair != "off" and src.face_broken is not None:
            mesh, rgb, part = mend_bodies(mesh, rgb, part, src.face_broken, progress)
        mesh.metadata["face_part"] = part
        return mesh, rgb

    mesh = trimesh.Trimesh(src.vertices, src.faces, process=True)
    mesh.remove_unreferenced_vertices()
    repaired = repair == "on" or (repair == "auto" and not mesh.is_watertight)
    if repaired:
        if progress:
            progress("Repairing the geometry (watertight solid for printing)…")
        solid = solidify(mesh, max_triangles)
        if len(solid.faces) > max_triangles:
            # coarser voxels instead of decimation, which would break watertightness
            solid = solidify(mesh, int(max_triangles * max_triangles / len(solid.faces) * 0.97))
        mesh = solid
    if len(mesh.faces) > max_triangles:
        import fast_simplification
        v, f = fast_simplification.simplify(mesh.vertices.astype(np.float32), mesh.faces.astype(np.int32),
                                            target_count=max_triangles)
        mesh = trimesh.Trimesh(v, f, process=True)
    # uniform midpoint subdivision keeps the mesh watertight
    for _ in range(6):
        if len(mesh.faces) * 4 > max_triangles:
            break
        v, f = trimesh.remesh.subdivide(mesh.vertices, mesh.faces)
        mesh = trimesh.Trimesh(v, f, process=True)
    mesh.metadata["repaired"] = repaired
    return mesh, (front_colors(mesh, src) if front_only else sample_face_colors(mesh, src))


def front_colors(mesh: trimesh.Trimesh, src: SourceModel, cell: float = 0.3) -> np.ndarray:
    """Project the front (-Y) appearance through the model along Y."""
    xz = src.sample_points[:, [0, 2]]
    order = np.argsort(src.sample_points[:, 1])            # nearest to the viewer first
    _, first = np.unique(np.floor(xz[order] / cell).astype(np.int64), axis=0, return_index=True)
    front = order[first]
    tree = cKDTree(xz[front])
    _, idx = tree.query(mesh.triangles_center[:, [0, 2]], k=4, workers=-1)
    return linear_to_srgb(srgb_to_linear(src.sample_rgb[front])[idx].mean(axis=1))


def sample_face_colors(mesh: trimesh.Trimesh, src: SourceModel, k: int = 4) -> np.ndarray:
    tree = cKDTree(src.sample_points)
    tri = mesh.triangles
    c = tri.mean(axis=1)
    probes = [c] + [0.6 * c + 0.4 * tri[:, i] for i in range(3)]
    lin = srgb_to_linear(src.sample_rgb)
    acc = np.zeros((len(c), 3))
    for p in probes:
        _, idx = tree.query(p, k=k, workers=-1)
        acc += lin[idx].mean(axis=1)
    return linear_to_srgb(acc / len(probes))


def face_adjacency_graph(mesh: trimesh.Trimesh):
    """(face pairs, shared edge lengths)."""
    edges = mesh.face_adjacency_edges
    return mesh.face_adjacency, np.linalg.norm(mesh.vertices[edges[:, 0]] - mesh.vertices[edges[:, 1]], axis=1)
