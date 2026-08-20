"""
Soft-source → movement paths (no Canny, no Otsu, no hard global BW).

Uses continuous dark-on-light soft map as ridge field + stroke weights.
Frozen sources only — does not regenerate Zeichenkarte.

  PYTHONPATH=admin/preprocess/vendor python3 admin/preprocess/soft_paths.py
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

FROZEN = Path("tmp/soft-sketchy-frozen")
OUT = Path("tmp/soft-sketchy")


def path_length(pts: Sequence[Point]) -> float:
    if len(pts) < 2:
        return 0.0
    return float(
        sum(math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]) for i in range(1, len(pts)))
    )


def soft_to_strength(soft: np.ndarray) -> np.ndarray:
    """Darker soft pixels → stronger lines in [0,1]. Continuous."""
    return np.clip((255.0 - soft.astype(np.float32)) / 255.0, 0, 1)


def ridge_mask(strength: np.ndarray, keep_pct: float = 72.0) -> np.ndarray:
    """
    Soft relevance mask from percentiles of non-zero strength — NOT Otsu.
    Only guides where ridges may be walked; weights stay continuous.
    """
    nz = strength[strength > 1e-4]
    if nz.size < 50:
        return (strength > 0.05).astype(np.uint8)
    floor = float(np.percentile(nz, keep_pct))
    m = (strength >= floor).astype(np.uint8)
    # mild reconnect in mask domain only (relative kernel)
    k = max(3, int(round(min(strength.shape) * 0.003)) | 1)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((k, k), np.uint8), iterations=1)
    return m


def nms_ridges(strength: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Keep local ridge responses (soft NMS), still float."""
    g = cv2.GaussianBlur(strength, (0, 0), max(0.6, min(strength.shape) * 0.0007))
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
    ang = np.arctan2(gy, gx)
    # sample ±1 along gradient normal
    h, w = g.shape
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    dx = np.cos(ang)
    dy = np.sin(ang)
    x1 = np.clip(np.round(xx + dx).astype(np.int32), 0, w - 1)
    y1 = np.clip(np.round(yy + dy).astype(np.int32), 0, h - 1)
    x2 = np.clip(np.round(xx - dx).astype(np.int32), 0, w - 1)
    y2 = np.clip(np.round(yy - dy).astype(np.int32), 0, h - 1)
    keep = (g >= g[y1, x1]) & (g >= g[y2, x2]) & (mask > 0)
    ridge = np.where(keep, g, 0.0).astype(np.float32)
    return ridge


def neighbors8(y, x, h, w):
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dx == 0 and dy == 0:
                continue
            ny, nx = y + dy, x + dx
            if 0 <= ny < h and 0 <= nx < w:
                yield ny, nx


def follow_ridges(ridge: np.ndarray, min_pts: int = 6, max_paths: int = 800) -> List[Tuple[PathPts, float]]:
    """Greedy walk on soft ridge field; returns (points, mean_strength)."""
    h, w = ridge.shape
    # Thin: keep only locally strong pixels to avoid O(n) explosion
    thr = float(np.percentile(ridge[ridge > 0], 55)) if np.any(ridge > 0) else 0.05
    on = ridge >= thr
    visited = np.zeros((h, w), dtype=bool)
    ys, xs = np.where(on)
    if len(xs) == 0:
        return []
    # subsample starts if too many
    strengths = ridge[ys, xs]
    order = np.argsort(-strengths)
    if len(order) > 12000:
        order = order[:: max(1, len(order) // 8000)]
    paths: List[Tuple[PathPts, float]] = []

    def walk(sy, sx):
        chain = [(float(sx), float(sy))]
        vals = [float(ridge[sy, sx])]
        visited[sy, sx] = True
        py, px = -1, -1
        cy, cx = int(sy), int(sx)
        while True:
            best = None
            best_sc = -1.0
            for ny, nx in neighbors8(cy, cx, h, w):
                if not on[ny, nx] or visited[ny, nx]:
                    continue
                if ny == py and nx == px:
                    continue
                sc = float(ridge[ny, nx])
                if py >= 0:
                    vx, vy = cx - px, cy - py
                    wx, wy = nx - cx, ny - cy
                    sc += 0.15 * max(
                        0.0,
                        (vx * wx + vy * wy) / ((math.hypot(vx, vy) * math.hypot(wx, wy)) + 1e-6),
                    )
                if sc > best_sc:
                    best_sc = sc
                    best = (ny, nx)
            if best is None:
                break
            ny, nx = best
            visited[ny, nx] = True
            chain.append((float(nx), float(ny)))
            vals.append(float(ridge[ny, nx]))
            py, px = cy, cx
            cy, cx = ny, nx
            if len(chain) > max(h, w):
                break
        return chain, float(np.mean(vals)) if vals else 0.0

    for i in order:
        if len(paths) >= max_paths:
            break
        y, x = int(ys[i]), int(xs[i])
        if visited[y, x]:
            continue
        deg = sum(1 for ny, nx in neighbors8(y, x, h, w) if on[ny, nx] and not visited[ny, nx])
        if deg > 3:
            continue
        chain, mean_s = walk(y, x)
        if len(chain) >= min_pts:
            paths.append((chain, mean_s))
    return paths


def light_join(paths: List[Tuple[PathPts, float]], max_dist_rel: float, short: int) -> List[Tuple[PathPts, float]]:
    """Mild endpoint joins — relative gap, limited rounds (no aggressive path reduction)."""
    max_dist = short * max_dist_rel
    items = [(list(p), s) for p, s in paths if len(p) >= 2]
    joins = 0
    for _ in range(min(120, len(items))):
        best = None
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                for a_s in (True, False):
                    for b_s in (True, False):
                        pa = items[i][0][0] if a_s else items[i][0][-1]
                        pb = items[j][0][0] if b_s else items[j][0][-1]
                        d = math.hypot(pa[0] - pb[0], pa[1] - pb[1])
                        if d > max_dist:
                            continue
                        sc = d
                        if best is None or sc < best[0]:
                            best = (sc, i, a_s, j, b_s)
        if not best:
            break
        _, i, a_s, j, b_s = best
        a = list(reversed(items[i][0])) if a_s else list(items[i][0])
        b = list(items[j][0]) if b_s else list(reversed(items[j][0]))
        merged = a + b[1:]
        mean_s = 0.5 * (items[i][1] + items[j][1])
        items = [(merged, mean_s) if k == i else it for k, it in enumerate(items) if k != j]
        joins += 1
    return items


def douglas_peucker(pts: PathPts, eps: float) -> PathPts:
    if len(pts) < 3:
        return list(pts)

    def d(p, a, b):
        x, y = a
        dx, dy = b[0] - x, b[1] - y
        if dx == 0 and dy == 0:
            return math.hypot(p[0] - x, p[1] - y)
        t = max(0.0, min(1.0, ((p[0] - x) * dx + (p[1] - y) * dy) / (dx * dx + dy * dy)))
        return math.hypot(p[0] - (x + t * dx), p[1] - (y + t * dy))

    def rec(chunk):
        if len(chunk) < 3:
            return chunk
        a, b = chunk[0], chunk[-1]
        idx, md = 0, -1.0
        for i in range(1, len(chunk) - 1):
            di = d(chunk[i], a, b)
            if di > md:
                md, idx = di, i
        if md > eps:
            return rec(chunk[: idx + 1])[:-1] + rec(chunk[idx:])
        return [a, b]

    return rec(list(pts))


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


def classify_region(pts: PathPts, w: int, h: int) -> Tuple[str, str]:
    """Soft heuristic labels for drawing priority — not face geometry invention."""
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    cy = (min(ys) + max(ys)) / 2 / h
    cx = (min(xs) + max(xs)) / 2 / w
    bw = (max(xs) - min(xs)) / w
    bh = (max(ys) - min(ys)) / h
    L = path_length(pts) / max(w, h)
    if L > 0.35 and bw > 0.2 and bh > 0.25:
        return "outline", "silhouette"
    if cy < 0.32:
        return "hair", "hair"
    if 0.22 <= cy <= 0.48 and abs(cx - 0.5) < 0.4:
        return "feature", "eyes"
    if 0.40 <= cy <= 0.60 and abs(cx - 0.5) < 0.22:
        return "feature", "nose"
    if 0.54 <= cy <= 0.74 and abs(cx - 0.5) < 0.3:
        return "feature", "mouth"
    if cy > 0.72:
        return "feature", "jaw"
    return "other", "form"


def soft_level_contours(soft: np.ndarray, strength: np.ndarray) -> List[Tuple[PathPts, float]]:
    """Multi-level soft contours via percentiles — not Otsu, not Canny."""
    h, w = soft.shape
    short = min(h, w)
    inkish = soft[soft < 248]
    if inkish.size < 80:
        return []
    out: List[Tuple[PathPts, float]] = []
    for pct in (8, 16, 28, 40):
        thr = float(np.clip(np.percentile(inkish, pct), 40, 245))
        mask = (soft <= thr).astype(np.uint8) * 255
        k = max(3, int(round(short * 0.0025)) | 1)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((k, k), np.uint8), iterations=1)
        cnts, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
        for c in cnts:
            if len(c) < 10:
                continue
            pts = [(float(p[0][0]), float(p[0][1])) for p in c]
            if math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < 3:
                pts = pts[:-1]
            n = len(pts)
            if n < 10:
                continue
            chunks = [pts]
            if n > 100:
                chunks = [pts[: n // 2 + 1], pts[n // 2 :]]
            if n > 220:
                step = n // 3
                chunks = [pts[i * step : (i + 1) * step + 1] for i in range(3)]
            for chunk in chunks:
                if len(chunk) < 8 or path_length(chunk) < short * 0.01:
                    continue
                vals = []
                for x, y in chunk[:: max(1, len(chunk) // 40)]:
                    xi, yi = int(round(x)), int(round(y))
                    if 0 <= yi < h and 0 <= xi < w:
                        vals.append(float(strength[yi, xi]))
                mean_s = float(np.mean(vals)) if vals else 0.05
                out.append((chunk, max(0.05, mean_s)))
    return out


def extract_structure(soft: np.ndarray) -> Tuple[List[dict], dict]:
    h, w = soft.shape
    short = min(h, w)
    strength = soft_to_strength(soft)
    strength = cv2.GaussianBlur(strength, (0, 0), max(0.45, short * 0.00055))

    mask_strong = ridge_mask(strength, keep_pct=58.0)
    mask_soft = ridge_mask(strength, keep_pct=40.0)
    ridge = np.maximum(nms_ridges(strength, mask_strong), nms_ridges(strength, mask_soft) * 0.9)

    raw = follow_ridges(ridge, min_pts=max(4, int(short * 0.004)), max_paths=700)
    raw.extend(soft_level_contours(soft, strength))
    joined = light_join(raw, max_dist_rel=0.008, short=short)

    strokes = []
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

    inkish = soft < 248
    if np.any(inkish):
        thr = float(np.percentile(soft[inkish], 55))
        mass = (soft <= thr).astype(np.uint8) * 255
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

    kept = []
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

    stats = {
        "rawRidges": len(raw),
        "afterJoin": len(joined),
        "structureStrokes": len(strokes),
        "avgPoints": round(float(np.mean([s["nPoints"] for s in strokes])) if strokes else 0, 1),
        "avgStrength": round(float(np.mean([s["strength"] for s in strokes])) if strokes else 0, 3),
        "byRegion": {},
    }
    for s in strokes:
        stats["byRegion"][s["region"]] = stats["byRegion"].get(s["region"], 0) + 1
    return strokes, stats


def tone_hatch(photo_gray: np.ndarray, soft: np.ndarray, max_strokes: int = 90, seed: int = 11) -> List[dict]:
    """Free hatch from photo tone — not from edges. Soft map only as subject prior."""
    rng = random.Random(seed)
    # align sizes
    if photo_gray.shape != soft.shape:
        photo_gray = cv2.resize(photo_gray, (soft.shape[1], soft.shape[0]), interpolation=cv2.INTER_AREA)
    h, w = soft.shape
    short = min(h, w)
    blur = cv2.GaussianBlur(photo_gray, (0, 0), max(1.5, short * 0.002))
    # subject ≈ where soft has any structure nearby
    sub = cv2.dilate((soft < 250).astype(np.uint8) * 255, np.ones((9, 9), np.uint8))
    dark = ((255 - blur).astype(np.float32) / 255.0) * (sub > 0)
    dark[dark < 0.30] = 0
    dark[int(h * 0.9) :, :] = 0
    ys, xs = np.where(dark > 0.38)
    if len(xs) == 0:
        return []
    idx = list(range(len(xs)))
    rng.shuffle(idx)
    used = np.zeros((h, w), np.uint8)
    strokes = []
    for i in idx:
        if len(strokes) >= max_strokes:
            break
        x, y = int(xs[i]), int(ys[i])
        if used[y, x]:
            continue
        strength = float(dark[y, x])
        gy = float(blur[min(h - 1, y + 1), x]) - float(blur[max(0, y - 1), x])
        gx = float(blur[y, min(w - 1, x + 1)]) - float(blur[y, max(0, x - 1)])
        ang = math.atan2(-gx, gy) + rng.uniform(-0.4, 0.4)
        length = short * (0.012 + strength * 0.028) + rng.uniform(0, short * 0.006)
        n_layers = 1 + int(strength > 0.5) + int(strength > 0.65)
        for layer in range(n_layers):
            a = ang + layer * 0.45 + rng.uniform(-0.2, 0.2)
            half = length * (0.5 + 0.15 * layer)
            dx, dy = math.cos(a), math.sin(a)
            p0 = (x - dx * half, y - dy * half)
            p1 = (x + dx * half, y + dy * half)
            pts = resample([p0, p1], spacing=max(2.0, short * 0.0025))
            strokes.append(
                {
                    "kind": "hatch",
                    "region": "tone",
                    "strength": round(strength * 0.7, 3),
                    "length": round(path_length(pts), 2),
                    "nPoints": len(pts),
                    "points": pts,
                }
            )
        r = max(4, int(short * 0.008))
        cv2.circle(used, (x, y), r, 1, -1)
    return strokes


def to_norm(pts: PathPts, w: int, h: int) -> List[List[float]]:
    return [[round(x / w, 5), round(y / h, 5)] for x, y in pts]


def pack(strokes: List[dict], w: int, h: int) -> List[dict]:
    out = []
    for i, s in enumerate(strokes):
        out.append(
            {
                "id": i,
                "kind": s["kind"],
                "region": s["region"],
                "strength": s["strength"],
                "length": s["length"],
                "nPoints": s["nPoints"],
                "points": to_norm(s["points"], w, h),
            }
        )
    return out


def preview(soft: np.ndarray, strokes: List[dict], path: Path) -> None:
    img = cv2.cvtColor(soft, cv2.COLOR_GRAY2BGR)
    img = (img.astype(np.float32) * 0.55 + 80).clip(0, 255).astype(np.uint8)
    colors = {
        "outline": (40, 40, 200),
        "feature": (180, 90, 30),
        "hair": (160, 50, 160),
        "hatch": (40, 140, 60),
        "other": (90, 90, 90),
    }
    for s in strokes:
        pts = np.array([[int(round(x)), int(round(y))] for x, y in s["points"]], np.int32)
        if len(pts) < 2:
            continue
        cv2.polylines(img, [pts], False, colors.get(s["kind"], (80, 80, 80)), 1, cv2.LINE_AA)
    cv2.imwrite(str(path), img)


def build_plan(soft_path: Path, photo_path: Optional[Path], out_dir: Path, tag: str) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    soft = cv2.imread(str(soft_path), cv2.IMREAD_GRAYSCALE)
    if soft is None:
        raise SystemExit(f"missing soft source {soft_path}")
    # NEVER rewrite frozen soft
    h, w = soft.shape
    structure, stats = extract_structure(soft)
    hatch: List[dict] = []
    if photo_path and photo_path.exists():
        photo = cv2.imread(str(photo_path), cv2.IMREAD_GRAYSCALE)
        hatch = tone_hatch(photo, soft, max_strokes=85)

    preview(soft, structure, out_dir / f"{tag}-paths-structure.png")
    preview(soft, structure + hatch, out_dir / f"{tag}-paths-all.png")

    plan = {
        "tag": tag,
        "softSource": str(soft_path),
        "photo": str(photo_path) if photo_path else None,
        "width": w,
        "height": h,
        "frozen": True,
        "method": "soft-ridge-follow + strength weights; no Canny/Otsu",
        "stats": {
            **stats,
            "hatchStrokes": len(hatch),
            "hairStrokes": sum(1 for s in structure if s["kind"] == "hair"),
            "featureStrokes": sum(1 for s in structure if s["kind"] == "feature"),
            "outlineStrokes": sum(1 for s in structure if s["kind"] == "outline"),
        },
        "structure": pack(structure, w, h),
        "hatch": pack(hatch, w, h),
    }
    (out_dir / f"{tag}-strokeplan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    return plan


def main() -> None:
    jobs = [
        (
            "ref",
            FROZEN / "zeichnkarte-source-soft.png",
            Path("tmp/photo-sketch-testset/ref-small-portrait.png"),
        ),
        (
            "dark-portrait",
            FROZEN / "dark-portrait-soft.png",
            Path("tmp/photo-sketch-testset/dark-portrait.jpg"),
        ),
        (
            "scene-tango",
            FROZEN / "scene-tango-soft.png",
            Path("tmp/photo-sketch-testset/scene-tango.jpg"),
        ),
    ]
    OUT.mkdir(parents=True, exist_ok=True)
    summary = []
    for tag, soft, photo in jobs:
        print(f"paths ← {soft}")
        plan = build_plan(soft, photo, OUT, tag)
        summary.append({"tag": tag, "stats": plan["stats"], "soft": str(soft)})
        print(json.dumps(plan["stats"], indent=2))
    (OUT / "soft-paths-summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
