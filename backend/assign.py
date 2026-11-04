"""Colour Threshold: merge specks and near-identical neighbouring patches."""
from __future__ import annotations

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components


def _components(labels: np.ndarray, pairs: np.ndarray):
    same = labels[pairs[:, 0]] == labels[pairs[:, 1]]
    p = pairs[same]
    n = len(labels)
    g = coo_matrix((np.ones(len(p)), (p[:, 0], p[:, 1])), shape=(n, n))
    return connected_components(g, directed=False)[1]


def apply_color_threshold(labels: np.ndarray, entry_ok: np.ndarray, adjacency, face_area: np.ndarray,
                          level: int, passes: int = 4) -> np.ndarray:
    """level 0..10 (0 = keep everything, 10 = fastest print)."""
    if level <= 0:
        return labels
    pairs, edge_len = adjacency
    a_min = 0.08 * level ** 2           # mm^2: specks smaller than this are absorbed
    d_sim = 0.004 * level               # OKLab distance treated as "same colour"
    a_sim = 4 * a_min
    labels = labels.copy()
    for _ in range(passes):
        comp = _components(labels, pairs)
        ncomp = comp.max() + 1
        carea = np.bincount(comp, weights=face_area, minlength=ncomp)
        clabel = np.zeros(ncomp, dtype=labels.dtype)
        clabel[comp] = labels
        ca, cb = comp[pairs[:, 0]], comp[pairs[:, 1]]
        diff = ca != cb
        if not diff.any():
            break
        ca, cb, el = ca[diff], cb[diff], edge_len[diff]
        # shared boundary length per (comp, neighbour) both directions
        src = np.concatenate([ca, cb])
        dst = np.concatenate([cb, ca])
        ln = np.concatenate([el, el])
        key = src.astype(np.int64) * ncomp + dst
        uk, inv = np.unique(key, return_inverse=True)
        shared = np.bincount(inv.ravel(), weights=ln)
        s_comp = uk // ncomp
        d_comp = uk % ncomp
        cdist = np.linalg.norm(entry_ok[clabel[s_comp]] - entry_ok[clabel[d_comp]], axis=1)
        small = carea[s_comp] < a_min
        similar = (cdist < d_sim) & (carea[s_comp] < a_sim) & (carea[s_comp] <= carea[d_comp])
        cand = small | similar
        if not cand.any():
            break
        # for each source comp pick the neighbour with the longest shared boundary
        # (similar colours preferred)
        pref = shared * np.where(cdist < d_sim, 4.0, 1.0)
        pref = np.where(cand, pref, -1)
        order = np.lexsort((-pref, s_comp))
        s_sorted = s_comp[order]
        firsts = order[np.r_[True, s_sorted[1:] != s_sorted[:-1]]]
        firsts = firsts[pref[firsts] > 0]
        new_label = clabel.copy()
        new_label[s_comp[firsts]] = clabel[d_comp[firsts]]
        changed = new_label[comp] != labels
        if not changed.any():
            break
        labels = new_label[comp]
    return labels
