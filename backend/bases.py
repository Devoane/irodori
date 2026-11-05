"""Procedural display bases under the model, with their own colours.

A base is a polar-grid slab (outline radius R(theta) on an ellipse that hugs the
model's footprint, top height z(r, theta)) with vertex colours from a style
function. It is printed as its own part of the same object, so Orca unions it
with the model.
"""
from __future__ import annotations

import numpy as np
import trimesh

SIZES = [("Compact", 2.0, 3.0), ("Standard", 6.0, 4.0), ("Large", 12.0, 5.0)]   # name, margin mm, thickness mm
EMBED = 0.6          # the model sinks this far into the base so the two parts overlap


def _noise(x, y, scale, seed=0):
    """Cheap smooth 2D value noise in -1..1 (sum of sines with fixed random phases)."""
    rng = np.random.default_rng(seed)
    out = np.zeros_like(x)
    for k in range(6):
        a = rng.uniform(0, np.pi)
        f = (1.7 ** k) / scale
        out += np.sin(f * (x * np.cos(a) + y * np.sin(a)) + rng.uniform(0, 6.3)) / (1.6 ** k)
    return out / 2.2


def _cells(x, y, size, seed=1):
    """(distance to the nearest cell border, cell id) of a jittered Voronoi grid."""
    rng = np.random.default_rng(seed)
    gx, gy = np.floor(x / size), np.floor(y / size)
    d1 = np.full(x.shape, np.inf)
    d2 = np.full(x.shape, np.inf)
    cid = np.zeros(x.shape)
    jit = rng.uniform(0.15, 0.85, (64, 64, 2))
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            cx, cy = gx + dx, gy + dy
            j = jit[cx.astype(int) % 64, cy.astype(int) % 64]
            px, py = (cx + j[..., 0]) * size, (cy + j[..., 1]) * size
            d = np.hypot(x - px, y - py)
            closer = d < d1
            d2 = np.where(closer, d1, np.minimum(d2, d))
            cid = np.where(closer, cx * 131 + cy * 17, cid)
            d1 = np.where(closer, d, d1)
    return (d2 - d1) / 2, cid


def _mix(a, b, t):
    t = np.clip(t, 0, 1)[..., None]
    return np.asarray(a, float) * (1 - t) + np.asarray(b, float) * t


# style: outline R(theta), top profile z(r) as a fraction of the thickness, colour(x, y, r, theta, side)
def _round_outline(th):
    return np.ones_like(th)


def _wobble(th):
    return 1 + 0.05 * np.sin(3 * th + 1) + 0.03 * np.sin(5 * th + 2)


def _stone_outline(th):
    k = np.linspace(0, 2 * np.pi, 10, endpoint=False)
    r = 0.9 + 0.1 * np.random.default_rng(4).random(10)
    return np.interp(th, np.r_[k, 2 * np.pi], np.r_[r, r[0]])


def _square(th, p=6.0):
    return 1 / (np.abs(np.cos(th)) ** p + np.abs(np.sin(th)) ** p) ** (1 / p)


def _star(th):
    return 0.86 + 0.14 * np.abs(np.cos(6 * th)) ** 3


def _flutes(th):
    return 1 - 0.035 * (np.cos(20 * th) > 0.3)


def _chamfer(r):
    return np.where(r > 0.94, 1 - 2.5 * (r - 0.94), 1.0)


def _dome(r):
    return 0.85 + 0.25 * (1 - r ** 2)


def _rim(r):
    return np.where(r > 0.86, 1.25, 1.0)


def _steps(r):
    return np.where(r > 0.82, 0.6, 1.0)


def _c_round(x, y, r, th, side):
    return _mix([62, 66, 74], [44, 47, 54], r ** 4 + side)


def _c_pebble(x, y, r, th, side):
    n = _noise(x, y, 6)
    return _mix([150, 140, 128], [102, 96, 90], 0.5 + 0.6 * n + 0.3 * side)


def _c_stone(x, y, r, th, side):
    n = _noise(x, y, 4, 2)
    return _mix([128, 128, 124], [78, 78, 76], 0.5 + 0.7 * n + 0.3 * side)


def _c_cracked(x, y, r, th, side):
    d, _ = _cells(x, y, 9, 3)
    base = _mix([150, 146, 140], [118, 114, 110], 0.5 + 0.5 * _noise(x, y, 5))
    return _mix(base, [36, 34, 32], (1 - d / 0.6) * (1 - side))


def _c_paved(x, y, r, th, side):
    t = 7.0
    gx, gy = np.abs((x / t) % 1 - 0.5), np.abs(((y / t) + 0.5 * (np.floor(x / t) % 2)) % 1 - 0.5)
    grout = (np.maximum(gx, gy) > 0.44) * (1 - side)
    tone = _mix([170, 160, 146], [140, 128, 112], 0.5 + 0.5 * _noise(np.floor(x / t), np.floor(y / t), 0.9, 5))
    return _mix(tone, [70, 66, 60], grout)


def _c_gridstone(x, y, r, th, side):
    t = 10.0
    line = ((np.abs((x / t) % 1 - 0.5) > 0.46) | (np.abs((y / t) % 1 - 0.5) > 0.46)) * (1 - side)
    return _mix(_mix([96, 100, 108], [70, 74, 82], 0.5 + 0.5 * _noise(x, y, 8, 6)), [40, 42, 48], line)


def _c_concrete(x, y, r, th, side):
    speck = (_noise(x * 7, y * 7, 1, 7) > 0.55) * 0.5
    return _mix([168, 168, 164], [120, 120, 118], 0.4 + 0.3 * _noise(x, y, 10, 8) + speck + 0.3 * side)


def _c_grassy(x, y, r, th, side):
    n = _noise(x * 2, y * 2, 3, 9)
    grass = _mix([70, 140, 52], [150, 182, 70], 0.5 + 0.7 * n)
    return _mix(grass, [104, 74, 46], side)


def _c_arena(x, y, r, th, side):
    sand = _mix([214, 186, 132], [190, 160, 108], 0.5 + 0.5 * _noise(x, y, 5, 10))
    return _mix(sand, [120, 82, 56], np.maximum((r > 0.86) * 1.0, side))


def _c_temple(x, y, r, th, side):
    return _mix([222, 214, 196], [168, 158, 140], np.maximum((r > 0.82) * 0.6, side * 0.8) + 0.2 * _noise(x, y, 6, 11))


def _c_column(x, y, r, th, side):
    vein = np.abs(_noise(x + 0.3 * y, y, 9, 12)) < 0.035
    return _mix(_mix([236, 234, 228], [205, 203, 198], side), [150, 150, 156], vein * 0.8)


def _c_ionic(x, y, r, th, side):
    band = (r > 0.8) & (r < 0.9)
    return _mix(_c_column(x, y, r, th, side), [196, 160, 64], band * (1 - side))


def _c_rune(x, y, r, th, side):
    ring = (r > 0.68) & (r < 0.84) & ((np.sin(18 * th) > 0.5) | (np.abs(r - 0.76) < 0.015))
    return _mix(_mix([46, 50, 62], [30, 32, 40], side + 0.3 * _noise(x, y, 5, 13)), [40, 220, 240], ring * (1 - side))


def _c_magic(x, y, r, th, side):
    star = np.zeros_like(r)
    for k in range(5):
        a = np.pi / 2 + k * 4 * np.pi / 5
        b = np.pi / 2 + (k + 1) * 4 * np.pi / 5
        p, q = 0.8 * np.array([np.cos(a), np.sin(a)]), 0.8 * np.array([np.cos(b), np.sin(b)])
        u, v = r * np.cos(th), r * np.sin(th)
        t = np.clip(((u - p[0]) * (q[0] - p[0]) + (v - p[1]) * (q[1] - p[1])) / np.sum((q - p) ** 2), 0, 1)
        star = np.maximum(star, np.hypot(u - p[0] - t * (q[0] - p[0]), v - p[1] - t * (q[1] - p[1])) < 0.02)
    circle = np.abs(r - 0.8) < 0.02
    return _mix(_mix([70, 30, 110], [40, 18, 66], side + 0.4 * r), [250, 200, 90], np.maximum(star, circle) * (1 - side))


def _c_spiked(x, y, r, th, side):
    return _mix([58, 60, 66], [176, 180, 188], np.clip((r - 0.85) * 6, 0, 1) * (1 - side) + side * 0.2)


STYLES = {
    "pebble": ("Pebble", _wobble, _dome, _c_pebble),
    "cracked": ("Cracked", _round_outline, _chamfer, _c_cracked),
    "round": ("Round", _round_outline, _chamfer, _c_round),
    "temple": ("Temple", lambda th: _square(th, 8), _steps, _c_temple),
    "rune": ("Runes", _round_outline, _chamfer, _c_rune),
    "paved": ("Paving", _round_outline, _chamfer, _c_paved),
    "stone": ("Boulder", _stone_outline, _dome, _c_stone),
    "arena": ("Arena", _round_outline, _rim, _c_arena),
    "concrete": ("Concrete", lambda th: _square(th, 10), _chamfer, _c_concrete),
    "gridstone": ("Slabs", lambda th: _square(th, 10), _chamfer, _c_gridstone),
    "column": ("Column", _flutes, _chamfer, _c_column),
    "ionic": ("Ionic", _flutes, _rim, _c_ionic),
    "grassy": ("Grass", _wobble, _dome, _c_grassy),
    "magic": ("Magic", _round_outline, _chamfer, _c_magic),
    "spiked": ("Spikes", _star, _chamfer, _c_spiked),
}


def footprint(vertices: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(centre xy, semi-axes) of the smallest axis-aligned ellipse, with the
    bounding box aspect, that holds every vertex."""
    xy = vertices[:, :2]
    lo, hi = xy.min(axis=0), xy.max(axis=0)
    c = (lo + hi) / 2
    h = np.maximum((hi - lo) / 2, 1.0)
    s = float(np.sqrt((((xy - c) / h) ** 2).sum(axis=1)).max())
    return c, h * s


def make_base(vertices: np.ndarray, style: str, size: int = 0, rings: int = 70,
              segments: int = 360) -> tuple[trimesh.Trimesh, np.ndarray]:
    """(base mesh with its top at z = 0, sRGB per face) for a model with these vertices."""
    if style not in STYLES:
        raise ValueError(f"Unknown base: {style}")
    _, outline, profile, colour = STYLES[style]
    _, margin, T = SIZES[int(np.clip(size, 0, len(SIZES) - 1))]
    c, ax = footprint(vertices)
    if outline is not _round_outline and outline is not _wobble:
        ax = ax * 1.04          # square and star outlines dip inside the ellipse
    ax = ax + margin
    th = np.linspace(0, 2 * np.pi, segments, endpoint=False)
    R = outline(th)
    r = np.linspace(0, 1, rings + 1)[1:]
    rr, tt = np.meshgrid(r, th, indexing="ij")                # (rings, segments)
    RR = np.broadcast_to(R, rr.shape)
    x = c[0] + ax[0] * RR * rr * np.cos(tt)
    y = c[1] + ax[1] * RR * rr * np.sin(tt)
    z = T * profile(rr) - T
    top = np.c_[x.ravel(), y.ravel(), z.ravel()]
    ctr_top = [c[0], c[1], T * float(profile(np.array([0.0]))[0]) - T]
    bottom_ring = np.c_[x[-1], y[-1], np.full(segments, -T)]
    V = np.vstack([[ctr_top], top, bottom_ring, [[c[0], c[1], -T]]])
    it = lambda i, j: 1 + i * segments + (j % segments)          # noqa: E731
    ib = lambda j: 1 + rings * segments + (j % segments)          # noqa: E731
    bc = len(V) - 1
    F = []
    for j in range(segments):
        F.append((0, it(0, j), it(0, j + 1)))
        for i in range(rings - 1):
            a, b, d, e = it(i, j), it(i, j + 1), it(i + 1, j), it(i + 1, j + 1)
            F += [(a, d, e), (a, e, b)]
        a, b = it(rings - 1, j), it(rings - 1, j + 1)
        F += [(a, ib(j), ib(j + 1)), (a, ib(j + 1), b)]
        F.append((bc, ib(j + 1), ib(j)))
    mesh = trimesh.Trimesh(V, np.array(F), process=False)
    if mesh.volume < 0:
        mesh.invert()
    cen = mesh.triangles_center
    rel = (cen[:, :2] - c) / ax
    rn = np.hypot(rel[:, 0], rel[:, 1]) / np.maximum(np.interp(np.arctan2(rel[:, 1], rel[:, 0]) % (2 * np.pi),
                                                              np.r_[th, 2 * np.pi], np.r_[R, R[0]]), 1e-9)
    side = (np.abs(mesh.face_normals[:, 2]) < 0.5).astype(float)
    rgb = colour(cen[:, 0], cen[:, 1], np.clip(rn, 0, 1), np.arctan2(rel[:, 1], rel[:, 0]), side)
    return mesh, np.clip(rgb, 0, 255)


def attach_base(mesh: trimesh.Trimesh, face_rgb: np.ndarray, style: str, size: int = 0):
    """Model lifted onto a new base. Returns (mesh, face_rgb, is_base per face,
    lift applied to the model)."""
    base, base_rgb = make_base(mesh.vertices, style, size)
    lift = float(-base.bounds[0][2]) - EMBED - float(mesh.bounds[0][2])
    model = mesh.copy()
    model.apply_translation([0, 0, lift])
    base.apply_translation([0, 0, float(-base.bounds[0][2])])
    out = trimesh.util.concatenate([model, base])
    is_base = np.r_[np.zeros(len(model.faces), dtype=bool), np.ones(len(base.faces), dtype=bool)]
    return out, np.r_[face_rgb, base_rgb], is_base, lift


def preview_with_base(preview_glb: bytes, mesh: trimesh.Trimesh, face_rgb: np.ndarray, is_base: np.ndarray,
                      lift: float) -> bytes:
    """The source preview lifted by `lift`, plus the coloured base."""
    import io
    scene = trimesh.load(io.BytesIO(preview_glb), file_type="glb", force="scene", process=False)
    scene.apply_translation([0, 0, lift])
    base = mesh.submesh([np.nonzero(is_base)[0]], append=True)
    base.visual.face_colors = np.c_[face_rgb[is_base], np.full(int(is_base.sum()), 255)].astype(np.uint8)
    scene.add_geometry(base, geom_name="kaicho_base")
    return scene.export(file_type="glb")
