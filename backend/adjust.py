"""Source colour adjustments applied before matching: brightness, saturation, rainbow.

frontend/app.js mirrors these formulas for the instant preview.
"""
from __future__ import annotations

import numpy as np

from color import oklab_to_rgb, rgb_to_oklab


def adjust_colors(rgb: np.ndarray, brightness: float = 0, saturation: float = 0) -> np.ndarray:
    """brightness / saturation in percent (-100..100), in OKLab: lightness moves
    toward white or black, chroma scales."""
    if not brightness and not saturation:
        return rgb
    lab = rgb_to_oklab(rgb)
    b = brightness / 100
    lab[:, 0] = np.where(b > 0, lab[:, 0] + b * (1 - lab[:, 0]), lab[:, 0] * (1 + b))
    lab[:, 1:] *= 1 + saturation / 100
    return np.clip(oklab_to_rgb(lab), 0, 255)


def hsv_rgb(h: np.ndarray, s: float = 1.0, v: float = 1.0) -> np.ndarray:
    """h in degrees -> sRGB 0..255."""
    h = np.asarray(h, dtype=np.float64)
    k = np.mod(np.array([5, 3, 1])[None, :] + h[:, None] / 60, 6)
    return 255 * (v - v * s * np.clip(np.minimum(k, 4 - k), 0, 1))


def rainbow_colors(points: np.ndarray, vertical: float = 1.0, horizontal: float = 1.0, ccw: bool = False,
                   offset: float = 0.0, white: float = 0.0) -> np.ndarray:
    """Rainbow swirl around the vertical axis of the model: hue turns with the
    angle, with the distance from the axis (spiral) and with height."""
    lo, hi = points.min(axis=0), points.max(axis=0)
    c = (lo + hi) / 2
    dx, dy = points[:, 0] - c[0], points[:, 1] - c[1]
    r = np.hypot(dx, dy)
    ang = np.degrees(np.arctan2(dy, dx))
    hue = offset + (1 if ccw else -1) * ang + 360 * horizontal * r / max(float(r.max()), 1e-9) \
        + 360 * vertical * (points[:, 2] - lo[2]) / max(float(hi[2] - lo[2]), 1e-9)
    rgb = hsv_rgb(hue)
    return rgb + (255 - rgb) * np.clip(white, 0, 75) / 100
