"""Calibration card: an upright plaque whose front face shows a grid of recipes.

Layer mixing is only visible on side walls, so the card is printed standing
up and photographed from the front (-Y).
"""
from __future__ import annotations

import numpy as np
import trimesh

from export_3mf import write_project
from palette import Settings
from recipes import Recipe, generate_words

COLS = 16
TILE_W = 10.0    # mm
TILE_H = 6.0     # mm (print time grows with height: every layer needs all tools)
GAP = 1.5        # mm frame between tiles
THICK = 5.0      # mm
MAX_TILES = 48


def layout(n: int) -> dict:
    words = list(generate_words(n, 12, 2))
    fr_all = np.array([[w.count(s) / len(w) for s in range(1, n + 1)] for w in words])
    tiles = [{"word": [i + 1], "fractions": np.eye(n)[i].tolist(), "pure": i} for i in range(n)]
    chosen_fr = [np.eye(n)[i] for i in range(n)]
    # farthest-point sampling of mixtures over the composition simplex
    d = np.min(np.linalg.norm(fr_all[:, None, :] - np.array(chosen_fr)[None], axis=2), axis=1)
    while len(tiles) < MAX_TILES and d.max() > 1e-6:
        j = int(np.argmax(d))
        tiles.append({"word": list(words[j]), "fractions": fr_all[j].tolist(), "pure": -1})
        d = np.minimum(d, np.linalg.norm(fr_all - fr_all[j], axis=1))
    rows = int(np.ceil(len(tiles) / COLS))
    width = COLS * TILE_W + (COLS + 1) * GAP
    height = rows * TILE_H + (rows + 1) * GAP
    for i, t in enumerate(tiles):
        r, c = divmod(i, COLS)
        x0 = GAP + c * (TILE_W + GAP)
        z0 = height - GAP - (r + 1) * TILE_H - r * GAP   # row 0 at the top
        t.update(row=r, col=c, rect=[x0, z0, x0 + TILE_W, z0 + TILE_H])
    return {"tiles": tiles, "width": width, "height": height, "cols": COLS, "rows": rows}


def _grid_box(xs, ys, zs):
    """Watertight box whose faces are split along the given grid lines."""
    verts = {}
    tris = []

    def vid(p):
        key = tuple(np.round(p, 6))
        if key not in verts:
            verts[key] = len(verts)
        return verts[key]

    def face(u, v, fixed_axis, fixed_val, flip):
        for i in range(len(u) - 1):
            for j in range(len(v) - 1):
                q = []
                for a, b in ((u[i], v[j]), (u[i + 1], v[j]), (u[i + 1], v[j + 1]), (u[i], v[j + 1])):
                    p = [0.0, 0.0, 0.0]
                    axes = [ax for ax in range(3) if ax != fixed_axis]
                    p[axes[0]], p[axes[1]], p[fixed_axis] = a, b, fixed_val
                    q.append(vid(p))
                t1, t2 = (q[0], q[1], q[2]), (q[0], q[2], q[3])
                if flip:
                    t1, t2 = t1[::-1], t2[::-1]
                tris.extend([t1, t2])

    face(xs, zs, 1, ys[0], True)    # front (-Y): axes x,z
    face(xs, zs, 1, ys[-1], False)  # back
    face(ys, zs, 0, xs[0], False)   # left: axes y,z
    face(ys, zs, 0, xs[-1], True)   # right
    face(xs, ys, 2, zs[0], False)   # bottom: axes x,y
    face(xs, ys, 2, zs[-1], True)   # top
    v = np.array(sorted(verts, key=verts.get), dtype=np.float64)
    m = trimesh.Trimesh(v, np.array(tris), process=True)
    if m.volume < 0:
        m.invert()
    return m


def card_mesh(n: int):
    lay = layout(n)
    xs = sorted({0.0, lay["width"]} | {t["rect"][0] for t in lay["tiles"]} | {t["rect"][2] for t in lay["tiles"]})
    zs = sorted({0.0, lay["height"]} | {t["rect"][1] for t in lay["tiles"]} | {t["rect"][3] for t in lay["tiles"]})
    mesh = _grid_box(np.array(xs), np.array([0.0, THICK]), np.array(zs))
    c = mesh.triangles_center
    state = np.ones(len(c), dtype=np.int32)
    recipes = [Recipe((i + 1,)) for i in range(n)]
    for t in lay["tiles"]:
        x0, z0, x1, z1 = t["rect"]
        sel = (c[:, 0] > x0) & (c[:, 0] < x1) & (c[:, 2] > z0) & (c[:, 2] < z1)
        if t["pure"] >= 0:
            state[sel] = t["pure"] + 1
        else:
            recipes.append(Recipe(tuple(t["word"])))
            state[sel] = len(recipes)
    return mesh, state, recipes, lay


def card_3mf(filaments: list[dict], layer_height: float = 0.10) -> bytes:
    mesh, state, recipes, _ = card_mesh(len(filaments))
    settings = Settings(layer_height=layer_height, colored_walls=2, take_interior=True, supports=False)
    return write_project(mesh, state, filaments, recipes, "karta_kalibracyjna", settings)
