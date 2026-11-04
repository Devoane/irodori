"""Encode/decode Orca/Bambu `paint_color` triangle strings.

Mirrors TriangleSelector::serialize/deserialize and FacetsAnnotation::
get_triangle_as_string in Snapmaker Orca 2.4 (states up to 255).
"""
from __future__ import annotations

import numpy as np

_HEX = "0123456789ABCDEF"


def _leaf_nibbles(state: int) -> list[int]:
    if state < 0 or state > 255:
        raise ValueError(f"state out of range: {state}")
    if state < 3:
        return [state << 2]
    n = state - 3
    nibbles = [0b1100]
    while n >= 15:
        nibbles.append(0b1111)
        n -= 15
    nibbles.append(n)
    return nibbles


def _to_string(nibbles: list[int]) -> str:
    # get_triangle_as_string prepends each nibble
    return "".join(_HEX[x] for x in reversed(nibbles))


def encode_state(state: int) -> str:
    """paint_color string for an unsplit triangle painted with `state` (0 = none)."""
    return _to_string(_leaf_nibbles(state))


def encode_tree(tree, default_state: int = 1) -> str:
    """Tree = leaf state (int) or 4 subtrees of a 3-side split, in the child order
    of decode_triangle: (a,mab,mca), (mab,b,mbc), (mbc,c,mca), (mab,mbc,mca).
    Leaves equal to the object's own filament are written as unpainted (0)."""
    nibbles: list[int] = []

    def rec(node):
        if isinstance(node, int):
            nibbles.extend(_leaf_nibbles(0 if node == default_state else node))
            return
        nibbles.append(0b0011)
        for child in reversed(node):
            rec(child)

    rec(tree)
    return _to_string(nibbles)


def subdivide_states(v0, v1, v2, state_at, min_edge: float, max_depth: int = 7):
    """Paint tree for one triangle: split until edges are below min_edge and
    sample state_at(point) at each leaf centroid; uniform children collapse."""
    def rec(a, b, c, depth):
        edge = max(np.linalg.norm(a - b), np.linalg.norm(b - c), np.linalg.norm(c - a))
        if depth >= max_depth or edge <= min_edge:
            return int(state_at((a + b + c) / 3.0))
        mab, mbc, mca = _mid(b, a), _mid(c, b), _mid(a, c)
        kids = [rec(a, mab, mca, depth + 1), rec(mab, b, mbc, depth + 1),
                rec(mbc, c, mca, depth + 1), rec(mab, mbc, mca, depth + 1)]
        if all(isinstance(k, int) for k in kids) and len(set(kids)) == 1:
            return kids[0]
        return kids

    return rec(np.asarray(v0, float), np.asarray(v1, float), np.asarray(v2, float), 0)


def _nibbles(s: str) -> list[int]:
    return [int(ch, 16) for ch in reversed(s.strip().upper())]


def _mid(a, b):
    return (a + b) * 0.5


def decode_triangle(s: str, v0, v1, v2):
    """Decode one paint string into leaf triangles.

    Returns list of ((p0, p1, p2), state) in the original winding.
    """
    nib = _nibbles(s)
    pos = 0

    def read():
        nonlocal pos
        if pos >= len(nib):
            raise ValueError("truncated paint string")
        v = nib[pos]
        pos += 1
        return v

    out: list = []

    def rec(verts):
        code = read()
        split = code & 0b11
        if split == 0:
            if (code & 0b1100) == 0b1100:
                nxt = read()
                num = 0
                while nxt == 0b1111:
                    num += 1
                    nxt = read()
                state = nxt + 15 * num + 3
            else:
                state = code >> 2
            out.append((verts, state))
            return
        special = code >> 2
        ss = special if split != 3 else 0
        a, b, c = verts[ss % 3], verts[(ss + 1) % 3], verts[(ss + 2) % 3]
        if split == 1:
            m = _mid(c, b)
            children = [(a, b, m), (m, c, a)]
        elif split == 2:
            m1 = _mid(b, a)
            m2 = _mid(a, c)
            children = [(a, m1, m2), (m1, b, m2), (b, c, m2)]
        else:
            mab = _mid(b, a)
            mbc = _mid(c, b)
            mca = _mid(a, c)
            children = [(a, mab, mca), (mab, b, mbc), (mbc, c, mca), (mab, mbc, mca)]
        # children are serialised in reverse order
        for child in reversed(children):
            rec(child)

    rec((np.asarray(v0, float), np.asarray(v1, float), np.asarray(v2, float)))
    return out


def expand_painted_mesh(vertices: np.ndarray, faces: np.ndarray, paint: list[str | None], default_state: np.ndarray):
    """Split painted triangles into their leaves. Unpainted triangles and leaves
    with state 0 take the per-face default (object/part filament).
    Returns (vertices, faces, states, source face per face) with unshared vertices."""
    tris, states, origin = [], [], []
    for fi, (i0, i1, i2) in enumerate(faces):
        p0, p1, p2 = vertices[i0], vertices[i1], vertices[i2]
        code = paint[fi] if fi < len(paint) else None
        if not code:
            tris.append((p0, p1, p2))
            states.append(default_state[fi])
            origin.append(fi)
            continue
        for tri, st in decode_triangle(code, p0, p1, p2):
            tris.append(tri)
            states.append(st or default_state[fi])
            origin.append(fi)
    verts = np.asarray(tris, dtype=np.float64).reshape(-1, 3)
    return verts, np.arange(len(verts)).reshape(-1, 3), np.asarray(states, dtype=np.int32), np.asarray(origin)
