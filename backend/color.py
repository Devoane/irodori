"""Color math: sRGB/OKLab/CIELAB conversions, CIEDE2000 and the FilamentMixer model.

FilamentMixer is a port of Snapmaker Orca's `filament_mixer_model.h`
(MIT, Justin Hayes), so predicted mixes match the slicer's own preview.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

_COEF = np.load(Path(__file__).resolve().parent.parent / "data" / "filament_mixer_coef.npz")
_POWERS = _COEF["powers"].astype(np.int64)
_W = _COEF["coef"]
_B = _COEF["intercept"]


def hex_to_rgb(h: str) -> np.ndarray:
    h = h.strip().lstrip("#")
    return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], dtype=np.float64)


def rgb_to_hex(rgb) -> str:
    r, g, b = (int(np.clip(round(float(c)), 0, 255)) for c in rgb)
    return f"#{r:02X}{g:02X}{b:02X}"


def mixer_lerp(c1: np.ndarray, c2: np.ndarray, t) -> np.ndarray:
    """Vectorised FilamentMixer lerp. c1, c2: (...,3) in 0..255, t: (...) in 0..1."""
    c1 = np.asarray(c1, dtype=np.float64)
    c2 = np.asarray(c2, dtype=np.float64)
    t = np.asarray(t, dtype=np.float64)
    c1, c2 = np.broadcast_arrays(c1, c2)
    t = np.broadcast_to(t, c1.shape[:-1])
    x = np.concatenate([c1, c2, t[..., None]], axis=-1)  # (...,7)
    flat = x.reshape(-1, 7)
    feats = np.ones((flat.shape[0], _POWERS.shape[0]))
    for j in range(7):
        p = _POWERS[:, j]
        if p.any():
            feats *= flat[:, j:j + 1] ** p[None, :]
    out = feats @ _W + _B
    out = np.clip(np.trunc(out), 0, 255).reshape(c1.shape)
    out = np.where((t <= 0)[..., None], c1, out)
    return np.where((t >= 1)[..., None], c2, out)


def blend_multi(colors: np.ndarray, counts) -> np.ndarray:
    """Blend physical colors weighted by layer counts the way Orca does
    (MixedFilamentManager::blend_color_multi: sequential lerp in slot order)."""
    colors = np.asarray(colors, dtype=np.float64)
    counts = [int(c) for c in counts]
    cur = None
    acc = 0
    for col, n in zip(colors, counts):
        if n <= 0:
            continue
        if cur is None:
            cur, acc = col.copy(), n
            continue
        tot = acc + n
        cur = mixer_lerp(cur, col, n / tot)
        acc = tot
    return cur if cur is not None else np.zeros(3)



def srgb_to_linear(c: np.ndarray) -> np.ndarray:
    c = np.asarray(c, dtype=np.float64) / 255.0
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(c: np.ndarray) -> np.ndarray:
    c = np.clip(np.asarray(c, dtype=np.float64), 0, 1)
    return 255.0 * np.where(c <= 0.0031308, 12.92 * c, 1.055 * c ** (1 / 2.4) - 0.055)


def rgb_to_oklab(rgb: np.ndarray) -> np.ndarray:
    lin = srgb_to_linear(rgb)
    m1 = np.array([[0.4122214708, 0.5363325363, 0.0514459929],
                   [0.2119034982, 0.6806995451, 0.1073969566],
                   [0.0883024619, 0.2817188376, 0.6299787005]])
    m2 = np.array([[0.2104542553, 0.7936177850, -0.0040720468],
                   [1.9779984951, -2.4285922050, 0.4505937099],
                   [0.0259040371, 0.7827717662, -0.8086757660]])
    lms = np.cbrt(lin @ m1.T)
    return lms @ m2.T


def oklab_to_rgb(lab: np.ndarray) -> np.ndarray:
    m2i = np.array([[1.0, 0.3963377774, 0.2158037573],
                    [1.0, -0.1055613458, -0.0638541728],
                    [1.0, -0.0894841775, -1.2914855480]])
    m1i = np.array([[4.0767416621, -3.3077115913, 0.2309699292],
                    [-1.2684380046, 2.6097574011, -0.3413193965],
                    [-0.0041960863, -0.7034186147, 1.7076147010]])
    lms = (np.asarray(lab) @ m2i.T) ** 3
    return linear_to_srgb(lms @ m1i.T)


def rgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    lin = srgb_to_linear(rgb)
    m = np.array([[0.4124564, 0.3575761, 0.1804375],
                  [0.2126729, 0.7151522, 0.0721750],
                  [0.0193339, 0.1191920, 0.9503041]])
    xyz = lin @ m.T / np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 216 / 24389, np.cbrt(xyz), (24389 / 27 * xyz + 16) / 116)
    L = 116 * f[..., 1] - 16
    a = 500 * (f[..., 0] - f[..., 1])
    b = 200 * (f[..., 1] - f[..., 2])
    return np.stack([L, a, b], axis=-1)


def delta_e2000(lab1: np.ndarray, lab2: np.ndarray) -> np.ndarray:
    L1, a1, b1 = np.moveaxis(np.asarray(lab1, dtype=np.float64), -1, 0)
    L2, a2, b2 = np.moveaxis(np.asarray(lab2, dtype=np.float64), -1, 0)
    C1 = np.hypot(a1, b1)
    C2 = np.hypot(a2, b2)
    Cm = (C1 + C2) / 2
    G = 0.5 * (1 - np.sqrt(Cm ** 7 / (Cm ** 7 + 25 ** 7)))
    a1p, a2p = (1 + G) * a1, (1 + G) * a2
    C1p, C2p = np.hypot(a1p, b1), np.hypot(a2p, b2)
    h1p = np.degrees(np.arctan2(b1, a1p)) % 360
    h2p = np.degrees(np.arctan2(b2, a2p)) % 360
    dLp = L2 - L1
    dCp = C2p - C1p
    dh = h2p - h1p
    dh = np.where(dh > 180, dh - 360, np.where(dh < -180, dh + 360, dh))
    dh = np.where(C1p * C2p == 0, 0, dh)
    dHp = 2 * np.sqrt(C1p * C2p) * np.sin(np.radians(dh / 2))
    Lpm = (L1 + L2) / 2
    Cpm = (C1p + C2p) / 2
    hs = h1p + h2p
    hpm = np.where(np.abs(h1p - h2p) > 180, np.where(hs < 360, (hs + 360) / 2, (hs - 360) / 2), hs / 2)
    hpm = np.where(C1p * C2p == 0, hs, hpm)
    T = (1 - 0.17 * np.cos(np.radians(hpm - 30)) + 0.24 * np.cos(np.radians(2 * hpm))
         + 0.32 * np.cos(np.radians(3 * hpm + 6)) - 0.20 * np.cos(np.radians(4 * hpm - 63)))
    dtheta = 30 * np.exp(-(((hpm - 275) / 25) ** 2))
    Rc = 2 * np.sqrt(Cpm ** 7 / (Cpm ** 7 + 25 ** 7))
    Sl = 1 + 0.015 * (Lpm - 50) ** 2 / np.sqrt(20 + (Lpm - 50) ** 2)
    Sc = 1 + 0.045 * Cpm
    Sh = 1 + 0.015 * Cpm * T
    Rt = -np.sin(np.radians(2 * dtheta)) * Rc
    return np.sqrt((dLp / Sl) ** 2 + (dCp / Sc) ** 2 + (dHp / Sh) ** 2 + Rt * (dCp / Sc) * (dHp / Sh))
