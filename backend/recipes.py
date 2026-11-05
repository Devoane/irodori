"""Layer-sequence recipes built from physical filaments.

A word is a cycle of physical slots (1-based), one per layer. Mixed words may
not repeat a filament more than `max_run` times in a row, seam included.
With dual walls a recipe carries an outer word (outer perimeter) and an inner
word (every wall behind it); Orca addresses them as pattern groups "outer,inner".
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from math import gcd

import numpy as np

from color import mixer_lerp, rgb_to_oklab

OUTER_MAX_RUN = 2
INNER_MAX_RUN = 3


@dataclass(frozen=True)
class Recipe:
    word: tuple[int, ...]
    inner: tuple[int, ...] | None = None

    @property
    def counts(self) -> dict[int, int]:
        return _counts(self.word)

    def groups(self) -> list[tuple[int, ...]]:
        return [self.word] if self.inner is None else [self.word, self.inner]

    def pattern(self) -> str:
        return ",".join("".join(str(s) for s in g) for g in self.groups())


def _counts(word) -> dict[int, int]:
    c: dict[int, int] = {}
    for s in word:
        c[s] = c.get(s, 0) + 1
    return c


def max_cyclic_run(word) -> int:
    n = len(word)
    if n == 0:
        return 0
    if all(w == word[0] for w in word):
        return n
    start = next(i for i in range(n) if word[i] != word[i - 1])
    w = word[start:] + word[:start]
    best = run = 1
    for i in range(1, n):
        run = run + 1 if w[i] == w[i - 1] else 1
        best = max(best, run)
    return best


def _interleave(counts: dict[int, int], max_run: int):
    """Spread counts evenly over a cycle (error diffusion) honouring max_run."""
    total = sum(counts.values())
    slots = sorted(counts)
    emitted = {s: 0 for s in slots}
    word: list[int] = []
    for t in range(total):
        target = {s: counts[s] * (t + 1) / total for s in slots}
        chosen = None
        for s in sorted(slots, key=lambda s: (emitted[s] - target[s], s)):
            if emitted[s] >= counts[s]:
                continue
            run = 0
            for x in reversed(word):
                if x != s:
                    break
                run += 1
            if run < max_run:
                chosen = s
                break
        if chosen is None:
            return None
        word.append(chosen)
        emitted[chosen] += 1
    return tuple(word) if max_cyclic_run(word) <= max_run else None


def _compositions(total: int, parts: int):
    if parts == 1:
        yield (total,)
        return
    for first in range(total + 1):
        for rest in _compositions(total - first, parts - 1):
            yield (first,) + rest


@lru_cache(maxsize=64)
def generate_words(n_slots: int, max_len: int = 20, max_run: int = OUTER_MAX_RUN):
    """All primitive mixed words over slots 1..n_slots up to max_len layers."""
    words = []
    for length in range(2, max_len + 1):
        for counts in _compositions(length, n_slots):
            nz = [c for c in counts if c]
            if len(nz) < 2:
                continue
            g = 0
            for c in nz:
                g = gcd(g, c)
            if g != 1:
                continue  # same ratio already exists at a shorter length
            w = _interleave({i + 1: c for i, c in enumerate(counts) if c}, max_run)
            if w is not None:
                words.append(w)
    return tuple(words)


def predict_many(counts: np.ndarray, phys_rgb: np.ndarray) -> np.ndarray:
    """Orca's blend_color_multi for many recipes: counts (R, n) -> (R, 3) sRGB."""
    R, n = counts.shape
    cur = np.zeros((R, 3))
    acc = np.zeros(R)
    for s in range(n):
        cnt = counts[:, s].astype(np.float64)
        has = cnt > 0
        cur[has & (acc == 0)] = phys_rgb[s]
        mix = has & (acc > 0)
        if mix.any():
            t = cnt[mix] / (acc[mix] + cnt[mix])
            cur[mix] = mixer_lerp(cur[mix], np.broadcast_to(phys_rgb[s], (mix.sum(), 3)), t)
        acc += cnt
    return cur


def _word_colors(words, phys_rgb, correction):
    n = len(phys_rgb)
    counts = np.zeros((len(words), n), dtype=np.int64)
    for i, w in enumerate(words):
        for s, c in _counts(w).items():
            counts[i, s - 1] = c
    rgb = predict_many(counts, phys_rgb)
    if correction is not None:
        rgb = correction.apply_many(counts)
    return rgb


def recipe_colors(recipes: list[Recipe], phys_rgb: np.ndarray, correction=None, backing: float = 0.3) -> np.ndarray:
    """Predicted sRGB of fixed recipes for another set of spools (same model as build_candidates)."""
    phys_rgb = np.asarray(phys_rgb, dtype=np.float64)
    rgb = _word_colors([r.word for r in recipes], phys_rgb, correction)
    dual = [i for i, r in enumerate(recipes) if r.inner is not None]
    if dual:
        inner = _word_colors([recipes[i].inner for i in dual], phys_rgb, correction)
        rgb[dual] = mixer_lerp(rgb[dual], inner, backing)
    return rgb


SLICER_CYCLE = 48      # Orca caps the colour-preview cycle at this many layers
SLICER_TOL = 0.035     # OKLab: largest gap between the printed look and Orca's swatch for a dual-wall recipe


def _prefix_counts(words: list[tuple[int, ...]], n: int) -> np.ndarray:
    """(W, max_len + 1, n): slot counts of the first k layers of each word."""
    L = max(len(w) for w in words)
    out = np.zeros((len(words), L + 1, n))
    for i, w in enumerate(words):
        for k, slot in enumerate(w):
            out[i, k + 1] = out[i, k]
            out[i, k + 1, slot - 1] += 1
        out[i, len(w) + 1:] = out[i, len(w)]
    return out


def _slicer_counts(outer, inner, oi, ii, n: int, walls: int) -> np.ndarray:
    """Slot counts behind the swatch Snapmaker Orca shows for a mixed filament
    (compute_mixed_filament_display_color): over min(lcm of group lengths, 48)
    layers, the outer group on the first of `walls` perimeters and the inner
    group on the rest. ii < 0 = single-group recipe, counted as one word."""
    po, lo = _prefix_counts(outer, n), np.array([len(w) for w in outer])
    full_o = po[np.arange(len(outer)), lo]
    counts = full_o[oi].copy()
    dual = ii >= 0
    if dual.any() and inner:
        pi, li = _prefix_counts(inner, n), np.array([len(w) for w in inner])
        a, b = oi[dual], ii[dual]
        cyc = np.minimum(np.lcm(lo[a], li[b]), SLICER_CYCLE)
        c_o = (cyc // lo[a])[:, None] * full_o[a] + po[a, cyc % lo[a]]
        c_i = (cyc // li[b])[:, None] * pi[b, li[b]] + pi[b, cyc % li[b]]
        counts[dual] = c_o + max(walls - 1, 0) * c_i
    return counts


def slicer_colors(recipes: list[Recipe], phys_rgb: np.ndarray, walls: int) -> np.ndarray:
    """sRGB swatch Snapmaker Orca shows for each recipe."""
    words = sorted({r.word for r in recipes} | {r.inner for r in recipes if r.inner is not None})
    idx = {w: i for i, w in enumerate(words)}
    oi = np.array([idx[r.word] for r in recipes])
    ii = np.array([idx[r.inner] if r.inner is not None else -1 for r in recipes])
    return predict_many(_slicer_counts(words, words, oi, ii, len(phys_rgb), walls), np.asarray(phys_rgb, float))


def build_candidates(phys_rgb: np.ndarray, max_len: int = 20, max_run: int = OUTER_MAX_RUN,
                     correction=None, dual: bool = False, backing: float = 0.3, walls: int = 2):
    """(recipes, predicted sRGB). Pure physical entries come first.

    Dual walls: the visible colour is the outer word with `backing` of the inner
    word showing through the translucent outer bead. Orca's own swatch averages
    all `walls` perimeters instead, so only pairs where the two agree are kept:
    the slicer then shows the colour that will actually be printed.
    """
    phys_rgb = np.asarray(phys_rgb, dtype=np.float64)
    n = len(phys_rgb)
    pure = [(i + 1,) for i in range(n)]
    if not dual:
        words = pure + list(generate_words(n, max_len, max_run))
        return [Recipe(w) for w in words], _word_colors(words, phys_rgb, correction)

    outer = pure + list(generate_words(n, min(max_len, 12), max_run))
    inner = pure + list(generate_words(n, 6, INNER_MAX_RUN))
    o_plain, i_plain = _word_colors(outer, phys_rgb, None), _word_colors(inner, phys_rgb, None)
    oi, ii = np.meshgrid(np.arange(len(outer)), np.arange(len(inner)), indexing="ij")
    oi, ii = oi.ravel(), ii.ravel()
    keep = ~((oi < n) & (ii < n) & (oi == ii))          # X over X is just physical X
    oi, ii = oi[keep], ii[keep]
    looks = np.empty((len(oi), 3))
    for s in range(0, len(oi), 20_000):
        looks[s:s + 20_000] = mixer_lerp(o_plain[oi[s:s + 20_000]], i_plain[ii[s:s + 20_000]], backing)
    swatch = predict_many(_slicer_counts(outer, inner, oi, ii, n, walls), phys_rgb)
    agree = np.linalg.norm(rgb_to_oklab(looks) - rgb_to_oklab(swatch), axis=1) < SLICER_TOL
    oi, ii, looks = oi[agree], ii[agree], looks[agree]
    if correction is not None:
        o_rgb, i_rgb = _word_colors(outer, phys_rgb, correction), _word_colors(inner, phys_rgb, correction)
        for s in range(0, len(oi), 20_000):
            looks[s:s + 20_000] = mixer_lerp(o_rgb[oi[s:s + 20_000]], i_rgb[ii[s:s + 20_000]], backing)
    recipes = [Recipe(w) for w in pure] + [Recipe(outer[a], inner[b]) for a, b in zip(oi, ii)]
    return recipes, np.concatenate([phys_rgb, looks])
