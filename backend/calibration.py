"""Per filament-set colour calibration from a printed and photographed card.

The card shows a grid of recipes. From a photo we fit a mixture model
(Scheffé quadratic in layer fractions, per OKLab channel) and map the camera
to filament colours using the pure endpoint tiles.
"""
from __future__ import annotations

import json
from itertools import combinations
from pathlib import Path

import numpy as np

from color import oklab_to_rgb, rgb_to_oklab

CAL_DIR = Path(__file__).resolve().parent.parent / "data" / "calibration"


def set_key(filament_ids: list[str]) -> str:
    return "__".join(filament_ids).replace("/", "_")


def _features(fr: np.ndarray) -> np.ndarray:
    n = fr.shape[1]
    cols = [fr[:, i] for i in range(n)]
    cols += [fr[:, i] * fr[:, j] for i, j in combinations(range(n), 2)]
    return np.stack(cols, axis=1)


class Correction:
    """Fitted mixture model: layer fractions -> OKLab of the printed wall."""

    def __init__(self, coef: np.ndarray):
        self.coef = coef          # (features, 3)

    def apply_many(self, counts: np.ndarray) -> np.ndarray:
        fr = counts / counts.sum(axis=1, keepdims=True)
        return np.clip(oklab_to_rgb(_features(fr) @ self.coef), 0, 255)


def fit(fractions: np.ndarray, measured_rgb: np.ndarray, nominal_rgb: np.ndarray,
        pure_idx: list[int]) -> tuple[np.ndarray, dict]:
    """fractions (T, n) per tile, measured_rgb (T, 3) from the photo, nominal_rgb (n, 3)
    filament colours, pure_idx[i] = tile showing pure filament i (or -1)."""
    meas_ok = rgb_to_oklab(measured_rgb)
    # camera -> filament space: affine map fitted on the pure endpoint tiles
    pi = [p for p in pure_idx if p >= 0]
    if len(pi) >= 3:
        A = np.c_[meas_ok[pi], np.ones(len(pi))]
        B = rgb_to_oklab(nominal_rgb[[i for i, p in enumerate(pure_idx) if p >= 0]])
        M, *_ = np.linalg.lstsq(A, B, rcond=None)
        meas_ok = np.c_[meas_ok, np.ones(len(meas_ok))] @ M
    X = _features(fractions)
    coef, *_ = np.linalg.lstsq(X, meas_ok, rcond=None)
    resid = np.linalg.norm(X @ coef - meas_ok, axis=1)
    return coef, {"tiles": int(len(fractions)), "fit_error_oklab": round(float(resid.mean()), 4)}


def save(filament_ids: list[str], coef: np.ndarray, info: dict):
    CAL_DIR.mkdir(parents=True, exist_ok=True)
    data = {"filaments": filament_ids, "coef": coef.tolist(), "info": info}
    (CAL_DIR / f"{set_key(filament_ids)}.json").write_text(json.dumps(data, indent=1), encoding="utf-8")


def load_correction(filament_ids: list[str]) -> Correction | None:
    p = CAL_DIR / f"{set_key(filament_ids)}.json"
    if not p.exists():
        return None
    try:
        return Correction(np.array(json.loads(p.read_text(encoding="utf-8"))["coef"]))
    except (ValueError, KeyError):
        return None


def list_calibrations() -> list[dict]:
    out = []
    for p in sorted(CAL_DIR.glob("*.json")):
        try:
            j = json.loads(p.read_text(encoding="utf-8"))
            out.append({"filaments": j["filaments"], "info": j.get("info", {})})
        except ValueError:
            pass
    return out
