"""Colour calibration from a printed and photographed card.

The card shows a grid of recipes. From a photo we fit a mixture model (Scheffé
quadratic in layer fractions, per OKLab channel). Its terms belong to single
spools and pairs of spools, so any set whose pairs were measured can reuse them.
"""
from __future__ import annotations

import json
from itertools import combinations
from pathlib import Path

import numpy as np

from color import delta_e2000, linear_to_srgb, oklab_to_rgb, rgb_to_lab, rgb_to_oklab, srgb_to_linear
from recipes import predict_many

CAL_DIR = Path(__file__).resolve().parent.parent / "data" / "calibration"
MAX_HELDOUT_DE = 8.0    # a worse fit means a bad photo or misplaced corners, not a difficult filament


def acceptable(info: dict) -> bool:
    return info["heldout_de"] < min(info["model_de"], MAX_HELDOUT_DE)


def set_key(filament_ids: list[str]) -> str:
    return "__".join(sorted(filament_ids)).replace("/", "_")


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


def white_balance(measured_rgb: np.ndarray, nominal_rgb: np.ndarray, pure_idx: list[int]) -> np.ndarray:
    """Exposure and white balance only: one linear-light gain per channel, fitted on the pure
    tiles. A richer map would pull each spool onto its catalogue colour and hide what it really is."""
    pairs = [(p, i) for i, p in enumerate(pure_idx) if p >= 0]
    if not pairs:
        return measured_rgb
    m = srgb_to_linear(measured_rgb)
    src, dst = m[[p for p, _ in pairs]], srgb_to_linear(nominal_rgb[[i for _, i in pairs]])
    gain = (src * dst).sum(axis=0) / np.maximum((src * src).sum(axis=0), 1e-9)
    return np.clip(linear_to_srgb(np.clip(m * gain, 0, 1)), 0, 255)


def _de(rgb_a: np.ndarray, rgb_b: np.ndarray) -> np.ndarray:
    return delta_e2000(rgb_to_lab(rgb_a), rgb_to_lab(rgb_b))


def _heldout_de(X: np.ndarray, meas_ok: np.ndarray, meas_rgb: np.ndarray) -> float:
    """Mean ΔE on each tile predicted by a model fitted without it."""
    pred = np.zeros_like(meas_ok)
    for i in range(len(X)):
        keep = np.arange(len(X)) != i
        c, *_ = np.linalg.lstsq(X[keep], meas_ok[keep], rcond=None)
        pred[i] = X[i] @ c
    return float(_de(np.clip(oklab_to_rgb(pred), 0, 255), meas_rgb).mean())


def fit(fractions: np.ndarray, measured_rgb: np.ndarray, nominal_rgb: np.ndarray,
        pure_idx: list[int]) -> tuple[np.ndarray, dict]:
    """fractions (T, n) per tile, measured_rgb (T, 3) from the photo, nominal_rgb (n, 3)
    filament colours, pure_idx[i] = tile showing pure filament i (or -1)."""
    balanced = white_balance(measured_rgb, nominal_rgb, pure_idx)
    meas_ok = rgb_to_oklab(balanced)
    X = _features(fractions)
    coef, *_ = np.linalg.lstsq(X, meas_ok, rcond=None)
    resid = np.linalg.norm(X @ coef - meas_ok, axis=1)
    model_de = float(_de(predict_many(fractions, nominal_rgb), balanced).mean())
    return coef, {
        "tiles": int(len(fractions)),
        "fit_error_oklab": round(float(resid.mean()), 4),
        "heldout_de": round(_heldout_de(X, meas_ok, balanced), 2),
        "model_de": round(model_de, 2),
        "spool_de": [round(float(_de(balanced[[p]], nominal_rgb[[i]])[0]), 1) if p >= 0 else None
                     for i, p in enumerate(pure_idx)],
    }


def save(filament_ids: list[str], coef: np.ndarray, info: dict, fractions: np.ndarray, measured_rgb: np.ndarray):
    CAL_DIR.mkdir(parents=True, exist_ok=True)
    data = {"filaments": filament_ids, "coef": coef.tolist(), "info": info,
            "fractions": fractions.tolist(), "measured_rgb": measured_rgb.tolist()}
    (CAL_DIR / f"{set_key(filament_ids)}.json").write_text(json.dumps(data, indent=1), encoding="utf-8")


def _terms() -> dict:
    """Model terms by spool id and by sorted pair of ids; the newest calibration wins."""
    terms = {}
    for p in sorted(CAL_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime):
        try:
            j = json.loads(p.read_text(encoding="utf-8"))
            ids, coef = j["filaments"], j["coef"]
        except (ValueError, KeyError):
            continue
        keys = [(i,) for i in ids] + [tuple(sorted(c)) for c in combinations(ids, 2)]
        if len(keys) == len(coef):
            terms.update(zip(keys, coef))
    return terms


def load_correction(filament_ids: list[str]) -> Correction | None:
    """Model for these spools in this order, or None unless every spool and pair was measured."""
    terms = _terms()
    keys = [(i,) for i in filament_ids] + [tuple(sorted(c)) for c in combinations(filament_ids, 2)]
    if not all(k in terms for k in keys):
        return None
    return Correction(np.array([terms[k] for k in keys]))


def list_calibrations() -> list[dict]:
    out = []
    for p in sorted(CAL_DIR.glob("*.json")):
        try:
            j = json.loads(p.read_text(encoding="utf-8"))
            out.append({"filaments": j["filaments"], "info": j.get("info", {})})
        except ValueError:
            pass
    return out
