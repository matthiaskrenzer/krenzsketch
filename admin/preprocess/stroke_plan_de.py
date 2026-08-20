"""
Stroke-plan D/E from frozen B structure — no soft-source changes.

Reuses existing structure strokes from ref-strokeplan.json.
Rebuilds only hatch + playback weights for balanced mid-tone modeling.

  PYTHONPATH=admin/preprocess/vendor python3 admin/preprocess/stroke_plan_de.py
"""

from __future__ import annotations

import json
import math
import random
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

Point = Tuple[float, float]
PathPts = List[Point]

ROOT = Path(".")
PLAN_IN = ROOT / "tmp/soft-sketchy/ref-strokeplan.json"
SOFT = ROOT / "tmp/soft-sketchy-frozen/zeichnkarte-source-soft.png"
PHOTO = ROOT / "tmp/photo-sketch-testset/ref-small-portrait.png"
OUT = ROOT / "tmp/soft-sketchy"


def path_length(pts: Sequence[Point]) -> float:
    if len(pts) < 2:
        return 0.0
    return float(
        sum(math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]) for i in range(1, len(pts)))
    )


def resample(pts: PathPts, spacing: float) -> PathPts:
    if len(pts) < 2:
        return list(pts)
    out = [pts[0]]
    acc = 0.0
    for i in range(1, len(pts)):
        ax, ay = pts[i - 1]
        bx, by = pts[i]
        seg = math.hypot(bx - ax, by - ay)
        if seg < 1e-6:
            continue
        while acc + seg >= spacing:
            t = (spacing - acc) / seg
            out.append((ax + t * (bx - ax), ay + t * (by - ay)))
            ax, ay = out[-1]
            seg = math.hypot(bx - ax, by - ay)
            acc = 0.0
        acc += seg
    if out[-1] != pts[-1]:
        out.append(pts[-1])
    return out


def to_norm(pts: PathPts, w: int, h: int) -> List[List[float]]:
    return [[round(x / w, 5), round(y / h, 5)] for x, y in pts]


def remap_structure(structure: List[dict], mode: str) -> List[dict]:
    """
    Soften dark overdraw while keeping B's stroke set as base.
    Thin overlapping eye/hair strokes (planning only — not source).
    mode: 'D' | 'E'
    """
    # Cap strengths first
    capped = []
    for s in structure:
        kind = s.get("kind", "other")
        region = s.get("region", "")
        st = float(s.get("strength") or 0.4)
        if region == "eyes":
            st = min(st, 0.40 if mode == "D" else 0.32)
        elif kind == "hair":
            st = min(st, 0.36 if mode == "D" else 0.28)
        elif kind == "outline" or region == "silhouette":
            st = min(st, 0.30 if mode == "D" else 0.22)
        elif region == "mouth":
            st = min(st, 0.46 if mode == "D" else 0.38)
        elif region == "nose":
            st = min(st, 0.26 if mode == "D" else 0.20)
        elif region == "jaw":
            st = min(st, 0.38 if mode == "D" else 0.30)
        else:
            st = min(st, 0.42 if mode == "D" else 0.34)
        ns = dict(s)
        ns["strength"] = round(st, 3)
        ns["allowSecondPass"] = False
        ns["toneBand"] = "structure"
        capped.append(ns)

    # Thin dense regions: keep strongest strokes only (prevent black fill)
    def thin(pred, keep: int):
        group = [s for s in capped if pred(s)]
        rest = [s for s in capped if not pred(s)]
        group.sort(key=lambda s: (-s["strength"], -(s.get("length") or 0)))
        return rest + group[:keep]

    if mode == "D":
        capped = thin(lambda s: s.get("region") == "eyes", 14)
        capped = thin(lambda s: s.get("kind") == "hair", 48)
        capped = thin(lambda s: s.get("kind") == "outline" or s.get("region") == "silhouette", 5)
    else:
        capped = thin(lambda s: s.get("region") == "eyes", 10)
        capped = thin(lambda s: s.get("kind") == "hair", 36)
        capped = thin(lambda s: s.get("kind") == "outline" or s.get("region") == "silhouette", 4)

    out = capped

    def mark_top(region_name: str, n: int, min_st: float):
        cand = [s for s in out if s.get("region") == region_name and s["strength"] >= min_st]
        cand.sort(key=lambda s: -s["strength"])
        for s in cand[:n]:
            s["allowSecondPass"] = True

    if mode == "D":
        mark_top("eyes", 6, 0.28)
        mark_top("mouth", 3, 0.28)
        mark_top("jaw", 2, 0.30)
        hair = [s for s in out if s.get("kind") == "hair" and s["strength"] >= 0.32]
        hair.sort(key=lambda s: -s["strength"])
        for s in hair[:3]:
            s["allowSecondPass"] = True
    else:
        mark_top("eyes", 3, 0.28)
        mark_top("mouth", 2, 0.30)

    return out


def form_angle(x: float, y: float, w: int, h: int, gx: float, gy: float, rng: random.Random) -> float:
    """Form-following hatch angle from region + local gradient."""
    cy, cx = y / h, x / w
    # iso-brightness direction (perp to gradient) as base
    base = math.atan2(-gx, gy)
    # region biases
    if cy < 0.28:
        # forehead: gently lateral / slightly curved
        prefer = 0.15 + (cx - 0.5) * 0.35
    elif 0.28 <= cy < 0.48 and abs(cx - 0.5) < 0.38:
        # eye sockets: slightly diagonal under form
        prefer = 0.55 if cx < 0.5 else -0.55
    elif 0.38 <= cy < 0.58 and abs(cx - 0.5) < 0.18:
        # nose: along / slightly across
        prefer = 1.35 + rng.uniform(-0.2, 0.2)
    elif 0.48 <= cy < 0.72 and abs(cx - 0.5) > 0.12:
        # cheeks follow oval
        prefer = math.atan2(cy - 0.45, cx - 0.5) + math.pi / 2
    elif 0.58 <= cy < 0.72 and abs(cx - 0.5) < 0.22:
        # under nose / mouth
        prefer = 0.2 + rng.uniform(-0.25, 0.25)
    elif cy >= 0.72:
        # under chin / jaw / neck: more diagonal-horizontal
        prefer = 0.05 + rng.uniform(-0.35, 0.35)
    elif cy < 0.34:
        # hair mass: flow downward-ish
        prefer = 1.2 + (cx - 0.5) * 0.5
    else:
        prefer = base

    # blend gradient direction with region prefer
    # convert prefer to angle blend
    ang = 0.45 * base + 0.55 * prefer
    ang += rng.uniform(-0.22, 0.22)
    return ang


def tone_band(v: float) -> str:
    # v = darkness 0..1
    if v < 0.22:
        return "very_light"
    if v < 0.38:
        return "light_mid"
    if v < 0.55:
        return "mid"
    if v < 0.72:
        return "mid_dark"
    return "very_dark"


def midtone_hatch(
    photo_gray: np.ndarray,
    soft: np.ndarray,
    structure_ink: np.ndarray,
    *,
    mode: str,
    seed: int = 21,
) -> Tuple[List[dict], dict]:
    """
    Organic mid-tone hatch. Fewer strokes; avoid stacking on already-dark structure.
    """
    rng = random.Random(seed + (0 if mode == "D" else 7))
    if photo_gray.shape != soft.shape:
        photo_gray = cv2.resize(photo_gray, (soft.shape[1], soft.shape[0]), interpolation=cv2.INTER_AREA)
    h, w = soft.shape
    short = min(h, w)
    blur = cv2.GaussianBlur(photo_gray, (0, 0), max(1.8, short * 0.0022))
    # subject from soft
    sub = cv2.dilate((soft < 252).astype(np.uint8) * 255, np.ones((11, 11), np.uint8))
    dark = ((255.0 - blur.astype(np.float32)) / 255.0) * (sub > 0)
    # structure occupancy: suppress hatch where structure already dense
    struct = structure_ink.astype(np.float32)
    if struct.shape != dark.shape:
        struct = cv2.resize(struct, (w, h), interpolation=cv2.INTER_AREA)
    struct = cv2.GaussianBlur(struct, (0, 0), max(2.0, short * 0.004))
    struct_n = struct / (struct.max() + 1e-6)

    # Mid-tone preference weights (not more hatch in very dark)
    # very_light:0, light_mid:low, mid:high, mid_dark:med, very_dark:low
    def hatch_weight(v: float, occupied: float) -> float:
        band = tone_band(v)
        base = {
            "very_light": 0.02,
            "light_mid": 0.85 if mode == "E" else 0.55,
            "mid": 1.0,
            "mid_dark": 0.40 if mode == "D" else 0.30,
            "very_dark": 0.05,
        }[band]
        # avoid black holes: heavily occupied structure → almost no hatch
        return base * (1.0 - 0.85 * occupied) * v

    max_strokes = 70 if mode == "D" else 85
    spacing_rel = 0.014 if mode == "D" else 0.011  # denser mid-tone seeds
    r_keep = max(6, int(short * spacing_rel))

    # candidate seeds in mid bands
    ys, xs = np.where((dark > 0.24) & (dark < 0.78) & (sub > 0))
    if len(xs) == 0:
        return [], {"hatchStrokes": 0}
    idx = list(range(len(xs)))
    rng.shuffle(idx)
    used = np.zeros((h, w), np.uint8)
    strokes: List[dict] = []
    band_counts = {k: 0 for k in ("very_light", "light_mid", "mid", "mid_dark", "very_dark")}

    # Sobel for form
    gx_img = cv2.Sobel(blur, cv2.CV_32F, 1, 0, ksize=3)
    gy_img = cv2.Sobel(blur, cv2.CV_32F, 0, 1, ksize=3)

    for i in idx:
        if len(strokes) >= max_strokes:
            break
        x, y = int(xs[i]), int(ys[i])
        if used[y, x]:
            continue
        v = float(dark[y, x])
        occ = float(struct_n[y, x])
        wgt = hatch_weight(v, occ)
        if wgt < 0.12:
            continue
        # probabilistic accept by weight
        if rng.random() > min(1.0, wgt):
            continue

        ang = form_angle(x, y, w, h, float(gx_img[y, x]), float(gy_img[y, x]), rng)
        # organic length variation
        length = short * (0.010 + 0.018 * wgt) * rng.uniform(0.55, 1.35)
        if mode == "E":
            length *= rng.uniform(0.7, 1.15)
        # single stroke mostly — Sketchy adds unrest (avoid multi-layer crosshatch)
        n_layers = 1
        if mode == "D" and wgt > 0.7 and rng.random() < 0.18:
            n_layers = 2
        if mode == "E" and wgt > 0.65 and rng.random() < 0.12:
            n_layers = 2

        for layer in range(n_layers):
            a = ang + layer * rng.uniform(0.25, 0.55) * rng.choice([-1, 1])
            # slight curve via 3-point polyline
            half = length * 0.5
            dx, dy = math.cos(a), math.sin(a)
            px, py = -dy, dx  # perpendicular for bow
            bow = rng.uniform(-0.22, 0.22) * half
            p0 = (x - dx * half, y - dy * half)
            p1 = (x + px * bow, y + py * bow)
            p2 = (x + dx * half, y + dy * half)
            pts = resample([p0, p1, p2], spacing=max(2.2, short * 0.0028))
            # clip if midpoint outside subject
            mx, my = int((p0[0] + p2[0]) / 2), int((p0[1] + p2[1]) / 2)
            if not (0 <= mx < w and 0 <= my < h and sub[my, mx] > 0):
                continue
            band = tone_band(v)
            band_counts[band] += 1
            # hatch strength: mid preferred, keep light for pencil
            hs = 0.22 + 0.28 * wgt
            if mode == "E":
                hs *= 0.85
            strokes.append(
                {
                    "kind": "hatch",
                    "region": "tone",
                    "toneBand": band,
                    "strength": round(float(np.clip(hs, 0.12, 0.42)), 3),
                    "length": round(path_length(pts), 2),
                    "nPoints": len(pts),
                    "allowSecondPass": False,
                    "points": to_norm(pts, w, h),
                }
            )
        cv2.circle(used, (x, y), r_keep + rng.randint(0, 3), 1, -1)

    stats = {
        "hatchStrokes": len(strokes),
        "bandCounts": band_counts,
        "maxStrokes": max_strokes,
        "mode": mode,
    }
    return strokes, stats


def structure_occupancy(structure: List[dict], w: int, h: int) -> np.ndarray:
    ink = np.zeros((h, w), np.float32)
    for s in structure:
        pts = s["points"]
        # points may already be normalized
        if pts and pts[0][0] <= 1.5 and pts[0][1] <= 1.5:
            pix = [(p[0] * w, p[1] * h) for p in pts]
        else:
            pix = pts
        arr = np.array([[int(round(x)), int(round(y))] for x, y in pix], np.int32)
        if len(arr) >= 2:
            cv2.polylines(ink, [arr], False, 1.0, 2, cv2.LINE_AA)
    return ink


def pack_keep_norm(structure: List[dict]) -> List[dict]:
    """Structure already normalized in plan."""
    out = []
    for i, s in enumerate(structure):
        out.append(
            {
                "id": i,
                "kind": s["kind"],
                "region": s.get("region", ""),
                "strength": s["strength"],
                "length": s.get("length"),
                "nPoints": s.get("nPoints") or len(s["points"]),
                "allowSecondPass": s.get("allowSecondPass", False),
                "toneBand": s.get("toneBand", "structure"),
                "points": s["points"],
            }
        )
    return out


def preview_plan(soft: np.ndarray, structure: List[dict], hatch: List[dict], path: Path) -> None:
    img = cv2.cvtColor(soft, cv2.COLOR_GRAY2BGR)
    img = (img.astype(np.float32) * 0.5 + 90).clip(0, 255).astype(np.uint8)
    h, w = soft.shape

    def draw(strokes, color):
        for s in strokes:
            pts = s["points"]
            pix = np.array([[int(p[0] * w), int(p[1] * h)] for p in pts], np.int32)
            if len(pix) >= 2:
                cv2.polylines(img, [pix], False, color, 1, cv2.LINE_AA)

    draw(structure, (40, 90, 200))
    draw(hatch, (40, 150, 70))
    cv2.imwrite(str(path), img)


def band_share(strokes: List[dict]) -> Dict[str, float]:
    if not strokes:
        return {}
    n = len(strokes)
    counts: Dict[str, int] = {}
    for s in strokes:
        b = s.get("toneBand", "structure")
        counts[b] = counts.get(b, 0) + 1
    return {k: round(100.0 * v / n, 1) for k, v in counts.items()}


def main() -> None:
    plan = json.loads(PLAN_IN.read_text())
    soft = cv2.imread(str(SOFT), 0)
    photo = cv2.imread(str(PHOTO), 0)
    if soft is None or photo is None:
        raise SystemExit("missing frozen soft or photo")
    w, h = plan["width"], plan["height"]
    raw_struct = plan["structure"]

    reports = {}
    for mode in ("D", "E"):
        structure = remap_structure(raw_struct, mode)
        occ = structure_occupancy(structure, w, h)
        hatch, hstats = midtone_hatch(photo, soft, occ, mode=mode)
        packed_s = pack_keep_norm(structure)
        all_strokes = packed_s + hatch
        avg_pts = float(np.mean([s["nPoints"] for s in packed_s])) if packed_s else 0
        avg_st = float(np.mean([s["strength"] for s in packed_s])) if packed_s else 0
        second = sum(1 for s in packed_s if s.get("allowSecondPass"))
        out = {
            "tag": f"ref-{mode}",
            "basedOn": "B structure (ref-strokeplan) + remapped strengths + midtone hatch",
            "softSource": str(SOFT),
            "frozenB": "tmp/soft-sketchy-frozen/portrait-soft-sketchy-b-frozen.png",
            "width": w,
            "height": h,
            "mode": mode,
            "stats": {
                "structureStrokes": len(packed_s),
                "hatchStrokes": len(hatch),
                "allowSecondPassCount": second,
                "avgPointsStructure": round(avg_pts, 1),
                "avgStrengthStructure": round(avg_st, 3),
                "avgStrengthHatch": round(float(np.mean([s["strength"] for s in hatch])) if hatch else 0, 3),
                "structureBandSharePct": band_share(packed_s),
                "hatchBandSharePct": band_share(hatch),
                "hatchDetail": hstats,
            },
            "structure": packed_s,
            "hatch": hatch,
            "playback": {
                "D": {
                    "outlineSize": 1.35,
                    "featureSize": 1.35,
                    "hairSize": 1.45,
                    "hatchSize": 1.15,
                    "outlinePressure": 0.28,
                    "densify": 0.0042,
                },
                "E": {
                    "outlineSize": 1.15,
                    "featureSize": 1.2,
                    "hairSize": 1.25,
                    "hatchSize": 1.05,
                    "outlinePressure": 0.22,
                    "densify": 0.0048,
                },
            }[mode],
        }
        name = f"ref-strokeplan-{mode.lower()}.json"
        (OUT / name).write_text(json.dumps(out, indent=2), encoding="utf-8")
        preview_plan(soft, packed_s, hatch, OUT / f"ref-paths-{mode.lower()}.png")
        reports[mode] = out["stats"]
        print(mode, json.dumps(out["stats"], indent=2))

    # B/C stats snapshot from existing plan for report
    b_struct = len(raw_struct)
    c_hatch = len(plan.get("hatch") or [])
    summary = {
        "B_structure": b_struct,
        "C_hatch_legacy": c_hatch,
        "D": reports["D"],
        "E": reports["E"],
        "notes": {
            "C_problem": "Hatch concentrated in dark zones + multi-layer → black holes / technical look",
            "DE_fix": "Mid-tone preference, suppress hatch on structure, form angles, fewer organic strokes, capped feature strength",
        },
    }
    (OUT / "stroke-plan-de-summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
