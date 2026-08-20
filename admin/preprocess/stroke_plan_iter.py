"""
Iterative stroke planning for Photo→Sketch (feature/photo-sketch only).

Does NOT modify adaptive_source / soft source generation / brushes.
Uses frozen soft maps + photo tone for modeling strokes.

  PYTHONPATH=admin/preprocess:admin/preprocess/vendor \\
    python3 admin/preprocess/stroke_plan_iter.py --variant v3
"""

from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

import cv2
import numpy as np

from soft_paths import (  # type: ignore
    classify_region,
    douglas_peucker,
    follow_ridges,
    light_join,
    nms_ridges,
    path_length,
    resample,
    ridge_mask,
    soft_level_contours,
    soft_to_strength,
    to_norm,
)

Point = Tuple[float, float]
PathPts = List[Point]

ROOT = Path(".")
SOFT_DIR = ROOT / "tmp/iter-stroke/soft"
PHOTO_DIR = ROOT / "tmp/photo-sketch-testset"
OUT_DIR = ROOT / "tmp/iter-stroke"

MOTIFS = ("duo-council", "painter-studio", "heron-pond")

# v6 is the frozen darkness/budget baseline. New work starts at v8+.
FROZEN_BASELINE = "v6"


def ver(variant: str) -> int:
    """Numeric variant id (v10 → 10). Avoids broken string compares like 'v10' < 'v8'."""
    try:
        return int(str(variant).lstrip("vV"))
    except ValueError:
        return 0


def tone_band(v: float) -> str:
    if v < 0.18:
        return "very_light"
    if v < 0.32:
        return "light"
    if v < 0.48:
        return "light_mid"
    if v < 0.62:
        return "mid"
    if v < 0.78:
        return "mid_dark"
    return "very_dark"


def structure_occupancy(strokes: List[dict], w: int, h: int, thickness: int = 2) -> np.ndarray:
    ink = np.zeros((h, w), np.float32)
    for s in strokes:
        pts = s["points"]
        if not pts:
            continue
        if pts[0][0] <= 1.5 and pts[0][1] <= 1.5:
            pix = [(p[0] * w, p[1] * h) for p in pts]
        else:
            pix = pts
        arr = np.array([[int(round(x)), int(round(y))] for x, y in pix], np.int32)
        if len(arr) >= 2:
            cv2.polylines(ink, [arr], False, 1.0, thickness, cv2.LINE_AA)
    return cv2.GaussianBlur(ink, (0, 0), max(1.5, min(h, w) * 0.003))


def mean_tone_along(pts: PathPts, dark: np.ndarray, w: int, h: int) -> float:
    vals = []
    for x, y in pts[:: max(1, len(pts) // 24)]:
        xi, yi = int(round(x)), int(round(y))
        if 0 <= xi < w and 0 <= yi < h:
            vals.append(float(dark[yi, xi]))
    return float(np.mean(vals)) if vals else 0.3


def local_texture_density(pts: PathPts, ridge_density: np.ndarray, w: int, h: int) -> float:
    vals = []
    for x, y in pts[:: max(1, len(pts) // 16)]:
        xi, yi = int(round(x)), int(round(y))
        if 0 <= xi < w and 0 <= yi < h:
            vals.append(float(ridge_density[yi, xi]))
    return float(np.mean(vals)) if vals else 0.0



def compute_texture_density(soft: np.ndarray, dark: np.ndarray) -> np.ndarray:
    """
    Robust local texture density in [0,1].
    High = many short edges, direction churn, local contrast, dense structure.
    Generic — no semantic classes.
    """
    h, w = soft.shape
    short = min(h, w)
    soft_f = soft.astype(np.float32)
    # high-frequency residual
    blur = cv2.GaussianBlur(soft_f, (0, 0), max(1.2, short * 0.004))
    hi = np.abs(soft_f - blur)
    hi = hi / (float(np.percentile(hi, 95)) + 1e-6)
    # soft edge magnitude
    gx = cv2.Sobel(soft_f, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(soft_f, cv2.CV_32F, 0, 1, ksize=3)
    gmag = cv2.magnitude(gx, gy)
    gmag_n = gmag / (float(np.percentile(gmag, 95)) + 1e-6)
    # local edge density (binary strong edges in neighborhood)
    edge = (gmag_n > 0.35).astype(np.float32)
    k = max(5, int(round(short * 0.018)) | 1)
    edge_den = cv2.blur(edge, (k, k))
    # orientation churn: local circular variance of gradient angles
    ang = np.arctan2(gy, gx)
    c = np.cos(2 * ang).astype(np.float32)
    s = np.sin(2 * ang).astype(np.float32)
    # weight by edge strength so flat areas don't look "churny"
    wt = np.clip(gmag_n, 0, 1)
    c_b = cv2.blur(c * wt, (k, k)) / (cv2.blur(wt, (k, k)) + 1e-6)
    s_b = cv2.blur(s * wt, (k, k)) / (cv2.blur(wt, (k, k)) + 1e-6)
    R = np.clip(np.sqrt(c_b * c_b + s_b * s_b), 0, 1)
    churn = (1.0 - R) * np.clip(edge_den * 2.0, 0, 1)
    # photo local contrast
    dblur = cv2.GaussianBlur(dark, (0, 0), max(1.5, short * 0.005))
    contrast = np.abs(dark - dblur)
    contrast = contrast / (float(np.percentile(contrast, 95)) + 1e-6)
    tex = 0.30 * np.clip(hi, 0, 1.5) + 0.30 * np.clip(edge_den * 1.8, 0, 1.5)
    tex += 0.25 * churn + 0.15 * np.clip(contrast, 0, 1.5)
    tex = cv2.GaussianBlur(tex.astype(np.float32), (0, 0), max(1.0, short * 0.003))
    tex = tex / (float(np.percentile(tex, 96)) + 1e-6)
    return np.clip(tex, 0, 1).astype(np.float32)


def tex_band(t: float) -> str:
    if t < 0.33:
        return "low"
    if t < 0.58:
        return "mid"
    return "high"


def candidate_crowd_map(prepared: List[dict], h: int, w: int, short: float) -> np.ndarray:
    """How many candidate midpoints fall near each pixel (proxy for competing strokes)."""
    m = np.zeros((h, w), np.float32)
    r = max(3, int(short * 0.012))
    for c in prepared:
        mx, my = c["mid"]
        xi, yi = int(round(mx)), int(round(my))
        if 0 <= xi < w and 0 <= yi < h:
            cv2.circle(m, (xi, yi), r, 1.0, -1)
    m = cv2.GaussianBlur(m, (0, 0), max(2.0, short * 0.01))
    return m / (float(m.max()) + 1e-6)


def selection_texture_stats(kept: List[dict], prepared: List[dict], fields: dict, cell: int, budget: np.ndarray) -> dict:
    """Metrics for texture-vs-form prioritization."""
    if not kept:
        return {"avgTex": 0, "texShare": {"low": 0, "mid": 0, "high": 0}}
    texes = [float(s.get("texDens", s.get("tex", 0))) for s in kept]
    share = {"low": 0, "mid": 0, "high": 0}
    for t in texes:
        share[tex_band(t)] += 1
    n = len(kept)
    # cell occupancy
    gh, gw = budget.shape
    used = np.zeros((gh, gw), np.int32)
    for s in kept:
        mx, my = s["mid"]
        cx = min(gw - 1, max(0, int(mx // cell)))
        cy = min(gh - 1, max(0, int(my // cell)))
        used[cy, cx] += 1
    # vs naive soft-strength ranking: how many high-tex displaced / low-tex gained
    naive = sorted(prepared, key=lambda c: (-c.get("strength", 0), -c.get("length", 0)))[:n]
    naive_ids = {(round(c["mid"][0], 1), round(c["mid"][1], 1)) for c in naive}
    kept_ids = {(round(s["mid"][0], 1), round(s["mid"][1], 1)) for s in kept}
    gained = [c for c in kept if (round(c["mid"][0], 1), round(c["mid"][1], 1)) not in naive_ids]
    displaced = [c for c in naive if (round(c["mid"][0], 1), round(c["mid"][1], 1)) not in kept_ids]
    midtone_gained = sum(1 for c in gained if c.get("midPref", 0) >= 0.4 and c.get("texDens", c.get("tex", 1)) < 0.45)
    high_tex_displaced = sum(1 for c in displaced if c.get("texDens", c.get("tex", 0)) >= 0.58)
    return {
        "avgTex": round(float(np.mean(texes)), 3),
        "texShare": {k: round(100.0 * v / n, 1) for k, v in share.items()},
        "texCounts": share,
        "cellOccMean": round(float(used[used > 0].mean()) if np.any(used > 0) else 0, 2),
        "cellOccMax": int(used.max()) if used.size else 0,
        "cellsUsed": int(np.count_nonzero(used)),
        "midtonePreferredVsNaive": midtone_gained,
        "highTexDisplacedVsNaive": high_tex_displaced,
        "gainedVsNaive": len(gained),
        "displacedVsNaive": len(displaced),
    }


def path_mid(pts: PathPts) -> Point:
    return pts[len(pts) // 2]


def mean_along(pts: PathPts, field: np.ndarray, w: int, h: int, step: int = 20) -> float:
    vals = []
    for x, y in pts[:: max(1, len(pts) // step)]:
        xi, yi = int(round(x)), int(round(y))
        if 0 <= xi < w and 0 <= yi < h:
            vals.append(float(field[yi, xi]))
    return float(np.mean(vals)) if vals else 0.0


def path_dir(pts: PathPts) -> Tuple[float, float]:
    if len(pts) < 2:
        return 1.0, 0.0
    dx = pts[-1][0] - pts[0][0]
    dy = pts[-1][1] - pts[0][1]
    n = math.hypot(dx, dy) + 1e-6
    return dx / n, dy / n


def apply_dark_caps(st: float, tone: float, kind: str, tex: float, L: float, short: float, variant: str) -> float:
    """Frozen v6 dark-budget strength caps (also used by v8+)."""
    if tone >= 0.72:
        st = min(st, 0.24)
    elif tone >= 0.55:
        st = min(st, 0.32)
    if kind == "outline":
        st = min(st, 0.20)
    if tex > 0.55 and L < short * 0.06:
        st = min(st, 0.22)
    return st


def dark_cell_budget(dark: np.ndarray, ridge_density: np.ndarray, short: int, variant: str) -> Tuple[np.ndarray, int]:
    """v6-style local saturation budget. v8+ keeps the principle, mid cells open for form."""
    h, w = dark.shape
    cell = max(22, int(short * 0.038))
    gh, gw = math.ceil(h / cell), math.ceil(w / cell)
    base = 4 if ver(variant) >= 6 else 5
    budget = np.full((gh, gw), base, dtype=np.int32)
    for cy in range(gh):
        for cx in range(gw):
            y0, x0 = cy * cell, cx * cell
            patch = dark[y0 : min(h, y0 + cell), x0 : min(w, x0 + cell)]
            m = float(patch.mean()) if patch.size else 0
            tpatch = ridge_density[y0 : min(h, y0 + cell), x0 : min(w, x0 + cell)]
            tmean = float(tpatch.mean()) if tpatch.size else 0
            if m > 0.72 or tmean > 0.62:
                budget[cy, cx] = 1
            elif m > 0.58:
                budget[cy, cx] = 2
            elif 0.32 <= m <= 0.58:
                # midtone cells: allow more structure slots for form (v8+)
                budget[cy, cx] = 7 if ver(variant) >= 8 else 5
            elif m < 0.28:
                budget[cy, cx] = 4
    return budget, cell


def select_with_budget(
    prepared: List[dict],
    budget: np.ndarray,
    cell: int,
    *,
    max_struct: int,
    second_budget: int = 2,
) -> List[dict]:
    """Greedy keep with dark-cell budgets; second-pass practically off (≤2)."""
    gh, gw = budget.shape
    prepared = sorted(prepared, key=lambda s: (-s["score"], -s.get("length", 0)))
    kept: List[dict] = []
    used = np.zeros_like(budget)
    seconds = 0
    for s in prepared:
        pts = s["points"]
        mid = path_mid(pts)
        cx = min(gw - 1, max(0, int(mid[0] // cell)))
        cy = min(gh - 1, max(0, int(mid[1] // cell)))
        lim = budget[cy, cx] + (1 if s.get("kind") == "outline" else 0)
        if used[cy, cx] >= lim:
            continue
        used[cy, cx] += 1
        allow2 = False
        if (
            seconds < second_budget
            and 0.34 <= s.get("localTone", 0) <= 0.62
            and 0.32 <= s.get("strength", 0) <= 0.48
            and s.get("tex", 0) < 0.45
            and s.get("score", 0) >= 0.35
            and used[cy, cx] <= 2
        ):
            allow2 = True
            seconds += 1
        s2 = dict(s)
        s2["allowSecondPass"] = bool(allow2)
        kept.append(s2)
        if len(kept) >= max_struct:
            break
    return kept


def post_thin_overink(kept: List[dict], w: int, h: int, score_floor: float = 0.28) -> List[dict]:
    if not kept:
        return kept
    occ = structure_occupancy(kept, w, h, thickness=2)
    occ_n = occ / (occ.max() + 1e-6)
    thinned = []
    for s in kept:
        mid = path_mid(s["points"])
        xi, yi = int(round(mid[0])), int(round(mid[1]))
        o = float(occ_n[yi, xi]) if 0 <= yi < h and 0 <= xi < w else 0
        if o > 0.72 and s.get("score", 0) < score_floor and s.get("kind") != "feature":
            continue
        thinned.append(s)
    return thinned if len(thinned) >= int(0.65 * len(kept)) else kept


def prepare_candidates(
    strokes: List[dict], dark: np.ndarray, soft: np.ndarray, variant: str
) -> Tuple[List[dict], dict]:
    """Shared feature fields for selection strategies."""
    h, w = dark.shape
    short = min(h, w)
    gx = cv2.Sobel(dark, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(dark, cv2.CV_32F, 0, 1, ksize=3)
    gmag = cv2.magnitude(gx, gy)
    gmag = gmag / (float(np.percentile(gmag, 95)) + 1e-6)
    soft_dark = (255.0 - soft.astype(np.float32)) / 255.0
    soft_mid = ((soft_dark > 0.18) & (soft_dark < 0.70)).astype(np.float32)
    soft_mid_blur = cv2.GaussianBlur(soft_mid, (0, 0), max(2.0, short * 0.01))
    ink = (dark > 0.35).astype(np.float32)
    ridge_density = cv2.GaussianBlur(ink, (0, 0), max(2.0, short * 0.008))
    ridge_density = ridge_density / (float(ridge_density.max()) + 1e-6)
    ang = np.arctan2(gy, gx)
    texture_density = compute_texture_density(soft, dark) if ver(variant) >= 11 else ridge_density
    # Region texture: broader neighborhood (patterned cloth vs calm cheek)
    region_tex = (
        cv2.GaussianBlur(texture_density, (0, 0), max(3.0, short * 0.022))
        if ver(variant) >= 11
        else ridge_density
    )

    prepared: List[dict] = []
    for s in strokes:
        pts = s["points"]
        if len(pts) < 2:
            continue
        tone = mean_tone_along(pts, dark, w, h)
        L = float(s.get("length") or path_length(pts))
        if ver(variant) >= 5 and tone >= 0.75 and L < short * 0.04:
            continue
        st = float(s.get("strength") or 0.4)
        kind = s.get("kind", "other")
        tex_legacy = local_texture_density(pts, ridge_density, w, h)
        # Prefer region texture over on-ridge texture (ridges are always "busy")
        tex_dens = mean_along(pts, region_tex, w, h) if ver(variant) >= 11 else tex_legacy
        tex = tex_dens if ver(variant) >= 11 else tex_legacy
        st = apply_dark_caps(st, tone, kind, tex, L, short, variant)
        mid = path_mid(pts)
        xi, yi = int(round(mid[0])), int(round(mid[1]))
        info = float(gmag[yi, xi]) if 0 <= yi < h and 0 <= xi < w else 0.2
        soft_cov = mean_along(pts, soft_mid_blur, w, h)
        soft_ink = mean_along(pts, soft_dark, w, h)
        dx, dy = path_dir(pts)
        local_ang = float(ang[yi, xi]) if 0 <= yi < h and 0 <= xi < w else 0.0
        path_ang = math.atan2(dy, dx)
        align = abs(math.cos(path_ang - local_ang))
        form_align = abs(math.sin(path_ang - local_ang))
        mid_pref = 1.0 - abs(tone - 0.48) * 1.35
        soft_mid_pref = 1.0 - abs(soft_ink - 0.42) * 1.55
        if ver(variant) >= 8:
            mid_pref = 0.35 * max(0.05, mid_pref) + 0.65 * max(0.05, soft_mid_pref)
        length_pref = float(np.clip(L / (short * 0.12), 0.25, 1.45))
        # local coherence proxy: form alignment × soft mid × inverse texture
        coherence = (0.35 + 0.65 * form_align) * (0.4 + 0.6 * soft_cov) * (1.0 - 0.55 * tex)
        prepared.append(
            {
                **s,
                "strength": round(st, 3),
                "localTone": round(tone, 3),
                "toneBand": tone_band(tone),
                "tex": round(float(tex), 3),
                "texDens": round(float(tex_dens), 3),
                "texBand": tex_band(float(tex_dens)),
                "info": round(info, 3),
                "softCov": round(soft_cov, 3),
                "softInk": round(soft_ink, 3),
                "midPref": round(float(max(0.05, mid_pref)), 3),
                "lengthPref": round(length_pref, 3),
                "formAlign": round(form_align, 3),
                "align": round(align, 3),
                "coherence": round(float(coherence), 3),
                "dir": (dx, dy),
                "mid": mid,
            }
        )
    # crowding among candidates (v11+) — used in score, not mixed into texBand
    if ver(variant) >= 11 and prepared:
        crowd = candidate_crowd_map(prepared, h, w, short)
        for c in prepared:
            mx, my = c["mid"]
            xi, yi = int(round(mx)), int(round(my))
            cr = float(crowd[yi, xi]) if 0 <= yi < h and 0 <= xi < w else 0.0
            c["crowd"] = round(cr, 3)
            c["texBand"] = tex_band(c["texDens"])
    fields = {
        "gmag": gmag,
        "soft_dark": soft_dark,
        "soft_mid_blur": soft_mid_blur,
        "ridge_density": ridge_density,
        "texture_density": texture_density,
        "region_tex": region_tex,
        "ang": ang,
        "short": short,
        "h": h,
        "w": w,
    }
    return prepared, fields


def score_v6_style(c: dict) -> float:
    """Frozen v6 ranking (for baseline reproducibility)."""
    tex_pen = 1.0 - 0.55 * c["tex"]
    score = c["strength"] * (0.45 + 0.55 * c["midPref"]) * (0.55 + 0.45 * min(1.0, c["info"]))
    score *= c["lengthPref"] * max(0.25, tex_pen)
    if c.get("kind") == "outline":
        score *= 0.72
    return score


def score_v8_info_gain(c: dict, occ_n: np.ndarray, w: int, h: int) -> float:
    """
    v8: prefer soft-supported midtone form that lands in under-covered areas.
    Busy short texture strongly demoted.
    """
    mx, my = c["mid"]
    xi, yi = int(round(mx)), int(round(my))
    occ = float(occ_n[yi, xi]) if 0 <= yi < h and 0 <= xi < w else 0.0
    novelty = 1.0 - 0.85 * occ
    tex_pen = 1.0 - 0.75 * c["tex"]
    # reward soft midtone support even when soft strength is weak
    soft_boost = 0.35 + 0.90 * c["softCov"]
    form = 0.35 + 0.65 * c["formAlign"]
    score = (
        (0.25 + 0.75 * c["midPref"])
        * (0.30 + 0.70 * min(1.0, c["info"]))
        * c["lengthPref"]
        * max(0.15, tex_pen)
        * soft_boost
        * form
        * max(0.2, novelty)
        * (0.55 + 0.45 * c["strength"])
    )
    if c.get("source") == "midtone_ridge":
        score *= 1.35
    if c.get("kind") == "outline":
        score *= 0.70
    # patterned / dark-busy local texture loses to soft mid form
    if c["tex"] > 0.45 and c["localTone"] > 0.52:
        score *= 0.42
    if c["tex"] > 0.5 and c.get("length", 0) < (min(h, w) * 0.05):
        score *= 0.30
    return score


def score_v9_coherence(c: dict, neighbors: List[dict], short: float) -> float:
    """
    v9: boost midtone candidates that form coherent directional clusters.
    """
    tex_pen = 1.0 - 0.70 * c["tex"]
    base = (
        (0.30 + 0.70 * c["midPref"])
        * (0.35 + 0.65 * min(1.0, c["info"]))
        * c["lengthPref"]
        * max(0.18, tex_pen)
        * (0.40 + 0.80 * c["softCov"])
        * (0.40 + 0.60 * c["formAlign"])
    )
    if c.get("source") == "midtone_ridge":
        base *= 1.30
    if c["tex"] > 0.45 and c["localTone"] > 0.52:
        base *= 0.45
    if not neighbors:
        return base * 0.85
    dx, dy = c["dir"]
    coh = 0.0
    for n in neighbors:
        ndx, ndy = n["dir"]
        # same-ish direction
        coh += abs(dx * ndx + dy * ndy)
        # similar midtone
        coh += 0.35 * (1.0 - abs(c["localTone"] - n["localTone"]))
    coh = coh / max(1, len(neighbors))
    score = base * (0.55 + 0.70 * min(1.0, coh))
    if c.get("kind") == "outline":
        score *= 0.70
    if c["tex"] > 0.52 and c.get("length", 0) < short * 0.055:
        score *= 0.28
    return score


def score_v10_residual(c: dict, residual: np.ndarray, w: int, h: int) -> float:
    """
    v10: prioritize soft residual — soft ink present, little structure yet planned nearby.
    """
    mx, my = c["mid"]
    xi, yi = int(round(mx)), int(round(my))
    res = float(residual[yi, xi]) if 0 <= yi < h and 0 <= xi < w else 0.0
    tex_pen = 1.0 - 0.72 * c["tex"]
    score = (
        (0.22 + 0.90 * res)
        * (0.30 + 0.70 * c["midPref"])
        * (0.30 + 0.70 * min(1.0, c["info"]))
        * c["lengthPref"]
        * max(0.16, tex_pen)
        * (0.45 + 0.55 * c["formAlign"])
        * (0.50 + 0.50 * c["softCov"])
    )
    if c.get("source") == "midtone_ridge":
        score *= 1.40
    if c.get("kind") == "outline":
        score *= 0.68
    if c["tex"] > 0.45 and c["localTone"] > 0.52:
        score *= 0.40
    if c["tex"] > 0.5 and c.get("length", 0) < (min(h, w) * 0.05):
        score *= 0.25
    return score


def build_neighbor_index(cands: List[dict], radius: float) -> Dict[int, List[dict]]:
    """Simple spatial hash for coherence neighbors."""
    cell = max(8.0, radius)
    buckets: Dict[Tuple[int, int], List[int]] = {}
    for i, c in enumerate(cands):
        mx, my = c["mid"]
        key = (int(mx // cell), int(my // cell))
        buckets.setdefault(key, []).append(i)
    out: Dict[int, List[dict]] = {}
    r2 = radius * radius
    for i, c in enumerate(cands):
        mx, my = c["mid"]
        cx, cy = int(mx // cell), int(my // cell)
        neigh = []
        for ox in (-1, 0, 1):
            for oy in (-1, 0, 1):
                for j in buckets.get((cx + ox, cy + oy), []):
                    if j == i:
                        continue
                    nx, ny = cands[j]["mid"]
                    if (mx - nx) ** 2 + (my - ny) ** 2 <= r2:
                        neigh.append(cands[j])
        out[i] = neigh
    return out


def score_form_texture(
    c: dict, occ_n: np.ndarray, w: int, h: int, *, tex_power: float = 1.6
) -> float:
    """
    Leitidee: Formwert ≈ Soft-Mid × Kohärenz × (1−Textur) × Novelty
    """
    mx, my = c["mid"]
    xi, yi = int(round(mx)), int(round(my))
    occ = float(occ_n[yi, xi]) if 0 <= yi < h and 0 <= xi < w else 0.0
    novelty = 1.0 - 0.88 * occ
    soft_mid = 0.25 + 0.85 * c["midPref"] * (0.45 + 0.55 * c["softCov"])
    coh = 0.35 + 0.65 * c.get("coherence", c["formAlign"])
    tex = float(c.get("texDens", c.get("tex", 0)))
    calm = max(0.08, (1.0 - tex) ** tex_power)
    crowd = float(c.get("crowd", 0))
    # crowded candidate neighborhoods (shirt patterns) get less priority
    space = max(0.2, 1.0 - 0.55 * crowd)
    length = 0.55 + 0.45 * min(1.2, c["lengthPref"])
    score = soft_mid * coh * calm * max(0.15, novelty) * length * space
    score *= 0.55 + 0.45 * min(1.0, c["info"])
    if c.get("kind") == "outline":
        score *= 0.70
    if tex >= 0.58:
        score *= 0.28
    elif tex >= 0.42:
        score *= 0.55
    if c.get("source") == "midtone_ridge" and tex < 0.40:
        score *= 1.30
    return float(score)


def texture_cell_budget(
    dark: np.ndarray, texture: np.ndarray, soft_mid_blur: np.ndarray, short: int
) -> Tuple[np.ndarray, int]:
    """v12: texturreiche Zellen weniger Budget, ruhige Midtone-Zellen mehr."""
    h, w = dark.shape
    cell = max(22, int(short * 0.038))
    gh, gw = math.ceil(h / cell), math.ceil(w / cell)
    budget = np.full((gh, gw), 4, dtype=np.int32)
    for cy in range(gh):
        for cx in range(gw):
            y0, x0 = cy * cell, cx * cell
            y1, x1 = min(h, y0 + cell), min(w, x0 + cell)
            tmean = float(texture[y0:y1, x0:x1].mean())
            dmean = float(dark[y0:y1, x0:x1].mean())
            smid = float(soft_mid_blur[y0:y1, x0:x1].mean())
            # dark-budget principle retained
            if dmean > 0.72:
                budget[cy, cx] = 1
            elif tmean >= 0.58:
                budget[cy, cx] = 1  # high texture: minimal
            elif tmean >= 0.40:
                budget[cy, cx] = 2
            elif smid > 0.35 and 0.22 <= dmean <= 0.62 and tmean < 0.38:
                budget[cy, cx] = 7  # calm midtone form cells
            elif 0.32 <= dmean <= 0.58:
                budget[cy, cx] = 5
            elif dmean < 0.28:
                budget[cy, cx] = 3
            else:
                budget[cy, cx] = 3
    return budget, cell


def remap_structure(
    strokes: List[dict], dark: np.ndarray, soft: np.ndarray, variant: str
) -> Tuple[List[dict], dict]:
    """
    Cap dark overdraw (v6 principle), then select by strategy.
    Returns (kept_strokes, selection_stats).
    """
    h, w = dark.shape
    short = min(h, w)
    prepared, fields = prepare_candidates(strokes, dark, soft, variant)
    ridge_density = fields["ridge_density"]
    budget, cell = dark_cell_budget(dark, ridge_density, short, variant)
    empty_stats: dict = {}

    if ver(variant) < 8:
        for c in prepared:
            c["score"] = round(score_v6_style(c), 4)
        max_struct = {1: 220, 2: 180, 3: 160, 4: 170, 5: 185, 6: 200, 7: 210}.get(ver(variant), 180)
        second = {1: 12, 2: 14, 3: 10, 4: 10, 5: 8, 6: 2, 7: 2}.get(ver(variant), 2)
        if ver(variant) == 6:
            second = 2
        kept = select_with_budget(prepared, budget, cell, max_struct=max_struct, second_budget=second)
        if ver(variant) >= 6:
            kept = post_thin_overink(kept, w, h)
        return kept, empty_stats

    # --- frozen v8–v10 strategies (unchanged behavior) ---
    max_struct = {8: 215, 9: 220, 10: 225, 11: 210, 12: 210, 13: 205}.get(ver(variant), 210)

    if ver(variant) == 8:
        seed = [
            c
            for c in prepared
            if c["lengthPref"] >= 0.7 and c["midPref"] >= 0.45 and c["tex"] < 0.55
        ]
        seed = sorted(seed, key=lambda c: -(c["lengthPref"] * c["midPref"] * (1 - c["tex"])))[:40]
        occ = structure_occupancy(seed, w, h, thickness=2) if seed else np.zeros((h, w), np.float32)
        occ_n = occ / (occ.max() + 1e-6) if float(occ.max()) > 0 else occ
        for c in prepared:
            c["score"] = round(score_v8_info_gain(c, occ_n, w, h), 4)
        soft_mid_pool = [
            c
            for c in prepared
            if 0.18 <= c["softInk"] <= 0.58 and c["tex"] < 0.50 and c["midPref"] >= 0.35
        ]
        soft_mid_pool.sort(key=lambda c: -c["score"])
        reserve = min(int(max_struct * 0.42), len(soft_mid_pool), 95)
        phase_a = select_with_budget(soft_mid_pool[: max(reserve * 2, reserve)], budget, cell, max_struct=reserve, second_budget=0)
        rem_budget = budget.copy()
        for s in phase_a:
            mx, my = s["mid"]
            cx = min(budget.shape[1] - 1, max(0, int(mx // cell)))
            cy = min(budget.shape[0] - 1, max(0, int(my // cell)))
            rem_budget[cy, cx] = max(0, rem_budget[cy, cx] - 1)
        a_mids = [s["mid"] for s in phase_a]
        phase_b_pool = [
            c
            for c in prepared
            if not any(math.hypot(c["mid"][0] - am[0], c["mid"][1] - am[1]) < short * 0.01 for am in a_mids)
        ]
        phase_b = select_with_budget(phase_b_pool, rem_budget, cell, max_struct=max_struct - len(phase_a), second_budget=2)
        kept = post_thin_overink(phase_a + phase_b, w, h, score_floor=0.24)
        return kept, empty_stats

    if ver(variant) == 9:
        neigh = build_neighbor_index(prepared, radius=short * 0.045)
        for i, c in enumerate(prepared):
            c["score"] = round(score_v9_coherence(c, neigh.get(i, []), short), 4)
        kept = select_with_budget(prepared, budget, cell, max_struct=max_struct, second_budget=2)
        return post_thin_overink(kept, w, h, score_floor=0.24), empty_stats

    if ver(variant) == 10:
        provisional = [
            c for c in prepared if c["lengthPref"] >= 0.65 and c["tex"] < 0.5 and c["midPref"] > 0.3
        ]
        provisional = sorted(provisional, key=lambda c: -c["lengthPref"])[:50]
        occ = structure_occupancy(provisional, w, h, thickness=2) if provisional else np.zeros((h, w), np.float32)
        occ_n = occ / (occ.max() + 1e-6) if float(occ.max()) > 0 else occ
        residual = fields["soft_mid_blur"] * (1.0 - 0.9 * occ_n) * (fields["gmag"] * 0.5 + 0.5)
        residual = residual / (float(np.percentile(residual, 95)) + 1e-6)
        for c in prepared:
            c["score"] = round(score_v10_residual(c, residual, w, h), 4)
        kept = select_with_budget(prepared, budget, cell, max_struct=max_struct, second_budget=2)
        return post_thin_overink(kept, w, h, score_floor=0.22), empty_stats

    # ========== v11+: texture-vs-form prioritization ==========
    texture = fields["texture_density"]

    if ver(variant) == 11:
        # Ranking penalty only (same cell budget as v8 dark-budget)
        seed = [c for c in prepared if c["lengthPref"] >= 0.65 and c["texDens"] < 0.5][:35]
        occ = structure_occupancy(seed, w, h, thickness=2) if seed else np.zeros((h, w), np.float32)
        occ_n = occ / (occ.max() + 1e-6) if float(occ.max()) > 0 else occ
        for c in prepared:
            c["score"] = round(score_form_texture(c, occ_n, w, h, tex_power=1.85), 4)
        kept = select_with_budget(prepared, budget, cell, max_struct=max_struct, second_budget=2)
        kept = post_thin_overink(kept, w, h, score_floor=0.22)
        return kept, selection_texture_stats(kept, prepared, fields, cell, budget)

    if ver(variant) == 12:
        # Local quotas by texture class + reserved calm-mid slots
        budget12, cell12 = texture_cell_budget(dark, fields.get("region_tex", texture), fields["soft_mid_blur"], short)
        seed = [c for c in prepared if c["texDens"] < 0.42 and c["midPref"] > 0.35][:40]
        occ = structure_occupancy(seed, w, h, thickness=2) if seed else np.zeros((h, w), np.float32)
        occ_n = occ / (occ.max() + 1e-6) if float(occ.max()) > 0 else occ
        for c in prepared:
            c["score"] = round(score_form_texture(c, occ_n, w, h, tex_power=1.35), 4)
        calm_pool = [c for c in prepared if c["texDens"] < 0.40 and c["midPref"] >= 0.36]
        calm_pool.sort(key=lambda c: -c["score"])
        reserve = min(85, int(max_struct * 0.40), len(calm_pool))
        phase_a = select_with_budget(calm_pool, budget12, cell12, max_struct=reserve, second_budget=0)
        rem = budget12.copy()
        for s in phase_a:
            mx, my = s["mid"]
            cx = min(budget12.shape[1] - 1, max(0, int(mx // cell12)))
            cy = min(budget12.shape[0] - 1, max(0, int(my // cell12)))
            rem[cy, cx] = max(0, rem[cy, cx] - 1)
        a_mids = [s["mid"] for s in phase_a]
        rest = [
            c
            for c in prepared
            if not any(math.hypot(c["mid"][0] - am[0], c["mid"][1] - am[1]) < short * 0.01 for am in a_mids)
        ]
        # high-tex may fill remaining slots but rem already limits them
        phase_b = select_with_budget(rest, rem, cell12, max_struct=max_struct - len(phase_a), second_budget=2)
        kept = post_thin_overink(phase_a + phase_b, w, h, score_floor=0.22)
        return kept, selection_texture_stats(kept, prepared, fields, cell12, budget12)

    # v13: residual midtones only in calm, under-covered soft-mid zones
    # Base: prefer calm form first (like mild v11), then fill residuals
    for c in prepared:
        c["score"] = round(score_form_texture(c, np.zeros((h, w), np.float32), w, h, tex_power=1.7), 4)
    base = select_with_budget(prepared, budget, cell, max_struct=int(max_struct * 0.72), second_budget=0)
    occ = structure_occupancy(base, w, h, thickness=2)
    occ_n = occ / (occ.max() + 1e-6) if float(occ.max()) > 0 else occ
    residuals = []
    base_mids = [s["mid"] for s in base]
    for c in prepared:
        if c["texDens"] >= 0.40:
            continue
        if c["softCov"] < 0.28 or c["midPref"] < 0.38:
            continue
        mx, my = c["mid"]
        xi, yi = int(round(mx)), int(round(my))
        if not (0 <= yi < h and 0 <= xi < w):
            continue
        if float(occ_n[yi, xi]) >= 0.32:
            continue
        if float(texture[yi, xi]) >= 0.42:
            continue
        if any(math.hypot(mx - bm[0], my - bm[1]) < short * 0.014 for bm in base_mids):
            continue
        sc = score_form_texture(c, occ_n, w, h, tex_power=2.0)
        if sc < 0.12:
            continue
        c2 = dict(c)
        c2["score"] = round(sc, 4)
        c2["strength"] = min(float(c2["strength"]), 0.28)
        c2["allowSecondPass"] = False
        c2["residual"] = True
        residuals.append(c2)
    residuals.sort(key=lambda s: -s["score"])
    # fill remaining slots under dark/texture-aware budget
    rem = budget.copy()
    for s in base:
        mx, my = s["mid"]
        cx = min(budget.shape[1] - 1, max(0, int(mx // cell)))
        cy = min(budget.shape[0] - 1, max(0, int(my // cell)))
        rem[cy, cx] = max(0, rem[cy, cx] - 1)
    add = select_with_budget(residuals, rem, cell, max_struct=max_struct - len(base), second_budget=0)
    kept = post_thin_overink(base + add, w, h, score_floor=0.20)
    stats = selection_texture_stats(kept, prepared, fields, cell, budget)
    stats["residualAdded"] = len(add)
    return kept, stats


def form_angle(x: float, y: float, w: int, h: int, gx: float, gy: float, rng: random.Random) -> float:
    base = math.atan2(-gx, gy)
    cy = y / h
    if cy < 0.25:
        prefer = 0.2
    elif cy > 0.75:
        prefer = 0.05
    else:
        prefer = base
    return 0.55 * base + 0.45 * prefer + rng.uniform(-0.28, 0.28)


def midtone_hatch(
    photo_gray: np.ndarray,
    soft: np.ndarray,
    structure: List[dict],
    *,
    variant: str,
    seed: int,
) -> Tuple[List[dict], dict]:
    rng = random.Random(seed)
    h, w = soft.shape
    short = min(h, w)
    if photo_gray.shape != soft.shape:
        photo_gray = cv2.resize(photo_gray, (w, h), interpolation=cv2.INTER_AREA)
    blur = cv2.GaussianBlur(photo_gray, (0, 0), max(1.6, short * 0.002))
    sub = cv2.dilate((soft < 252).astype(np.uint8) * 255, np.ones((9, 9), np.uint8))
    dark = ((255.0 - blur.astype(np.float32)) / 255.0) * (sub > 0)
    occ = structure_occupancy(structure, w, h)
    occ_n = occ / (occ.max() + 1e-6) if float(occ.max()) > 0 else occ
    gx_img = cv2.Sobel(blur, cv2.CV_32F, 1, 0, ksize=3)
    gy_img = cv2.Sobel(blur, cv2.CV_32F, 0, 1, ksize=3)
    gmag = cv2.magnitude(gx_img, gy_img)
    gmag_n = gmag / (float(np.percentile(gmag, 95)) + 1e-6)

    soft_dark = (255.0 - soft.astype(np.float32)) / 255.0
    vn = ver(variant)
    tex_map = compute_texture_density(soft, dark) if vn >= 11 else None
    eligible = (
        (dark > 0.22)
        & (dark < 0.68)
        & (sub > 0)
        & (soft_dark > 0.12)
        & (soft_dark < 0.70)
        & (occ_n < 0.50)
        & (gmag_n > 0.12)
    )
    if tex_map is not None:
        # hatch in calmer midtones; allow mild texture so volume isn't starved
        eligible = eligible & (tex_map < 0.72)
    eligible_n = int(eligible.sum())

    fixed = {
        1: dict(max=55, spacing=0.016, light_mid=0.45, mid=1.0, mid_dark=0.35, very_dark=0.05, strength_scale=0.85),
        2: dict(max=75, spacing=0.013, light_mid=0.70, mid=1.0, mid_dark=0.28, very_dark=0.03, strength_scale=0.80),
        3: dict(max=95, spacing=0.011, light_mid=0.85, mid=1.0, mid_dark=0.22, very_dark=0.02, strength_scale=0.72),
        4: dict(max=110, spacing=0.010, light_mid=0.95, mid=1.0, mid_dark=0.18, very_dark=0.01, strength_scale=0.68),
        5: dict(max=130, spacing=0.0095, light_mid=1.05, mid=0.95, mid_dark=0.12, very_dark=0.0, strength_scale=0.62),
        6: dict(max=155, spacing=0.0082, light_mid=1.20, mid=0.88, mid_dark=0.08, very_dark=0.0, strength_scale=0.55),
        7: dict(max=170, spacing=0.0076, light_mid=1.25, mid=0.92, mid_dark=0.06, very_dark=0.0, strength_scale=0.52),
    }
    if vn >= 8:
        spacing = 0.0080 if vn <= 8 else (0.0078 if vn <= 10 else 0.0080)
        dens = max(1, int((short * spacing) ** 2))
        mult = 0.38 if vn == 8 else (0.42 if vn == 9 else (0.48 if vn == 10 else 0.48))
        adaptive_max = int(round(eligible_n / dens * mult))
        hatch_max = int(np.clip(adaptive_max, 90 if vn >= 11 else 80, 165))
        cfg = dict(
            max=hatch_max,
            spacing=spacing,
            light_mid=1.45 if vn <= 10 else 1.48,
            mid=0.78 if vn <= 10 else 0.75,
            mid_dark=0.06,
            very_dark=0.0,
            strength_scale=0.52 if vn >= 9 else 0.54,
        )
    else:
        cfg = fixed.get(vn, fixed[6])

    def wgt(v: float, occupied: float, grad: float) -> float:
        band = tone_band(v)
        base = {
            "very_light": 0.02,
            "light": 0.45 if vn >= 8 else (0.55 if vn >= 5 else 0.18),
            "light_mid": cfg["light_mid"],
            "mid": cfg["mid"],
            "mid_dark": cfg["mid_dark"],
            "very_dark": cfg["very_dark"],
        }[band]
        occ_k = 0.95 if vn >= 5 else 0.9
        g_k = 0.25 + 0.75 * min(1.0, grad)
        return base * (1.0 - occ_k * occupied) * (0.55 + 0.45 * min(v, 0.65)) * g_k

    if vn >= 5:
        ys, xs = np.where(eligible if vn >= 8 else ((dark > 0.22) & (dark < 0.68) & (sub > 0) & (soft_dark < 0.70) & (occ_n < 0.55)))
    else:
        ys, xs = np.where((dark > 0.18) & (dark < 0.72) & (sub > 0) & (soft_dark < 0.78))
    if len(xs) == 0:
        return [], {"hatchStrokes": 0, "eligiblePx": eligible_n, "hatchMax": cfg["max"]}

    if vn >= 8:
        scores = []
        for i in range(len(xs)):
            x, y = int(xs[i]), int(ys[i])
            v = float(dark[y, x])
            # Prefer light_mid / soft mid modeling over denser mid-dark
            mid_w = 1.35 if 0.30 <= v <= 0.52 else (0.85 if v > 0.58 else 1.0)
            scores.append(float(gmag_n[y, x]) * (1.0 - float(occ_n[y, x])) * mid_w * (0.55 + 0.45 * min(v, 0.6)))
        order = np.argsort(-np.asarray(scores))
        if len(order) > 12000:
            order = order[:: max(1, len(order) // 10000)]
        idx = order.tolist()
        top = idx[: min(800, len(idx))]
        rng.shuffle(top)
        idx = top + idx[min(800, len(idx)) :]
    else:
        idx = list(range(len(xs)))
        rng.shuffle(idx)

    used = np.zeros((h, w), np.uint8)
    r_keep = max(5, int(short * cfg["spacing"]))
    strokes = []
    bands = {k: 0 for k in ("very_light", "light", "light_mid", "mid", "mid_dark", "very_dark")}

    for i in idx:
        if len(strokes) >= cfg["max"]:
            break
        x, y = int(xs[i]), int(ys[i])
        if used[y, x]:
            continue
        v = float(dark[y, x])
        ww = wgt(v, float(occ_n[y, x]), float(gmag_n[y, x]))
        if ww < 0.10 or rng.random() > min(1.0, ww):
            continue
        ang = form_angle(x, y, w, h, float(gx_img[y, x]), float(gy_img[y, x]), rng)
        length = short * (0.009 + 0.020 * ww) * rng.uniform(0.5, 1.4)
        half = length * 0.5
        dx, dy = math.cos(ang), math.sin(ang)
        px, py = -dy, dx
        bow = rng.uniform(-0.25, 0.25) * half
        p0 = (x - dx * half, y - dy * half)
        p1 = (x + px * bow, y + py * bow)
        p2 = (x + dx * half, y + dy * half)
        mx, my = int((p0[0] + p2[0]) / 2), int((p0[1] + p2[1]) / 2)
        if not (0 <= mx < w and 0 <= my < h and sub[my, mx] > 0):
            continue
        if vn >= 8 and float(gmag_n[my, mx]) < 0.08:
            continue
        pts = resample([p0, p1, p2], spacing=max(2.0, short * 0.0026))
        band = tone_band(v)
        bands[band] += 1
        hs = (0.12 + 0.18 * ww) * cfg["strength_scale"] if vn >= 5 else (0.14 + 0.22 * ww) * cfg["strength_scale"]
        strokes.append(
            {
                "kind": "hatch",
                "region": "tone",
                "toneBand": band,
                "strength": round(float(np.clip(hs, 0.07, 0.24 if vn >= 8 else (0.26 if vn >= 5 else 0.32))), 3),
                "length": round(path_length(pts), 2),
                "nPoints": len(pts),
                "allowSecondPass": False,
                "points": pts,
            }
        )
        if vn >= 3 and ww > 0.55 and rng.random() < (0.08 if vn >= 8 else (0.10 if vn >= 5 else 0.15)):
            a2 = ang + rng.uniform(-0.5, 0.5)
            L2 = length * rng.uniform(0.35, 0.65)
            q0 = (x - math.cos(a2) * L2 * 0.5, y - math.sin(a2) * L2 * 0.5)
            q1 = (x + math.cos(a2) * L2 * 0.5, y + math.sin(a2) * L2 * 0.5)
            pts2 = resample([q0, q1], spacing=max(2.0, short * 0.0028))
            if len(strokes) < cfg["max"]:
                strokes.append(
                    {
                        "kind": "hatch",
                        "region": "tone",
                        "toneBand": band,
                        "strength": round(float(np.clip(hs * 0.85, 0.07, 0.22 if vn >= 8 else 0.24)), 3),
                        "length": round(path_length(pts2), 2),
                        "nPoints": len(pts2),
                        "allowSecondPass": False,
                        "points": pts2,
                    }
                )
        cv2.circle(used, (x, y), r_keep + rng.randint(0, 4), 1, -1)

    return strokes, {
        "hatchStrokes": len(strokes),
        "bands": bands,
        "variant": variant,
        "eligiblePx": eligible_n,
        "hatchMax": cfg["max"],
    }



def pack(strokes: List[dict], w: int, h: int) -> List[dict]:
    out = []
    for i, s in enumerate(strokes):
        pts = s["points"]
        if pts and not (pts[0][0] <= 1.5 and pts[0][1] <= 1.5):
            npts = to_norm(pts, w, h)
        else:
            npts = [[round(p[0], 5), round(p[1], 5)] for p in pts]
        out.append(
            {
                "id": i,
                "kind": s["kind"],
                "region": s.get("region", ""),
                "strength": s["strength"],
                "length": s.get("length"),
                "nPoints": len(npts),
                "allowSecondPass": s.get("allowSecondPass", False),
                "toneBand": s.get("toneBand", "structure"),
                "localTone": s.get("localTone"),
                "texDens": s.get("texDens"),
                "texBand": s.get("texBand"),
                "points": npts,
            }
        )
    return out


def extract_midtone_form_strokes(soft: np.ndarray, dark: np.ndarray, max_paths: int = 140) -> List[dict]:
    """
    Extra candidates from softer midtone ridges + soft percentile contours
    in midtone zones (generic — no face rules).
    """
    h, w = soft.shape
    short = min(h, w)
    strength = soft_to_strength(soft)
    strength = cv2.GaussianBlur(strength, (0, 0), max(0.45, short * 0.00055))
    mask = ridge_mask(strength, keep_pct=22.0)
    mid_gate = ((dark > 0.20) & (dark < 0.70)).astype(np.uint8)
    mask = ((mask > 0) & (mid_gate > 0)).astype(np.uint8)
    ridge = nms_ridges(strength, mask)
    ink = (dark > 0.62).astype(np.float32)
    busy = cv2.GaussianBlur(ink, (0, 0), max(2.0, short * 0.012))
    busy = busy / (float(busy.max()) + 1e-6)
    ridge = ridge * (1.0 - 0.55 * busy)
    raw = follow_ridges(ridge, min_pts=max(3, int(short * 0.003)), max_paths=max_paths)
    # also soft percentile contours restricted to midtones
    cont = soft_level_contours(soft, strength)
    cont_mid = []
    for pts, mean_s in cont:
        tone = mean_tone_along(pts, dark, w, h)
        if 0.22 <= tone <= 0.68 and path_length(pts) >= short * 0.015:
            cont_mid.append((pts, mean_s * 0.85))
    cont_mid = sorted(cont_mid, key=lambda t: -path_length(t[0]))[:80]
    raw = list(raw) + cont_mid

    out: List[dict] = []
    min_len = short * 0.008
    for pts, mean_s in raw:
        if mean_s < 0.012:
            continue
        simp = douglas_peucker(pts, max(0.55, short * 0.00055))
        L = path_length(simp)
        if L < min_len:
            continue
        # skip busy short texture
        tex = local_texture_density(simp, busy, w, h)
        if tex > 0.55 and L < short * 0.05:
            continue
        dens = resample(simp, spacing=max(1.7, short * 0.0021))
        kind, region = classify_region(dens, w, h)
        priority = float(np.clip(0.16 + mean_s * 0.60, 0.12, 0.40))
        out.append(
            {
                "kind": kind if kind != "outline" else "other",
                "region": region if region != "silhouette" else "form",
                "strength": round(priority, 3),
                "length": round(path_length(dens), 2),
                "nPoints": len(dens),
                "points": dens,
                "source": "midtone_ridge",
            }
        )
    # dedupe roughly
    kept: List[dict] = []
    for s in sorted(out, key=lambda x: -x["length"]):
        mid = s["points"][len(s["points"]) // 2]
        if any(math.hypot(mid[0] - k["points"][len(k["points"]) // 2][0], mid[1] - k["points"][len(k["points"]) // 2][1]) < short * 0.014 for k in kept):
            continue
        kept.append(s)
        if len(kept) >= 120:
            break
    return kept


def extract_structure_fast(soft: np.ndarray, max_side: int = 720, variant: str = "v5") -> Tuple[List[dict], dict]:
    """
    Same soft ridge + percentile contour idea as soft_paths.extract_structure,
    but capped before light_join (O(n²) blows up on busy photos).
    Does not modify soft_paths / source generation.
    """
    h0, w0 = soft.shape
    short0 = max(h0, w0)
    scale = 1.0
    soft_s = soft
    if short0 > max_side:
        scale = max_side / float(short0)
        sw, sh = max(64, int(round(w0 * scale))), max(64, int(round(h0 * scale)))
        soft_s = cv2.resize(soft, (sw, sh), interpolation=cv2.INTER_AREA)
    h, w = soft_s.shape
    short = min(h, w)
    strength = soft_to_strength(soft_s)
    strength = cv2.GaussianBlur(strength, (0, 0), max(0.45, short * 0.00055))
    mask_strong = ridge_mask(strength, keep_pct=58.0)
    mask_soft = ridge_mask(strength, keep_pct=40.0)
    ridge = np.maximum(nms_ridges(strength, mask_strong), nms_ridges(strength, mask_soft) * 0.9)

    raw = follow_ridges(ridge, min_pts=max(4, int(short * 0.004)), max_paths=420)
    cont = soft_level_contours(soft_s, strength)
    # keep strongest/longest contours only — avoid join explosion
    cont_sorted = sorted(cont, key=lambda t: (-path_length(t[0]), -t[1]))[:280]
    joined = light_join(raw + cont_sorted, max_dist_rel=0.008, short=short)

    strokes: List[dict] = []
    min_len = short * 0.008
    for pts, mean_s in joined:
        if mean_s < 0.02:
            continue
        simp = douglas_peucker(pts, max(0.5, short * 0.0005))
        if path_length(simp) < min_len:
            continue
        dens = resample(simp, spacing=max(1.6, short * 0.002))
        kind, region = classify_region(dens, w, h)
        priority = float(np.clip(mean_s * 1.35, 0.05, 1.0))
        strokes.append(
            {
                "kind": kind,
                "region": region,
                "strength": round(priority, 3),
                "length": round(path_length(dens), 2),
                "nPoints": len(dens),
                "points": dens,
            }
        )

    # mild silhouette hint from soft mass (incomplete outlines handled later)
    # v5+: skip mass silhouette — it creates dark blob contours without midtone info
    if ver(variant) < 5:
        inkish = soft_s < 248
        if np.any(inkish):
            thr = float(np.percentile(soft_s[inkish], 55))
            mass = (soft_s <= thr).astype(np.uint8) * 255
            mass = cv2.morphologyEx(mass, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8), iterations=2)
            mass = cv2.dilate(mass, np.ones((5, 5), np.uint8), iterations=1)
            cnts, _ = cv2.findContours(mass, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
            if cnts:
                cnt = max(cnts, key=cv2.contourArea)
                if cv2.contourArea(cnt) > 0.05 * h * w:
                    pts = [(float(p[0][0]), float(p[0][1])) for p in cnt]
                    n = len(pts)
                    if n > 24:
                        for a, b in [(0, n // 3), (n // 3, 2 * n // 3), (2 * n // 3, n)]:
                            chunk = pts[a:b]
                            if len(chunk) < 12:
                                continue
                            dens = resample(douglas_peucker(chunk, 1.2), spacing=max(2.0, short * 0.0025))
                            strokes.insert(
                                0,
                                {
                                    "kind": "outline",
                                    "region": "silhouette",
                                    "strength": 0.55,
                                    "length": round(path_length(dens), 2),
                                    "nPoints": len(dens),
                                    "points": dens,
                                },
                            )

    kept: List[dict] = []
    for s in sorted(strokes, key=lambda x: -x["length"]):
        mid = s["points"][len(s["points"]) // 2]
        dup = False
        for k in kept:
            km = k["points"][len(k["points"]) // 2]
            if math.hypot(mid[0] - km[0], mid[1] - km[1]) < short * 0.012:
                if abs(k["length"] - s["length"]) < max(10.0, 0.3 * k["length"]):
                    dup = True
                    break
        if not dup:
            kept.append(s)
    strokes = kept
    strokes.sort(key=lambda s: (-s["strength"], -s["length"]))
    if len(strokes) > 450:
        strokes = strokes[:450]

    if scale != 1.0:
        inv = 1.0 / scale
        for s in strokes:
            s["points"] = [(p[0] * inv, p[1] * inv) for p in s["points"]]
            s["length"] = round(float(s["length"]) * inv, 2)

    stats = {
        "rawRidges": len(raw),
        "contoursKept": len(cont_sorted),
        "afterJoin": len(joined),
        "structureStrokes": len(strokes),
        "extractScale": round(scale, 4),
        "extractSize": [w, h],
        "avgPoints": round(float(np.mean([s["nPoints"] for s in strokes])) if strokes else 0, 1),
        "avgStrength": round(float(np.mean([s["strength"] for s in strokes])) if strokes else 0, 3),
    }
    return strokes, stats


def build_for_motif(motif: str, variant: str) -> dict:
    soft = cv2.imread(str(SOFT_DIR / f"{motif}-soft.png"), 0)
    photo = cv2.imread(str(PHOTO_DIR / f"{motif}.png"), 0)
    if soft is None or photo is None:
        raise SystemExit(f"missing soft/photo for {motif}")
    h, w = soft.shape
    photo_r = cv2.resize(photo, (w, h), interpolation=cv2.INTER_AREA)
    blur = cv2.GaussianBlur(photo_r, (0, 0), max(1.5, min(h, w) * 0.002))
    dark = (255.0 - blur.astype(np.float32)) / 255.0

    print(f"… {motif}/{variant}: extract_structure_fast", flush=True)
    raw, raw_stats = extract_structure_fast(soft, max_side=720, variant=variant)
    if ver(variant) >= 8:
        # work-size midtone extras (soft already at work size)
        extras = extract_midtone_form_strokes(soft, dark, max_paths=100)
        raw = list(raw) + extras
        raw_stats = {**raw_stats, "midtoneExtras": len(extras)}
        print(f"… {motif}/{variant}: +{len(extras)} midtone form candidates", flush=True)
    print(f"… {motif}/{variant}: remap+hatch (raw={raw_stats})", flush=True)
    structure, sel_stats = remap_structure(raw, dark, soft, variant)
    hatch, hstats = midtone_hatch(photo_r, soft, structure, variant=variant, seed=hash(motif) % 10000)

    # incomplete outline: randomly drop end chunks for outline strokes
    rng = random.Random(7 + len(variant))
    drop_lo, drop_hi = (0.18, 0.38) if ver(variant) >= 5 else (0.12, 0.28)
    for s in structure:
        if s.get("kind") == "outline" and len(s["points"]) > 16:
            drop = int(len(s["points"]) * rng.uniform(drop_lo, drop_hi))
            if rng.random() < 0.5:
                s["points"] = s["points"][drop:]
            else:
                s["points"] = s["points"][:-drop]
            s["nPoints"] = len(s["points"])
            s["length"] = round(path_length(s["points"]), 2)

    packed_s = pack(structure, w, h)
    packed_h = pack(hatch, w, h)
    plan = {
        "motif": motif,
        "variant": variant,
        "softSource": str(SOFT_DIR / f"{motif}-soft.png"),
        "photo": str(PHOTO_DIR / f"{motif}.png"),
        "width": w,
        "height": h,
        "stats": {
            "rawStructure": raw_stats.get("structureStrokes"),
            "structureStrokes": len(packed_s),
            "hatchStrokes": len(packed_h),
            "secondPassEligible": sum(1 for s in packed_s if s["allowSecondPass"]),
            "avgPointsStructure": round(float(np.mean([s["nPoints"] for s in packed_s])) if packed_s else 0, 1),
            "avgStrengthStructure": round(float(np.mean([s["strength"] for s in packed_s])) if packed_s else 0, 3),
            "avgStrengthHatch": round(float(np.mean([s["strength"] for s in packed_h])) if packed_h else 0, 3),
            "hatchBands": hstats.get("bands", {}),
            "hatchEligiblePx": hstats.get("eligiblePx"),
            "hatchMax": hstats.get("hatchMax"),
            "strategy": {
                8: "info_gain",
                9: "coherence",
                10: "soft_residual",
                11: "texture_penalty",
                12: "texture_quotas",
                13: "calm_residual",
            }.get(ver(variant), "legacy"),
            "selection": sel_stats,
        },
        "structure": packed_s,
        "hatch": packed_h,
        "playback": {
            "outlineSize": 1.1 if ver(variant) >= 5 else (1.2 if ver(variant) >= 2 else 1.35),
            "featureSize": 1.15 if ver(variant) >= 5 else (1.25 if ver(variant) >= 3 else 1.35),
            "hairSize": 1.2 if ver(variant) >= 5 else 1.3,
            "otherSize": 1.12 if ver(variant) >= 5 else 1.2,
            "hatchSize": 0.92 if ver(variant) >= 8 else (0.95 if ver(variant) >= 5 else (1.05 if ver(variant) >= 3 else 1.15)),
            "outlinePressure": 0.18 if ver(variant) >= 5 else (0.22 if ver(variant) >= 2 else 0.28),
            "featurePressure": 0.27 if ver(variant) >= 8 else (0.28 if ver(variant) >= 5 else (0.32 if ver(variant) >= 3 else 0.36)),
            "hatchPressure": 0.14 if ver(variant) >= 8 else (0.15 if ver(variant) >= 5 else (0.18 if ver(variant) >= 3 else 0.20)),
            "densify": 0.0048 if ver(variant) >= 5 else 0.0045,
        },
    }
    out = OUT_DIR / variant
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{motif}-plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    # preview
    vis = cv2.cvtColor(soft, cv2.COLOR_GRAY2BGR)
    vis = (vis.astype(np.float32) * 0.45 + 100).clip(0, 255).astype(np.uint8)
    for s in packed_s:
        arr = np.array([[int(p[0] * w), int(p[1] * h)] for p in s["points"]], np.int32)
        if len(arr) > 1:
            cv2.polylines(vis, [arr], False, (40, 90, 200), 1, cv2.LINE_AA)
    for s in packed_h:
        arr = np.array([[int(p[0] * w), int(p[1] * h)] for p in s["points"]], np.int32)
        if len(arr) > 1:
            cv2.polylines(vis, [arr], False, (40, 160, 70), 1, cv2.LINE_AA)
    cv2.imwrite(str(out / f"{motif}-plan-preview.png"), vis)
    print(motif, variant, plan["stats"])
    return plan


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default="v3")
    ap.add_argument("--motif", default="all")
    args = ap.parse_args()
    motifs = MOTIFS if args.motif == "all" else (args.motif,)
    summary = []
    for m in motifs:
        plan = build_for_motif(m, args.variant)
        summary.append({"motif": m, "stats": plan["stats"]})
    (OUT_DIR / args.variant / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
