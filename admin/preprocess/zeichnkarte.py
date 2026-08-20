"""
Zeichenkarte / Stroke-Plan for KrenzSketch portrait automation.

Philosophy:
  Preprocessing must NOT produce a finished drawing.
  It only supplies movement paths (structure) + optional hatch zones (tone).
  Sketchy creates the drawing.

Usage:
  PYTHONPATH=admin/preprocess/vendor python3 admin/preprocess/zeichnkarte.py \\
    --photo /path/foto.png --outdir tmp/portrait-preprocess
"""

from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

Point = Tuple[float, float]
PathPts = List[Point]

PHOTO_DEFAULT = (
    "/Users/matthiaskrenzer/.cursor/projects/Volumes-Extreme-SSD-Projekte-krenztek/assets/"
    "Bildschirmfoto_2026-08-20_um_09.43.02-25c065d2-f660-4f1e-b81e-20067d0898e2.png"
)


def path_length(pts: Sequence[Point]) -> float:
    if len(pts) < 2:
        return 0.0
    return float(
        sum(math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]) for i in range(1, len(pts)))
    )


def load_portrait(path: Path, max_side: int = 900) -> Tuple[np.ndarray, np.ndarray]:
    bgr = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if bgr is None:
        raise SystemExit(f"Cannot read {path}")
    h, w = bgr.shape[:2]
    if max(h, w) > max_side:
        s = max_side / float(max(h, w))
        bgr = cv2.resize(bgr, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    gray = clahe.apply(gray)
    gray = cv2.GaussianBlur(gray, (0, 0), 0.6)
    return bgr, gray


def xdog(gray: np.ndarray) -> np.ndarray:
    g = gray.astype(np.float32) / 255.0
    g1 = cv2.GaussianBlur(g, (0, 0), 0.9)
    g2 = cv2.GaussianBlur(g, (0, 0), 0.9 * 1.6)
    dog = g1 - 0.97 * g2
    dog_n = dog / (np.abs(dog).max() + 1e-8)
    e = 1.0 + np.tanh(18.0 * (dog_n + 0.02))
    out = ((1.0 - np.clip(e, 0, 1)) * 255.0).astype(np.uint8)
    _, bw = cv2.threshold(out, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if np.mean(bw) < 127:
        bw = 255 - bw
    return bw


def canny_lineart(gray: np.ndarray) -> np.ndarray:
    return 255 - cv2.Canny(gray, 40, 120, L2gradient=True)


def hybrid_karte(gray: np.ndarray) -> np.ndarray:
    """Legacy early-binary hybrid (kept for comparison only). Prefer adaptive_source."""
    x = xdog(gray)
    c = canny_lineart(gray)
    combo = cv2.max(255 - x, 255 - c)
    combo = cv2.morphologyEx(combo, cv2.MORPH_CLOSE, np.ones((2, 2), np.uint8), iterations=1)
    combo = cv2.morphologyEx(combo, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8), iterations=1)
    return 255 - combo


def subject_mask(bgr: np.ndarray, gray: np.ndarray) -> np.ndarray:
    h, w = gray.shape
    mask = np.zeros(gray.shape, np.uint8)
    rect = (int(w * 0.08), int(h * 0.01), int(w * 0.84), int(h * 0.97))
    bgd = np.zeros((1, 65), np.float64)
    fgd = np.zeros((1, 65), np.float64)
    try:
        cv2.grabCut(bgr, mask, rect, bgd, fgd, 2, cv2.GC_INIT_WITH_RECT)
        subject = np.where((mask == 2) | (mask == 0), 0, 255).astype(np.uint8)
        subject = cv2.morphologyEx(subject, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
        if cv2.countNonZero(subject) > 0.08 * w * h:
            return subject
    except cv2.error:
        pass
    yy, xx = np.ogrid[:h, :w]
    return (((xx - w * 0.5) / (w * 0.42)) ** 2 + ((yy - h * 0.48) / (h * 0.52)) ** 2 <= 1).astype(np.uint8) * 255


def suppress_bg(lineart: np.ndarray, subject: np.ndarray) -> np.ndarray:
    sub = cv2.dilate(subject, np.ones((9, 9), np.uint8), iterations=1)
    out = lineart.copy()
    out[sub == 0] = 255
    h, w = out.shape
    m = max(3, min(h, w) // 50)
    out[:m, :] = 255
    out[-m:, :] = 255
    out[:, :m] = 255
    out[:, -m:] = 255
    return out


def neighbors8(y, x, h, w):
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dx == 0 and dy == 0:
                continue
            ny, nx = y + dy, x + dx
            if 0 <= ny < h and 0 <= nx < w:
                yield ny, nx


def morphological_skeleton(ink: np.ndarray) -> np.ndarray:
    img = ink.copy()
    skel = np.zeros_like(img)
    el = cv2.getStructuringElement(cv2.MORPH_CROSS, (3, 3))
    while True:
        opened = cv2.morphologyEx(img, cv2.MORPH_OPEN, el)
        temp = cv2.subtract(img, opened)
        eroded = cv2.erode(img, el)
        skel = cv2.bitwise_or(skel, temp)
        img = eroded
        if cv2.countNonZero(img) == 0:
            break
    return skel


def edge_follow(ink: np.ndarray, min_pts: int = 5) -> List[PathPts]:
    h, w = ink.shape
    on = (ink > 0).astype(np.uint8)
    if cv2.countNonZero(on) == 0:
        return []
    kernel = np.array([[1, 1, 1], [1, 0, 1], [1, 1, 1]], dtype=np.uint8)
    deg = cv2.filter2D(on, -1, kernel) * on
    visited = np.zeros((h, w), dtype=bool)
    offs = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
    ys, xs = np.where((on > 0) & (deg <= 1))
    starts = list(zip(ys.tolist(), xs.tolist()))
    if len(starts) < 8:
        ys2, xs2 = np.where(on > 0)
        starts = list(zip(ys2.tolist()[::4], xs2.tolist()[::4]))
    paths: List[PathPts] = []
    for sy, sx in starts:
        if visited[sy, sx]:
            continue
        visited[sy, sx] = True
        chain: PathPts = [(float(sx), float(sy))]
        py, px = -1, -1
        cy, cx = int(sy), int(sx)
        while True:
            best = None
            best_sc = None
            for dy, dx in offs:
                ny, nx = cy + dy, cx + dx
                if ny < 0 or nx < 0 or ny >= h or nx >= w:
                    continue
                if not on[ny, nx] or visited[ny, nx]:
                    continue
                if ny == py and nx == px:
                    continue
                sc = 0.0
                if py >= 0:
                    vx, vy = cx - px, cy - py
                    wx, wy = nx - cx, ny - cy
                    sc = -(vx * wx + vy * wy)
                if best is None or sc < best_sc:
                    best, best_sc = (ny, nx), sc
            if best is None:
                break
            ny, nx = best
            visited[ny, nx] = True
            chain.append((float(nx), float(ny)))
            py, px = cy, cx
            cy, cx = ny, nx
        if len(chain) >= min_pts:
            paths.append(chain)
    return paths


def skeleton_walk(skel: np.ndarray) -> List[PathPts]:
    h, w = skel.shape
    on = skel > 0
    if not np.any(on):
        return []
    deg = np.zeros((h, w), dtype=np.uint8)
    ys, xs = np.where(on)
    for y, x in zip(ys, xs):
        deg[y, x] = sum(1 for ny, nx in neighbors8(y, x, h, w) if on[ny, nx])
    visited = set()

    def ek(a, b):
        return (a, b) if a <= b else (b, a)

    paths: List[PathPts] = []

    def walk(sy, sx, ny0, nx0):
        chain = [(float(sx), float(sy)), (float(nx0), float(ny0))]
        visited.add(ek((sy, sx), (ny0, nx0)))
        py, px = sy, sx
        cy, cx = ny0, nx0
        while True:
            if deg[cy, cx] != 2:
                break
            nxt = None
            for ny, nx in neighbors8(cy, cx, h, w):
                if not on[ny, nx] or (ny, nx) == (py, px):
                    continue
                if ek((cy, cx), (ny, nx)) in visited:
                    continue
                nxt = (ny, nx)
                break
            if not nxt:
                break
            ny, nx = nxt
            visited.add(ek((cy, cx), (ny, nx)))
            chain.append((float(nx), float(ny)))
            py, px = cy, cx
            cy, cx = ny, nx
        return chain

    seeds = [(int(y), int(x)) for y, x in zip(ys, xs) if deg[y, x] != 2] or [(int(ys[0]), int(xs[0]))]
    for y, x in seeds:
        for ny, nx in neighbors8(y, x, h, w):
            if not on[ny, nx]:
                continue
            if ek((y, x), (ny, nx)) in visited:
                continue
            ch = walk(y, x, ny, nx)
            if len(ch) >= 3:
                paths.append(ch)
    return paths


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


def light_chain(segments: List[PathPts], max_dist: float = 5.0, max_ang: float = 28.0) -> Tuple[List[PathPts], int]:
    """Mild chaining only — keep many separate strokes."""

    def tan(pts, start):
        if len(pts) < 2:
            return (1.0, 0.0)
        if start:
            a, b = pts[0], pts[min(4, len(pts) - 1)]
            vx, vy = a[0] - b[0], a[1] - b[1]
        else:
            a, b = pts[max(0, len(pts) - 5)], pts[-1]
            vx, vy = b[0] - a[0], b[1] - a[1]
        n = math.hypot(vx, vy) or 1.0
        return (vx / n, vy / n)

    def ang(u, v):
        return math.degrees(math.acos(max(-1.0, min(1.0, u[0] * v[0] + u[1] * v[1]))))

    paths = [list(s) for s in segments if len(s) >= 2]
    joins = 0
    # limit join rounds for speed / to avoid over-merging
    for _ in range(min(80, len(paths))):
        best = None
        for i in range(len(paths)):
            for j in range(i + 1, len(paths)):
                for a_s in (True, False):
                    for b_s in (True, False):
                        pa = paths[i][0] if a_s else paths[i][-1]
                        pb = paths[j][0] if b_s else paths[j][-1]
                        dist = math.hypot(pa[0] - pb[0], pa[1] - pb[1])
                        if dist > max_dist:
                            continue
                        ta, tb = tan(paths[i], a_s), tan(paths[j], b_s)
                        link = (pb[0] - pa[0], pb[1] - pa[1])
                        ln = math.hypot(*link) or 1.0
                        lu = (link[0] / ln, link[1] / ln)
                        aa, ab = ang(ta, lu), ang(tb, (-lu[0], -lu[1]))
                        if aa > max_ang or ab > max_ang:
                            continue
                        sc = dist + 0.1 * (aa + ab)
                        if best is None or sc < best[0]:
                            best = (sc, i, a_s, j, b_s)
        if not best:
            break
        _, i, a_s, j, b_s = best
        a = list(reversed(paths[i])) if a_s else list(paths[i])
        b = list(paths[j]) if b_s else list(reversed(paths[j]))
        merged = a + b[1:]
        paths = [merged if k == i else p for k, p in enumerate(paths) if k != j]
        joins += 1
    return paths, joins


def classify(pts: PathPts, w: int, h: int) -> str:
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    cy = (min(ys) + max(ys)) / 2 / h
    cx = (min(xs) + max(xs)) / 2 / w
    bw = (max(xs) - min(xs)) / w
    bh = (max(ys) - min(ys)) / h
    L = path_length(pts) / max(w, h)
    if L > 0.45 and bw > 0.25 and bh > 0.3:
        return "outline"
    if cy < 0.34:
        return "hair"
    if 0.22 <= cy <= 0.76 and abs(cx - 0.5) < 0.42:
        return "feature"
    if cy > 0.82:
        return "other"
    return "hair" if cy < 0.4 else "other"


def silhouette(subject: np.ndarray, simplify: float = 2.5) -> Optional[PathPts]:
    mask = cv2.morphologyEx(subject, cv2.MORPH_CLOSE, np.ones((11, 11), np.uint8))
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not cnts:
        return None
    cnt = max(cnts, key=cv2.contourArea)
    area = cv2.contourArea(cnt)
    frame = mask.shape[0] * mask.shape[1]
    if area < 0.08 * frame or area > 0.92 * frame:
        return None
    pts = [(float(p[0][0]), float(p[0][1])) for p in cnt]
    if pts and math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < 2:
        pts = pts[:-1]
    simp = douglas_peucker(pts, simplify)
    if simp and simp[0] != simp[-1]:
        simp = simp + [simp[0]]
    return simp


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


def tone_hatch_strokes(
    gray: np.ndarray,
    subject: np.ndarray,
    *,
    max_strokes: int = 120,
    seed: int = 7,
) -> List[dict]:
    """Free hatch strokes from dark tone zones — not edge contours."""
    rng = random.Random(seed)
    h, w = gray.shape
    # darker = lower value
    blur = cv2.GaussianBlur(gray, (0, 0), 2.5)
    dark = ((255 - blur).astype(np.float32) / 255.0) * (subject > 0)
    # ignore very light face areas
    dark[dark < 0.28] = 0
    # avoid extreme bottom shirt clutter
    dark[int(h * 0.88) :, :] = 0
    strokes = []
    # sample candidate seeds in dark zones
    ys, xs = np.where(dark > 0.35)
    if len(xs) == 0:
        return []
    idx = list(range(len(xs)))
    rng.shuffle(idx)
    used = np.zeros(dark.shape, dtype=np.uint8)
    for i in idx:
        if len(strokes) >= max_strokes:
            break
        x, y = int(xs[i]), int(ys[i])
        if used[y, x]:
            continue
        strength = float(dark[y, x])
        # direction: local gradient perpendicular (hatch across form)
        gy = float(blur[min(h - 1, y + 1), x]) - float(blur[max(0, y - 1), x])
        gx = float(blur[y, min(w - 1, x + 1)]) - float(blur[y, max(0, x - 1)])
        # hatch roughly along iso-brightness (perp to gradient)
        ang = math.atan2(-gx, gy) + rng.uniform(-0.35, 0.35)
        length = 10 + strength * 28 + rng.uniform(0, 8)
        # more strokes density in darker zones via shorter spacing of seeds
        n_layers = 1 + int(strength > 0.5) + int(strength > 0.65)
        for layer in range(n_layers):
            a = ang + layer * 0.4 + rng.uniform(-0.15, 0.15)
            half = length * (0.55 + 0.2 * layer)
            dx, dy = math.cos(a), math.sin(a)
            p0 = (x - dx * half, y - dy * half)
            p1 = (x + dx * half, y + dy * half)
            # clip roughly to subject
            mid = ((p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2)
            mx, my = int(mid[0]), int(mid[1])
            if not (0 <= mx < w and 0 <= my < h and subject[my, mx] > 0):
                continue
            pts = resample([p0, p1], spacing=2.2)
            strokes.append(
                {
                    "kind": "hatch",
                    "region": "tone",
                    "strength": round(strength, 3),
                    "points": pts,
                    "length": path_length(pts),
                }
            )
        cv2.circle(used, (x, y), 7, 1, -1)
    return strokes


def contour_paths(ink: np.ndarray, min_pts: int = 8) -> List[PathPts]:
    """Open stroke candidates from blob contours (stipple-friendly after dilate)."""
    cnts, _ = cv2.findContours(ink, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
    out: List[PathPts] = []
    for c in cnts:
        if len(c) < min_pts:
            continue
        pts = [(float(p[0][0]), float(p[0][1])) for p in c]
        # closed contour → open by cutting at farthest pair of similar-tangent points
        # simpler: take longest arc between two extremes along the contour
        if len(pts) < 3:
            continue
        # drop exact duplicate close
        if math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < 2:
            pts = pts[:-1]
        # split very long loops into 2–3 arcs so Sketchy gets multiple strokes
        n = len(pts)
        if n > 180:
            step = n // 3
            for i in range(3):
                chunk = pts[i * step : (i + 1) * step + 1]
                if len(chunk) >= min_pts:
                    out.append(chunk)
        elif n > 90:
            mid = n // 2
            out.append(pts[: mid + 1])
            out.append(pts[mid:])
        else:
            out.append(pts)
    return out


def build_structure_strokes(
    karte: np.ndarray,
    subject: np.ndarray,
    gray: Optional[np.ndarray] = None,
    *,
    min_len: float = 5.0,
) -> Tuple[List[dict], dict]:
    """Keep rich movement paths — mild cleanup only, no aggressive path reduction."""
    h, w = karte.shape
    # Soft maps (many gray levels) → late threshold; binary maps → classic 127
    if len(np.unique(karte)) > 8:
        thr = float(np.percentile(karte, 22))
        thr = float(np.clip(thr, 140, 235))
        ink = np.where(karte < thr, 255, 0).astype(np.uint8)
    else:
        ink = 255 - karte
        _, ink = cv2.threshold(ink, 127, 255, cv2.THRESH_BINARY)
    ink[subject == 0] = 0
    ink[int(h * 0.9) :, :] = 0

    # Connect stipple into drawable ribbons, then extract medial + contour paths
    linked = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8), iterations=2)
    linked = cv2.dilate(linked, np.ones((3, 3), np.uint8), iterations=1)

    raw: List[PathPts] = []
    raw.extend(contour_paths(linked, min_pts=6))
    thin = morphological_skeleton(linked)
    raw.extend(skeleton_walk(thin))
    raw.extend(edge_follow(linked, min_pts=4))

    # Extra structure from photo Canny (still movement paths, not a finished drawing)
    if gray is not None:
        edges = cv2.Canny(gray, 45, 130, L2gradient=True)
        edges[subject == 0] = 0
        edges[int(h * 0.9) :, :] = 0
        edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((2, 2), np.uint8), iterations=1)
        raw.extend(contour_paths(edges, min_pts=10))
        raw.extend(edge_follow(edges, min_pts=5))

    raw_n = len(raw)

    filtered: List[PathPts] = []
    for pts in raw:
        L = path_length(pts)
        if L < min_len:
            continue
        ys = [p[1] for p in pts]
        xs = [p[0] for p in pts]
        # drop tiny top-margin scrap / frame noise
        if min(ys) < h * 0.02 and max(ys) < h * 0.1 and L < 40:
            continue
        # drop near-zero-area speckles
        if (max(xs) - min(xs)) < 2 and (max(ys) - min(ys)) < 2:
            continue
        filtered.append(pts)

    # Mild local joins for stipple gaps — do not collapse into few master paths
    chained, joins = light_chain(filtered, max_dist=7.0, max_ang=32.0)

    outline = silhouette(subject, simplify=2.2)
    strokes: List[dict] = []
    if outline:
        # denser silhouette for Sketchy (less DP)
        strokes.append(
            {
                "kind": "outline",
                "region": "silhouette",
                "points": outline,
                "length": path_length(outline),
            }
        )

    for pts in chained:
        simp = douglas_peucker(pts, 0.65)  # keep organic detail
        if path_length(simp) < min_len:
            continue
        # skip near-duplicates of silhouette
        if outline and path_length(simp) > 0.55 * max(w, h):
            continue
        kind = classify(simp, w, h)
        cy = sum(p[1] for p in simp) / len(simp) / h
        cx = sum(p[0] for p in simp) / len(simp) / w
        region = "face"
        if kind == "hair":
            region = "hair"
        elif 0.22 <= cy <= 0.46 and abs(cx - 0.5) < 0.38:
            region = "eyes"
            kind = "feature"
        elif 0.40 <= cy <= 0.60 and abs(cx - 0.5) < 0.22:
            region = "nose"
            kind = "feature"
        elif 0.54 <= cy <= 0.72 and abs(cx - 0.5) < 0.28:
            region = "mouth"
            kind = "feature"
        elif cy > 0.72:
            region = "jaw"
        strokes.append(
            {
                "kind": kind,
                "region": region,
                "points": simp,
                "length": path_length(simp),
            }
        )

    # Dedup near-identical short strokes
    strokes = _dedup_strokes(strokes, min_sep=3.5)

    stats = {
        "rawSegments": raw_n,
        "afterFilter": len(filtered),
        "joins": joins,
        "structureStrokes": len(strokes),
    }
    return strokes, stats


def _dedup_strokes(strokes: List[dict], min_sep: float = 3.5) -> List[dict]:
    kept: List[dict] = []
    for s in sorted(strokes, key=lambda x: -x["length"]):
        pts = s["points"]
        mid = pts[len(pts) // 2]
        dup = False
        for k in kept:
            if k["kind"] != s["kind"]:
                continue
            kp = k["points"]
            km = kp[len(kp) // 2]
            if math.hypot(mid[0] - km[0], mid[1] - km[1]) < min_sep:
                # similar length & mid → skip
                if abs(k["length"] - s["length"]) < max(8.0, 0.35 * k["length"]):
                    dup = True
                    break
        if not dup:
            kept.append(s)
    return kept


def to_norm(pts: PathPts, w: int, h: int) -> List[List[float]]:
    return [[round(x / w, 5), round(y / h, 5)] for x, y in pts]


def write_strokeplan_preview(strokes: List[dict], w: int, h: int, path: Path) -> None:
    img = np.full((h, w, 3), (234, 241, 245), dtype=np.uint8)
    colors = {
        "outline": (40, 40, 180),
        "feature": (140, 90, 30),
        "hair": (140, 60, 140),
        "hatch": (90, 140, 90),
        "other": (100, 100, 100),
    }
    for s in strokes:
        pts = np.array([[int(round(x)), int(round(y))] for x, y in s["points"]], dtype=np.int32)
        if len(pts) < 2:
            continue
        col = colors.get(s["kind"], (80, 80, 80))
        thick = 2 if s["kind"] == "outline" else 1
        cv2.polylines(img, [pts], False, col, thick, cv2.LINE_AA)
    # legend strip
    cv2.putText(img, "stroke-plan: outline/feature/hair/hatch", (10, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (40, 40, 40), 1)
    cv2.imwrite(str(path), img)


def write_svg(strokes: List[dict], w: int, h: int, path: Path) -> None:
    colors = {
        "outline": "#c0392b",
        "feature": "#1a5276",
        "hair": "#6c3483",
        "hatch": "#1e8449",
        "other": "#7f8c8d",
    }
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">',
        '<rect width="100%" height="100%" fill="#f5f1ea"/>',
    ]
    for s in strokes:
        d = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in s["points"])
        parts.append(
            f'<path d="{d}" fill="none" stroke="{colors.get(s["kind"], "#333")}" '
            f'stroke-width="1.1" stroke-linecap="round" data-kind="{s["kind"]}" data-region="{s.get("region","")}"/>'
        )
    parts.append("</svg>")
    path.write_text("\n".join(parts), encoding="utf-8")


def process(photo: Path, outdir: Path, karte_path: Optional[Path] = None) -> dict:
    outdir.mkdir(parents=True, exist_ok=True)
    # Native load for adaptive (avoid fixed max_side=900 path for source generation)
    bgr_native = cv2.imread(str(photo), cv2.IMREAD_COLOR)
    if bgr_native is None:
        raise SystemExit(f"Cannot read {photo}")

    adaptive_meta = None
    if karte_path and karte_path.exists():
        bgr, gray = load_portrait(photo)
        h, w = gray.shape
        subject = subject_mask(bgr, gray)
        karte = cv2.imread(str(karte_path), cv2.IMREAD_GRAYSCALE)
        if karte.shape[:2] != (h, w):
            karte = cv2.resize(karte, (w, h), interpolation=cv2.INTER_AREA)
        source_name = str(karte_path)
        karte = suppress_bg(karte, subject)
    else:
        # Adaptive soft multi-scale source (generic Photo→Sketch)
        try:
            from adaptive_source import build_adaptive_source  # type: ignore

            karte, adaptive_meta = build_adaptive_source(bgr_native, suppress_background=True)
            source_name = "adaptive-soft-multiscale"
            h, w = karte.shape
            bgr = cv2.resize(bgr_native, (w, h), interpolation=cv2.INTER_AREA)
            gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
            subject = subject_mask(bgr, gray)
        except Exception:
            bgr, gray = load_portrait(photo)
            h, w = gray.shape
            subject = subject_mask(bgr, gray)
            karte = hybrid_karte(gray)
            source_name = "generated-hybrid-karte-fallback"
            karte = suppress_bg(karte, subject)

    cv2.imwrite(str(outdir / "zeichnkarte-source.png"), karte)
    cv2.imwrite(str(outdir / "zeichnkarte-at-style.png"), karte)
    if adaptive_meta:
        (outdir / "zeichnkarte-adaptive-meta.json").write_text(
            json.dumps(adaptive_meta, indent=2), encoding="utf-8"
        )

    structure, stats = build_structure_strokes(karte, subject, gray, min_len=5.0)
    hatches = tone_hatch_strokes(gray, subject, max_strokes=110)

    # Cap only extreme noise count, not artistic richness
    structure = sorted(structure, key=lambda s: -s["length"])
    if len(structure) > 360:
        structure = structure[:360]
    all_strokes = structure + [
        {
            "kind": "hatch",
            "region": h.get("region", "tone"),
            "points": h["points"],
            "length": h["length"],
            "strength": h.get("strength", 0),
        }
        for h in hatches
    ]

    write_strokeplan_preview(all_strokes, w, h, outdir / "portrait-strokeplan-preview.png")
    write_svg(all_strokes, w, h, outdir / "portrait-strokeplan-preview.svg")
    # structure-only preview for test A
    write_strokeplan_preview(structure, w, h, outdir / "portrait-strokeplan-a.png")
    write_svg(structure, w, h, outdir / "portrait-strokeplan-a.svg")

    def pack(strokes: List[dict]) -> List[dict]:
        out = []
        for i, s in enumerate(strokes):
            pts = s["points"]
            # densify moderately for Sketchy (pixel spacing ~2.5)
            pts = resample(pts, spacing=2.5)
            out.append(
                {
                    "id": i,
                    "kind": s["kind"],
                    "region": s.get("region", ""),
                    "length": round(path_length(pts), 2),
                    "nPoints": len(pts),
                    "strength": s.get("strength"),
                    "points": to_norm(pts, w, h),
                }
            )
        return out

    structure_pack = pack(structure)
    hatch_pack = pack(
        [
            {
                "kind": "hatch",
                "region": "tone",
                "points": h["points"],
                "length": h["length"],
                "strength": h.get("strength"),
            }
            for h in hatches
        ]
    )

    plan = {
        "width": w,
        "height": h,
        "sourcePhoto": str(photo),
        "sourceKarte": source_name,
        "philosophy": "Zeichenkarte only — Sketchy creates the drawing",
        "stats": {
            **stats,
            "structureStrokes": len(structure_pack),
            "hairStrokes": sum(1 for s in structure_pack if s["kind"] == "hair"),
            "featureStrokes": sum(1 for s in structure_pack if s["kind"] == "feature"),
            "outlineStrokes": sum(1 for s in structure_pack if s["kind"] == "outline"),
            "hatchStrokes": len(hatch_pack),
            "avgPointsStructure": round(
                float(np.mean([s["nPoints"] for s in structure_pack])) if structure_pack else 0, 1
            ),
            "avgPointsHatch": round(float(np.mean([s["nPoints"] for s in hatch_pack])) if hatch_pack else 0, 1),
        },
        "structure": structure_pack,
        "hatch": hatch_pack,
        "sketchyDefaults": {
            "mode": "sketchy",
            "sizeA": 1.8,
            "sizeB": 1.8,
            "sizeHatch": 1.4,
            "densifyStepA": 0.007,
            "densifyStepB": 0.0035,
            "passesA": 1,
            "passesB": {"eyes": 2, "outline": 2, "default": 1},
        },
    }
    (outdir / "portrait-strokeplan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    # mark plot allowed if we have outline + some features
    ready = (
        plan["stats"]["outlineStrokes"] >= 1
        and plan["stats"]["featureStrokes"] >= 10
        and plan["stats"]["structureStrokes"] >= 60
    )
    (outdir / "PLOT_READY").write_text("yes\n" if ready else "no\n", encoding="utf-8")
    summary = {
        "ready": ready,
        "karte": str(outdir / "zeichnkarte-source.png"),
        "strokeplan": str(outdir / "portrait-strokeplan.json"),
        "previewA": str(outdir / "portrait-strokeplan-a.png"),
        "previewFull": str(outdir / "portrait-strokeplan-preview.png"),
        "stats": plan["stats"],
    }
    (outdir / "zeichnkarte-summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--photo", type=Path, default=Path(PHOTO_DEFAULT))
    ap.add_argument("--outdir", type=Path, default=Path("tmp/portrait-preprocess"))
    ap.add_argument("--karte", type=Path, default=None, help="Optional existing lineart PNG (e.g. at-in.png)")
    args = ap.parse_args()
    # Prefer denser hybrid over sparse at-in if no explicit karte
    karte = args.karte
    if karte is None:
        hybrid = args.outdir / "portrait-lineart-hybrid.png"
        candidate = args.outdir / "at-in.png"
        if hybrid.exists():
            karte = hybrid
        elif candidate.exists():
            karte = candidate
    print(json.dumps(process(args.photo, args.outdir, karte), indent=2))


if __name__ == "__main__":
    main()
