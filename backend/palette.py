"""Palette search: physical filament sets, virtual recipe selection, scoring."""
from __future__ import annotations

import heapq
import json
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path

import numpy as np
from scipy.cluster.vq import kmeans2

from assign import apply_color_threshold
from color import delta_e2000, hex_to_rgb, mixer_lerp, oklab_to_rgb, rgb_to_hex, rgb_to_lab, rgb_to_oklab
from recipes import Recipe, build_candidates, slicer_colors

DATA = Path(__file__).resolve().parent.parent / "data"
INVENTORY = DATA / "inventory.json"


def load_inventory() -> dict:
    """owned: library/custom ids the user has, custom: own filaments, loaded: ids in T1..Tn."""
    inv = json.loads(INVENTORY.read_text(encoding="utf-8")) if INVENTORY.exists() else {}
    return {"owned": inv.get("owned", []), "custom": inv.get("custom", []), "loaded": inv.get("loaded", [])}


def save_inventory(inv: dict):
    INVENTORY.write_text(json.dumps(inv, ensure_ascii=False, indent=1), encoding="utf-8")


def load_library():
    lib = json.loads((DATA / "filaments.json").read_text(encoding="utf-8"))
    lib["filaments"] += load_inventory()["custom"]
    return lib, {f["id"]: f for f in lib["filaments"]}


@dataclass
class Settings:
    # analysis
    toolheads: int = 4
    capacity: int = 0                # 0 = automatic, 64, 255, or == toolheads for physical filaments only
    samples: int = 4
    source: str = "library"          # library | owned | loaded (see load_inventory)
    cmyk_optimized: bool = False
    precise: bool = True             # deeper set search: wider pool, spool swaps over the whole library
    texture_optimization: bool = False
    lock_colors: bool = False        # source colours equal to a filament swatch print with that filament alone
    color_threshold: int = 0
    dual_wall: bool = False          # single word on every wall: Orca's swatch equals the print
    opaque: bool = False             # opaque filaments: every coloured wall uses the outer pattern
    backing: float = 0.3             # how much of the inner wall shows through the outer one
    front_only: bool = False
    max_recipe_len: int = 20
    max_run: int = 2
    max_triangles: int = 1_000_000
    longest_side: float = 0.0        # 0 = automatic
    repair: str = "auto"             # rebuild open / paper-thin meshes: auto | on | off
    bake_overlays: bool = True       # blend see-through layers with the surface behind them
    base: str = ""                   # display base style (bases.STYLES), "" = none
    base_size: int = 0               # index into bases.SIZES
    brightness: int = 0              # -100..100, applied to the source colours before matching
    saturation: int = 0
    rainbow: bool = False            # replace the source colours with a rainbow swirl
    rainbow_v: float = 1.0           # cycles bottom -> top
    rainbow_h: float = 1.0           # cycles centre -> outside
    rainbow_ccw: bool = False
    rainbow_offset: float = 0.0      # degrees
    rainbow_white: float = 0.0       # percent
    # export only
    layer_height: float = 0.10
    variable_layer: float = 0.0      # thicker layers where nothing is mixed (0 = off)
    detailed_top: float = 0.0        # finer layers on top surfaces (0 = off)
    colored_walls: int = 5
    take_interior: bool = True
    interweave: int = 6              # roof weave level 0..10
    vol: bool = False                # Variable Optical Layers
    subdivide_mix: bool = False      # Orca's native mixed-layer subdivision (Local-Z)
    supports: bool = True
    rotate: bool = False             # False = keep the model's own orientation
    rot_x: float = 0.0
    rot_y: float = 0.0
    rot_z: float = 0.0
    z_offset: float = 0.0            # <= 0; the part below the plate is cut off

    @property
    def no_dither(self) -> bool:
        return 0 < self.capacity <= self.toolheads

    @property
    def max_colors(self) -> int:
        return 255 if self.capacity == 0 else self.capacity

    @property
    def dual(self) -> bool:
        return self.dual_wall and not self.opaque and self.colored_walls >= 2 and self.toolheads <= 4

    @property
    def recipe_len(self) -> int:
        """Words over many slots explode combinatorially; shorten them past four toolheads."""
        return min(self.max_recipe_len, {5: 12, 6: 10, 7: 9, 8: 8}.get(self.toolheads, 40))


@dataclass
class Row:
    title: str
    filaments: list[dict]
    recipes: list[Recipe] = field(default_factory=list)
    entry_rgb: np.ndarray | None = None
    entry_slicer: np.ndarray | None = None   # swatch Snapmaker Orca shows for each entry
    labels: np.ndarray | None = None     # entry index per face
    metrics: dict = field(default_factory=dict)
    family: str = "custom"               # preset id, "search", "loaded" or "custom"
    grouped: int = 0                     # similar ranked sets folded into this row
    alternatives: dict = field(default_factory=dict)   # slot index -> interchangeable library ids
    cmy_brand: str = ""                  # CMY family of a preset row, for the brand filter

    def to_json(self):
        n = len(self.filaments)
        return {
            "title": self.title,
            "family": self.family,
            "grouped": self.grouped,
            "alternatives": self.alternatives,
            "cmy_brand": self.cmy_brand,
            "filaments": [{k: f.get(k) for k in ("id", "brand", "name", "hex", "td", "profile")} for f in self.filaments],
            "entries": [{"hex": rgb_to_hex(c), "slicer": rgb_to_hex(sc), "pattern": r.pattern(), "physical": i < n}
                        for i, (r, c, sc) in enumerate(zip(self.recipes, self.entry_rgb, self.entry_slicer))],
            "metrics": self.metrics,
        }


class Targets:
    """Area/focus weighted face colours, binned in OKLab for the palette search."""

    def __init__(self, face_rgb: np.ndarray, weights: np.ndarray, max_bins: int = 3500,
                 score_weights: np.ndarray | None = None):
        self.face_ok = rgb_to_oklab(face_rgb)
        self.face_lab = rgb_to_lab(face_rgb)
        self.weights = weights
        self.score_weights = weights if score_weights is None else score_weights
        sel = weights > 0
        self.ok, self.w = _bin(self.face_ok[sel], weights[sel], max_bins)

    def coarse(self, max_bins: int) -> Targets:
        """Same colours in fewer bins, for fast set scoring."""
        t = object.__new__(Targets)
        t.ok, t.w = _bin(self.ok, self.w, max_bins)
        return t


def _bin(ok: np.ndarray, w: np.ndarray, max_bins: int):
    step = 0.006
    while True:
        uniq, inv = np.unique(np.round(ok / step).astype(np.int64), axis=0, return_inverse=True)
        if len(uniq) <= max_bins:
            break
        step *= 1.3
    inv = inv.ravel()
    bw = np.bincount(inv, weights=w, minlength=len(uniq))
    out = np.stack([np.bincount(inv, weights=ok[:, d] * w, minlength=len(uniq)) for d in range(3)], 1)
    return out / np.maximum(bw, 1e-12)[:, None], bw / bw.sum()


def _dist(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    a = a.astype(np.float32)
    b = b.astype(np.float32)
    d2 = (a * a).sum(1)[:, None] + (b * b).sum(1)[None, :] - 2 * a @ b.T
    return np.sqrt(np.maximum(d2, 0))


def _lab(ok: np.ndarray) -> np.ndarray:
    return rgb_to_lab(np.clip(oklab_to_rgb(np.asarray(ok, dtype=np.float64)), 0, 255))


def _de_dist(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """CIEDE2000 between OKLab colours, the distance the match score uses."""
    la, lb = _lab(a), _lab(b)
    out = np.empty((len(a), len(b)), dtype=np.float32)
    for s in range(0, len(a), 256):
        out[s:s + 256] = delta_e2000(la[s:s + 256, None, :], lb[None, :, :])
    return out


def select_virtual(targets: Targets, cand_ok: np.ndarray, n_phys: int, k: int, auto: bool = False,
                   perceptual: bool = False) -> list[int]:
    """Greedy facility location over candidate colours (indices >= n_phys).

    perceptual: distances in CIEDE2000 instead of OKLab (slower, matches the score).
    auto: stop once further colours would recover less than 1 % of the error
    the full budget removes (and at least 0.0005 OKLab / 0.05 ΔE of mean error)."""
    if k <= 0:
        return []
    dist = _de_dist if perceptual else _dist
    mixed = np.arange(n_phys, len(cand_ok))
    _, first = np.unique(np.round(cand_ok[mixed] / 0.0025).astype(np.int64), axis=0, return_index=True)
    mixed = mixed[np.sort(first)]
    w = targets.w.astype(np.float32)
    cur = dist(targets.ok, cand_ok[:n_phys]).min(axis=1)
    D = np.empty((len(targets.ok), len(mixed)), dtype=np.float32)
    for s in range(0, len(mixed), 20_000):
        D[:, s:s + 20_000] = dist(targets.ok, cand_ok[mixed[s:s + 20_000]])
    gain0 = np.zeros(len(mixed), dtype=np.float32)
    for s in range(0, len(mixed), 20_000):
        gain0[s:s + 20_000] = (w[:, None] * np.maximum(cur[:, None] - D[:, s:s + 20_000], 0)).sum(axis=0)
    # lazy greedy: gains only shrink, so only the current best is re-evaluated
    heap = [(-float(g), j) for j, g in enumerate(gain0) if g > 0]
    heapq.heapify(heap)
    chosen: list[int] = []
    err = [float((w * cur).sum())]
    while heap and len(chosen) < k:
        _, j = heapq.heappop(heap)
        g = float((w * np.maximum(cur - D[:, j], 0)).sum())
        if g <= 1e-7:
            continue
        if heap and g < -heap[0][0]:
            heapq.heappush(heap, (-g, j))
            continue
        chosen.append(int(mixed[j]))
        cur = np.minimum(cur, D[:, j])
        err.append(err[-1] - g)
    if auto and chosen:
        e = np.array(err)
        tol = max(0.05 if perceptual else 0.0005, 0.01 * (e[0] - e[-1]))
        chosen = chosen[:int(np.argmax(e <= e[-1] + tol))]
    return chosen


def assign_faces(face_ok: np.ndarray, entry_ok: np.ndarray, n_phys: int, texture_opt: bool,
                 face_lab: np.ndarray | None = None) -> np.ndarray:
    """Nearest entry per face; with face_lab the 6 nearest in OKLab are re-ranked by CIEDE2000."""
    labels = np.empty(len(face_ok), dtype=np.int32)
    refine = face_lab is not None and not texture_opt and len(entry_ok) > 1
    entry_lab = _lab(entry_ok) if refine else None
    for s in range(0, len(face_ok), 200_000):
        d = _dist(face_ok[s:s + 200_000], entry_ok)
        if texture_opt:
            dp = d[:, :n_phys]
            d[:, :n_phys] = np.where(dp < 0.06, dp * 0.6, dp)
        if refine:
            top = np.argpartition(d, min(6, d.shape[1]) - 1, axis=1)[:, :6]
            de = delta_e2000(face_lab[s:s + 200_000, None, :], entry_lab[top])
            labels[s:s + 200_000] = top[np.arange(len(top)), np.argmin(de, axis=1)]
        else:
            labels[s:s + 200_000] = np.argmin(d, axis=1)
    return labels


LOCK_DE = 2.5     # CIEDE2000: a source colour this close to a swatch counts as that filament


def lock_physical(face_lab: np.ndarray, phys_lab: np.ndarray) -> np.ndarray:
    """Filament index per face whose colour matches a filament swatch, else -1."""
    out = np.full(len(face_lab), -1, dtype=np.int32)
    for s in range(0, len(face_lab), 200_000):
        de = delta_e2000(face_lab[s:s + 200_000, None, :], phys_lab[None, :, :])
        j = np.argmin(de, axis=1)
        out[s:s + 200_000] = np.where(de[np.arange(len(j)), j] < LOCK_DE, j, -1)
    return out


def score(face_lab: np.ndarray, entry_lab: np.ndarray, labels: np.ndarray, weights: np.ndarray) -> dict:
    de = delta_e2000(face_lab, entry_lab[labels])
    w = weights / max(weights.sum(), 1e-12)
    order = np.argsort(de)
    cw = np.cumsum(w[order])

    def quantile(q):
        return float(de[order][min(np.searchsorted(cw, q), len(de) - 1)])

    mean = float((de * w).sum())
    return {
        "match": round(float(np.clip(100 - 2.5 * mean, 0, 100)), 1),
        "mean_de": round(mean, 2),
        "rms": round(float(np.sqrt((de ** 2 * w).sum())), 2),
        "median": round(quantile(0.5), 2),
        "p95": round(quantile(0.95), 2),
        "worst": round(float(de.max()) if len(de) else 0.0, 2),
    }


def build_row(title: str, filaments: list[dict], targets: Targets, settings: Settings, family: str = "custom",
              correction=None, exact_rgb: np.ndarray | None = None, adjacency=None, face_area=None) -> Row:
    phys = np.array([hex_to_rgb(f["hex"]) for f in filaments])
    n = len(phys)
    recipes, cand_rgb = build_candidates(phys, settings.recipe_len, settings.max_run, correction,
                                         dual=settings.dual, backing=settings.backing, walls=settings.colored_walls)
    cand_ok = rgb_to_oklab(cand_rgb)
    if settings.no_dither:
        chosen = []
    elif exact_rgb is not None:
        # one identity per distinct source colour
        chosen = sorted({int(j) for j in np.argmin(_dist(rgb_to_oklab(exact_rgb), cand_ok), axis=1) if j >= n})
    else:
        chosen = select_virtual(targets, cand_ok, n, settings.max_colors - n, auto=settings.capacity == 0,
                                perceptual=settings.precise)
    idx = list(range(n)) + chosen
    entry_ok = cand_ok[idx]
    labels = assign_faces(targets.face_ok, entry_ok, n, settings.texture_optimization,
                          targets.face_lab if settings.precise else None)
    if settings.color_threshold > 0 and adjacency is not None:
        labels = apply_color_threshold(labels, entry_ok, adjacency, face_area, settings.color_threshold)
    if settings.lock_colors:
        locked = lock_physical(targets.face_lab, rgb_to_lab(phys))
        labels = np.where(locked >= 0, locked, labels)
    used = np.zeros(len(idx), dtype=bool)
    used[np.unique(labels)] = True
    used[:n] = True
    remap = -np.ones(len(idx), dtype=np.int32)
    remap[used] = np.arange(used.sum())
    keep = np.nonzero(used)[0]
    row = Row(title=title, filaments=filaments, recipes=[recipes[idx[i]] for i in keep],
              entry_rgb=cand_rgb[idx][keep], labels=remap[labels], family=family)
    row.entry_slicer = slicer_colors(row.recipes, phys, settings.colored_walls)
    row.metrics = score(targets.face_lab, rgb_to_lab(row.entry_rgb), row.labels, targets.score_weights)
    row.metrics["virtual_used"] = int(len(keep) - n)
    frac = np.zeros((len(row.recipes), n))
    for i, r in enumerate(row.recipes):
        for slot, c in r.counts.items():
            frac[i, slot - 1] = c / len(r.word)
    use = np.bincount(row.labels, weights=targets.weights, minlength=len(row.recipes)) @ frac
    row.metrics["usage"] = [round(float(u), 1) for u in 100 * use / max(use.sum(), 1e-12)]
    row.metrics["capacity"] = int(settings.capacity)
    row.metrics["candidates"] = int(len(recipes) - n)
    row.metrics["dual"] = settings.dual
    row.metrics["calibrated"] = correction is not None
    # print-time proxy: every filament of a recipe costs its own paths on each layer
    n_fil = np.array([len(set(r.word) | set(r.inner or ())) for r in row.recipes])
    area = np.bincount(row.labels, weights=targets.weights if face_area is None else face_area,
                       minlength=len(row.recipes))
    single = n_fil == 1
    row.metrics["time"] = {"single": round(float(area[single].sum()), 3),
                           "mixed": round(float((area * n_fil)[~single].sum()), 3),
                           "mixed_share": round(float(100 * area[~single].sum() / max(area.sum(), 1e-12)), 1)}
    return row


def preset_rows(settings: Settings):
    """(title, filaments, family, extra) for the calibrated CMY families."""
    lib, by_id = load_library()
    return [(p["name"], [by_id[i] for i in p["filaments"]], p["id"],
             {"alternatives": p.get("alternatives", {}), "cmy_brand": p.get("cmy_brand", "")})
            for p in lib["presets"] if len(p["filaments"]) == settings.toolheads]


def _combos(pool: list[int], lib_rgb: np.ndarray, P: int) -> list[tuple[int, ...]]:
    """All P-sets of the pool; two near-identical spools in one set waste a toolhead."""
    sl = rgb_to_lab(lib_rgb[pool])
    close = delta_e2000(sl[:, None, :], sl[None, :, :]) < 12
    pos = {s: i for i, s in enumerate(pool)}
    every = list(combinations(pool, P))
    combos = [c for c in every if not any(close[pos[a], pos[b]] for a, b in combinations(c, 2))]
    return combos or every


def _quick_scores(targets: Targets, combos, lib_rgb, lib_ok) -> np.ndarray:
    """Mean OKLab error against the spools plus their 1:2, 1:1, 2:1 pair mixes."""
    rng = np.random.default_rng(0)
    T = targets.ok[rng.choice(len(targets.ok), size=min(1200, len(targets.ok)), p=targets.w, replace=True)]
    pair_cache = {}

    def pair_mix(a, b):
        if (a, b) not in pair_cache:
            m = mixer_lerp(np.tile(lib_rgb[a], (3, 1)), np.tile(lib_rgb[b], (3, 1)), np.array([1 / 3, 0.5, 2 / 3]))
            pair_cache[(a, b)] = rgb_to_oklab(m)
        return pair_cache[(a, b)]

    scores = np.empty(len(combos))
    for ci, combo in enumerate(combos):
        pal = np.concatenate([lib_ok[list(combo)]] + [pair_mix(a, b) for a, b in combinations(combo, 2)])
        scores[ci] = _dist(T, pal).min(axis=1).mean()
    return scores


def estimate_match(targets: Targets, phys_rgb: np.ndarray, settings: Settings) -> float:
    """Match % of a set with short recipes, scored on the colour bins; close to build_row's."""
    n = len(phys_rgb)
    _, cand_rgb = build_candidates(phys_rgb, min(settings.recipe_len, 12 if n <= 4 else 6), settings.max_run)
    cand_ok = rgb_to_oklab(cand_rgb)
    idx = list(range(n)) if settings.no_dither else \
        list(range(n)) + select_virtual(targets, cand_ok, n, settings.max_colors - n, auto=settings.capacity == 0)
    if settings.precise:
        de = _de_dist(targets.ok, cand_ok[idx]).min(axis=1)
    else:
        near = np.argmin(_dist(targets.ok, cand_ok[idx]), axis=1)
        de = delta_e2000(_lab(targets.ok), rgb_to_lab(cand_rgb[idx])[near])
    return round(float(np.clip(100 - 2.5 * (de * targets.w).sum(), 0, 100)), 1)


def rank_sets(targets: Targets, settings: Settings, owned: list[str] | None = None, top: int = 30,
              progress=lambda m: None, seeds: list[list[str]] = ()) -> list[dict]:
    """Best physical sets from the library, or only from the owned spools.

    Every combination gets a quick score; the best ones are re-scored with real
    recipes and capacity. Precise mode then improves the leaders (and `seeds`,
    e.g. the CMY presets) by swapping single spools. Returns [{"ids", "match"}], best first.
    """
    lib, _ = load_library()
    fil = lib["filaments"]
    lib_rgb = np.array([hex_to_rgb(f["hex"]) for f in fil])
    lib_ok = rgb_to_oklab(lib_rgb)
    P = settings.toolheads
    if owned is not None:
        pool = sorted({i for i, f in enumerate(fil) if f["id"] in set(owned)})
        if len(pool) < P:
            raise ValueError(f"Masz zaznaczone {len(pool)} filamenty, a głowic jest {P}")
    else:
        rng = np.random.default_rng(0)
        pick = rng.choice(len(targets.ok), size=min(4000, len(targets.ok)), p=targets.w, replace=True)
        centers, _ = kmeans2(targets.ok[pick].astype(np.float64), min(12, len(targets.ok)), minit="++", seed=1)
        short = set()
        for row in _dist(centers, lib_ok):
            short.update(np.argsort(row)[:2].tolist())
        # mixing endpoints: lightest, darkest and the Full Spectrum line
        short.add(int(np.argmax(lib_ok[:, 0])))
        short.add(int(np.argmin(lib_ok[:, 0])))
        short.update(i for i, f in enumerate(fil) if f["brand"] == "Snapmaker PLA Full Spectrum")
        pool = sorted(short)[:26]
    if P > 4 and len(pool) > 15:
        # C(26, 8) is 1.5 M sets; keep the spools closest to the model's colours
        near = _dist(targets.ok, lib_ok[pool]).min(axis=0)
        pool = sorted(np.array(pool)[np.argsort(near)[:15]].tolist())
    combos = _combos(pool, lib_rgb, P)
    progress(f"Szybka ocena {len(combos)} zestawów…")
    quick = _quick_scores(targets, combos, lib_rgb, lib_ok)
    best = [combos[i] for i in np.argsort(quick)[:max(top * 2, 200 if settings.precise else 40)]]
    small = targets.coarse(1200 if settings.precise else 700)
    found: dict[frozenset, float] = {}

    def est(combo):
        key = frozenset(combo)
        if key not in found:
            found[key] = estimate_match(small, lib_rgb[list(combo)], settings)
        return found[key]

    for k, combo in enumerate(best):
        progress(f"Dokładna ocena zestawów {k + 1}/{len(best)}…")
        est(combo)
    if settings.precise:
        index = {f["id"]: i for i, f in enumerate(fil)}
        allowed = pool if owned is not None else list(range(len(fil)))
        starts = sorted(found, key=lambda c: -found[c])[:6]
        starts += [frozenset(index[i] for i in ids) for ids in seeds
                   if len(ids) == P and all(i in index for i in ids)]
        for k, start in enumerate(dict.fromkeys(starts)):
            progress(f"Dopracowanie zestawów: wymiana szpul {k + 1}/{len(starts)}…")
            _improve(tuple(start), est, targets, allowed, lib_rgb, lib_ok)
    out = [{"ids": [fil[i]["id"] for i in _slot_order(c, lib_ok)], "match": m} for c, m in found.items()]
    out.sort(key=lambda r: -r["match"])
    return out[:top]


def _improve(combo: tuple[int, ...], est, targets: Targets, allowed: list[int], lib_rgb, lib_ok,
             rounds: int = 4, tries: int = 10):
    """Hill climbing: swap one spool for any allowed one while the estimate improves."""
    lab = rgb_to_lab(lib_rgb)
    cur = est(combo)
    for _ in range(rounds):
        swaps = []
        for slot in range(len(combo)):
            rest = combo[:slot] + combo[slot + 1:]
            far = delta_e2000(lab[allowed][:, None, :], lab[list(rest)][None, :, :]).min(axis=1) >= 12
            swaps += [rest + (j,) for j, ok in zip(allowed, far) if ok and j not in combo]
        if not swaps:
            return
        quick = _quick_scores(targets, swaps, lib_rgb, lib_ok)
        scored = [(est(swaps[i]), swaps[i]) for i in np.argsort(quick)[:tries]]
        m, c = max(scored)
        if m <= cur:
            return
        combo, cur = c, m


def _slot_order(combo, lib_ok) -> list[int]:
    """Darker colours first, the lightest one in the last slot (like CMY+White)."""
    cs = sorted(combo, key=lambda i: -lib_ok[i, 0])
    return cs[1:] + cs[:1]


def diverse(ranking: list[dict], n: int, P: int, taken: list[list[str]] = ()) -> list[dict]:
    """Best sets that differ from each other and from `taken` in at least two spools.
    Each result's "grouped" counts the lower-ranked sets it stands in for."""
    out = []
    for r in ranking:
        near = [o for o in out if len(set(r["ids"]) & set(o["ids"])) >= P - 1]
        if near:
            near[0]["grouped"] += 1
        elif len(out) < n and not any(len(set(r["ids"]) & set(o)) >= P - 1 for o in taken):
            out.append({**r, "grouped": 0})
    return out
