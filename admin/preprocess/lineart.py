"""
Portrait line-art → stroke-path pipeline (admin only).

Focus: long, organic, drawable centerline strokes via skeleton walk +
direction-aware path chaining. No KrenzSketch plotting here.

Pipeline:
  photo → Canny | tuned XDoG | region hybrid → BG suppress
       → morphological skeleton (centerline)
       → raw polylines
       → filter + path chaining (distance + tangent)
       → Chaikin / Douglas–Peucker
       → SVG/PNG preview + stats

Also compares AutoTrace -centerline on the same line-art.

Licenses (admin-only):
  - OpenCV: Apache-2.0
  - NumPy: BSD
  - AutoTrace: GPL-2.0+ (CLI, local)
  - Potrace: GPL-2.0+ (optional compare)
  - XDoG: Winnemöller et al., CAG 2012 (local reimplementation)
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

Point = Tuple[float, float]
PathPts = List[Point]

# Tuned defaults (documented in summary)
DEFAULT_MAX_JOIN = 10.0  # px
DEFAULT_MAX_ANGLE = 32.0  # degrees — avoid scribble joins across features
DEFAULT_MIN_LEN = 22.0
DEFAULT_MIN_FEATURE = 14.0
DEFAULT_SIMPLIFY = 1.4


@dataclass
class StrokePath:
    points: PathPts
    length: float = 0.0
    kind: str = "other"
    source: str = ""

    def __post_init__(self) -> None:
        if not self.length:
            self.length = path_length(self.points)


@dataclass
class PipelineStats:
    raw_segments: int = 0
    after_filter: int = 0
    joins: int = 0
    final_paths: int = 0
    mean_len: float = 0.0
    median_len: float = 0.0
    longest: float = 0.0
    under_10: int = 0
    under_20: int = 0
    max_join_distance: float = DEFAULT_MAX_JOIN
    max_angle_deg: float = DEFAULT_MAX_ANGLE
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "rawSegments": self.raw_segments,
            "afterFilter": self.after_filter,
            "joins": self.joins,
            "finalPaths": self.final_paths,
            "meanLength": round(self.mean_len, 2),
            "medianLength": round(self.median_len, 2),
            "longest": round(self.longest, 2),
            "pathsUnder10px": self.under_10,
            "pathsUnder20px": self.under_20,
            "maxJoinDistance": self.max_join_distance,
            "maxAngleDeg": self.max_angle_deg,
            "notes": self.notes,
        }


def ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def path_length(pts: Sequence[Point]) -> float:
    if len(pts) < 2:
        return 0.0
    return float(
        sum(math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]) for i in range(1, len(pts)))
    )


def load_portrait(path: Path, max_side: int = 900) -> Tuple[np.ndarray, np.ndarray]:
    bgr = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if bgr is None:
        raise SystemExit(f"Could not read image: {path}")
    h, w = bgr.shape[:2]
    if max(h, w) > max_side:
        scale = max_side / float(max(h, w))
        bgr = cv2.resize(bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    gray = clahe.apply(gray)
    gray = cv2.GaussianBlur(gray, (0, 0), 0.7)
    return bgr, gray


# --- Line-art ---------------------------------------------------------------


def xdog(
    gray: np.ndarray,
    sigma: float = 0.95,
    k_sigma: float = 1.6,
    gamma: float = 0.97,
    epsilon: float = -0.05,
    phi: float = 25.0,
) -> np.ndarray:
    """XDoG → dark lines on light; light morphological close for continuity."""
    g = gray.astype(np.float32) / 255.0
    g1 = cv2.GaussianBlur(g, (0, 0), sigma)
    g2 = cv2.GaussianBlur(g, (0, 0), sigma * k_sigma)
    dog = g1 - gamma * g2
    dog_n = dog / (np.abs(dog).max() + 1e-8)
    e = 1.0 + np.tanh(phi * (dog_n - epsilon))
    e = np.clip(e, 0.0, 1.0)
    out = ((1.0 - e) * 255.0).astype(np.uint8)
    _, bw = cv2.threshold(out, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if np.mean(bw) < 127:
        bw = 255 - bw
    ink = 255 - bw
    ink = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8), iterations=1)
    return 255 - ink


def canny_lineart(gray: np.ndarray, low: int = 45, high: int = 130) -> np.ndarray:
    edges = cv2.Canny(gray, low, high, L2gradient=True)
    # Light close to reconnect lids etc.
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((2, 2), np.uint8), iterations=1)
    return 255 - edges


def hybrid_lineart(gray: np.ndarray) -> np.ndarray:
    """Pixel hybrid: XDoG face continuity + Canny structure."""
    x = xdog(gray)
    c = canny_lineart(gray)
    combo = cv2.max(255 - x, 255 - c)
    combo = cv2.morphologyEx(combo, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    return 255 - combo


# --- Subject / outline ------------------------------------------------------


def subject_mask(gray: np.ndarray, bgr: Optional[np.ndarray] = None) -> np.ndarray:
    h, w = gray.shape
    if bgr is not None:
        mask = np.zeros(gray.shape, np.uint8)
        rect = (int(w * 0.08), int(h * 0.01), int(w * 0.84), int(h * 0.97))
        bgd = np.zeros((1, 65), np.float64)
        fgd = np.zeros((1, 65), np.float64)
        try:
            cv2.grabCut(bgr, mask, rect, bgd, fgd, 2, cv2.GC_INIT_WITH_RECT)
            subject = np.where((mask == 2) | (mask == 0), 0, 255).astype(np.uint8)
            subject = cv2.morphologyEx(subject, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
            subject = cv2.morphologyEx(subject, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
            if cv2.countNonZero(subject) > 0.08 * w * h:
                return subject
        except cv2.error:
            pass
    border = np.concatenate([gray[0, :], gray[-1, :], gray[:, 0], gray[:, -1]])
    bg = float(np.median(border))
    dist = np.abs(gray.astype(np.float32) - bg)
    subject = (dist > 16).astype(np.uint8) * 255
    yy, xx = np.ogrid[:h, :w]
    center = (((xx - w * 0.5) / (w * 0.42)) ** 2 + ((yy - h * 0.48) / (h * 0.52)) ** 2) <= 1.0
    subject = np.where(center, subject, 0).astype(np.uint8)
    subject = cv2.morphologyEx(subject, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    n, labels, stats, _ = cv2.connectedComponentsWithStats((subject > 0).astype(np.uint8), 8)
    if n > 1:
        keep = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        subject = np.where(labels == keep, 255, 0).astype(np.uint8)
    return subject


def suppress_background(
    line_dark_on_light: np.ndarray, gray: np.ndarray, bgr: Optional[np.ndarray] = None
) -> np.ndarray:
    h, w = gray.shape
    subject = subject_mask(gray, bgr)
    subject = cv2.dilate(subject, np.ones((11, 11), np.uint8), iterations=1)
    out = line_dark_on_light.copy()
    out[subject == 0] = 255
    m = max(4, min(h, w) // 40)
    out[:m, :] = 255
    out[-m:, :] = 255
    out[:, :m] = 255
    out[:, -m:] = 255
    return out


def silhouette_outline(
    gray: np.ndarray, simplify: float, bgr: Optional[np.ndarray] = None
) -> Optional[PathPts]:
    mask = subject_mask(gray, bgr)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((11, 11), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return None
    cnt = max(contours, key=cv2.contourArea)
    area = cv2.contourArea(cnt)
    frame = gray.shape[0] * gray.shape[1]
    if area < 0.08 * frame or area > 0.92 * frame:
        return None
    pts = [(float(p[0][0]), float(p[0][1])) for p in cnt]
    if len(pts) > 2 and math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < 2:
        pts = pts[:-1]
    # Resample lightly then simplify AFTER (caller may re-simplify)
    simplified = douglas_peucker(pts, max(simplify, 2.4))
    if simplified and simplified[0] != simplified[-1]:
        simplified = simplified + [simplified[0]]
    return simplified


# --- Skeleton / centerline --------------------------------------------------


def morphological_skeleton(binary_white_lines: np.ndarray) -> np.ndarray:
    img = binary_white_lines.copy()
    skel = np.zeros_like(img)
    element = cv2.getStructuringElement(cv2.MORPH_CROSS, (3, 3))
    while True:
        opened = cv2.morphologyEx(img, cv2.MORPH_OPEN, element)
        temp = cv2.subtract(img, opened)
        eroded = cv2.erode(img, element)
        skel = cv2.bitwise_or(skel, temp)
        img = eroded
        if cv2.countNonZero(img) == 0:
            break
    return skel


def neighbors8(y: int, x: int, h: int, w: int):
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dx == 0 and dy == 0:
                continue
            ny, nx = y + dy, x + dx
            if 0 <= ny < h and 0 <= nx < w:
                yield ny, nx


def edge_follow_polylines(binary_white: np.ndarray, min_points: int = 8) -> List[PathPts]:
    """
    Follow 1-pixel edge chains preferring direction continuity.
    Optimized: only start at endpoints; skip already-visited pixels.
    """
    h, w = binary_white.shape
    on = (binary_white > 0).astype(np.uint8)
    if cv2.countNonZero(on) == 0:
        return []

    # 8-neighbor count via convolution
    kernel = np.array([[1, 1, 1], [1, 0, 1], [1, 1, 1]], dtype=np.uint8)
    deg = cv2.filter2D(on, -1, kernel)
    deg = deg * on

    visited = np.zeros((h, w), dtype=bool)
    paths: List[PathPts] = []
    offs = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]

    ys, xs = np.where((on > 0) & (deg <= 1))
    starts = list(zip(ys.tolist(), xs.tolist()))
    if len(starts) < 4:
        ys2, xs2 = np.where(on > 0)
        starts = list(zip(ys2.tolist()[::3], xs2.tolist()[::3]))

    for sy, sx in starts:
        if visited[sy, sx]:
            continue
        visited[sy, sx] = True
        chain: PathPts = [(float(sx), float(sy))]
        py, px = -1, -1
        cy, cx = int(sy), int(sx)
        while True:
            best = None
            best_score = None
            for dy, dx in offs:
                ny, nx = cy + dy, cx + dx
                if ny < 0 or nx < 0 or ny >= h or nx >= w:
                    continue
                if not on[ny, nx] or visited[ny, nx]:
                    continue
                if ny == py and nx == px:
                    continue
                score = 0.0
                if py >= 0:
                    vx, vy = cx - px, cy - py
                    wx, wy = nx - cx, ny - cy
                    score = -(vx * wx + vy * wy)
                if best is None or score < best_score:
                    best = (ny, nx)
                    best_score = score
            if best is None:
                break
            ny, nx = best
            visited[ny, nx] = True
            chain.append((float(nx), float(ny)))
            py, px = cy, cx
            cy, cx = ny, nx
        if len(chain) >= min_points:
            paths.append(chain)
    return paths


def skeleton_polylines(skel: np.ndarray) -> List[PathPts]:
    """
    Walk 1px skeleton into open polylines between endpoints/junctions.
    Junctions split branches (chaining reconnects coherent ones later).
    """
    h, w = skel.shape
    on = skel > 0
    if not np.any(on):
        return []

    deg = np.zeros((h, w), dtype=np.uint8)
    ys, xs = np.where(on)
    for y, x in zip(ys, xs):
        d = sum(1 for ny, nx in neighbors8(y, x, h, w) if on[ny, nx])
        deg[y, x] = d

    visited_edges: set = set()

    def edge_key(a, b):
        return (a, b) if a <= b else (b, a)

    paths: List[PathPts] = []

    def walk_from(sy: int, sx: int, ny0: int, nx0: int) -> PathPts:
        chain: PathPts = [(float(sx), float(sy)), (float(nx0), float(ny0))]
        visited_edges.add(edge_key((sy, sx), (ny0, nx0)))
        py, px = sy, sx
        cy, cx = ny0, nx0
        while True:
            if deg[cy, cx] != 2:
                break
            nxt = None
            for ny, nx in neighbors8(cy, cx, h, w):
                if not on[ny, nx]:
                    continue
                if (ny, nx) == (py, px):
                    continue
                ek = edge_key((cy, cx), (ny, nx))
                if ek in visited_edges:
                    continue
                nxt = (ny, nx)
                break
            if nxt is None:
                break
            ny, nx = nxt
            visited_edges.add(edge_key((cy, cx), (ny, nx)))
            chain.append((float(nx), float(ny)))
            py, px = cy, cx
            cy, cx = ny, nx
        return chain

    # From each endpoint / junction, start unused branches
    seeds = [(int(y), int(x)) for y, x in zip(ys, xs) if deg[y, x] != 2]
    if not seeds:
        seeds = [(int(ys[0]), int(xs[0]))]

    for y, x in seeds:
        for ny, nx in neighbors8(y, x, h, w):
            if not on[ny, nx]:
                continue
            ek = edge_key((y, x), (ny, nx))
            if ek in visited_edges:
                continue
            chain = walk_from(y, x, ny, nx)
            if len(chain) >= 3:
                paths.append(chain)

    # Leftover loops (all deg==2)
    for y, x in zip(ys, xs):
        y, x = int(y), int(x)
        for ny, nx in neighbors8(y, x, h, w):
            if not on[ny, nx]:
                continue
            ek = edge_key((y, x), (ny, nx))
            if ek in visited_edges:
                continue
            chain = walk_from(y, x, ny, nx)
            if len(chain) >= 3:
                paths.append(chain)

    return paths


# --- Geometry helpers -------------------------------------------------------


def douglas_peucker(pts: PathPts, epsilon: float) -> PathPts:
    if len(pts) < 3:
        return list(pts)

    def _d(p, a, b):
        x, y = a
        dx, dy = b[0] - x, b[1] - y
        if dx == 0 and dy == 0:
            return math.hypot(p[0] - x, p[1] - y)
        t = max(0.0, min(1.0, ((p[0] - x) * dx + (p[1] - y) * dy) / (dx * dx + dy * dy)))
        return math.hypot(p[0] - (x + t * dx), p[1] - (y + t * dy))

    def rec(chunk: PathPts) -> PathPts:
        if len(chunk) < 3:
            return chunk
        a, b = chunk[0], chunk[-1]
        idx, md = 0, -1.0
        for i in range(1, len(chunk) - 1):
            d = _d(chunk[i], a, b)
            if d > md:
                md, idx = d, i
        if md > epsilon:
            return rec(chunk[: idx + 1])[:-1] + rec(chunk[idx:])
        return [a, b]

    return rec(list(pts))


def chaikin(pts: PathPts, iterations: int = 1) -> PathPts:
    if len(pts) < 3 or iterations <= 0:
        return list(pts)
    cur = list(pts)
    closed = math.hypot(cur[0][0] - cur[-1][0], cur[0][1] - cur[-1][1]) < 1.5
    for _ in range(iterations):
        if closed and cur[0] != cur[-1]:
            cur = cur + [cur[0]]
        nxt: PathPts = []
        n = len(cur) - 1
        for i in range(n):
            p, q = cur[i], cur[i + 1]
            nxt.append((0.75 * p[0] + 0.25 * q[0], 0.75 * p[1] + 0.25 * q[1]))
            nxt.append((0.25 * p[0] + 0.75 * q[0], 0.25 * p[1] + 0.75 * q[1]))
        if closed:
            nxt.append(nxt[0])
        else:
            # keep endpoints
            nxt = [cur[0]] + nxt + [cur[-1]]
        cur = nxt
    return cur


def end_tangent(pts: PathPts, at_start: bool, window: int = 6) -> Point:
    if len(pts) < 2:
        return (1.0, 0.0)
    if at_start:
        a = pts[0]
        b = pts[min(window, len(pts) - 1)]
        vx, vy = b[0] - a[0], b[1] - a[1]
        # outward tangent at start points opposite to path direction
        vx, vy = -vx, -vy
    else:
        a = pts[max(0, len(pts) - 1 - window)]
        b = pts[-1]
        vx, vy = b[0] - a[0], b[1] - a[1]
    n = math.hypot(vx, vy) or 1.0
    return (vx / n, vy / n)


def angle_deg(u: Point, v: Point) -> float:
    dot = max(-1.0, min(1.0, u[0] * v[0] + u[1] * v[1]))
    return math.degrees(math.acos(dot))


def classify_path(pts: PathPts, w: int, h: int) -> str:
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    cx = (minx + maxx) / 2 / w
    cy = (miny + maxy) / 2 / h
    bw = (maxx - minx) / w
    bh = (maxy - miny) / h
    length = path_length(pts) / max(w, h)
    if length > 0.5 and bw > 0.25 and bh > 0.35:
        return "outline"
    if cy < 0.34 and length > 0.035:
        return "hair"
    if 0.22 <= cy <= 0.55 and abs(cx - 0.5) < 0.42:
        return "feature"
    if 0.48 <= cy <= 0.76 and abs(cx - 0.5) < 0.34:
        return "feature"
    if cy > 0.82:
        return "other"
    # no shadow category in this stage
    return "other"


# --- Chaining ---------------------------------------------------------------


def join_score(
    a: PathPts,
    a_at_start: bool,
    b: PathPts,
    b_at_start: bool,
    max_dist: float,
    max_angle: float,
) -> Optional[float]:
    pa = a[0] if a_at_start else a[-1]
    pb = b[0] if b_at_start else b[-1]
    dist = math.hypot(pa[0] - pb[0], pa[1] - pb[1])
    if dist > max_dist:
        return None
    ta = end_tangent(a, a_at_start)
    tb = end_tangent(b, b_at_start)
    # link vector from a-end to b-end should follow a's outward tangent
    link = (pb[0] - pa[0], pb[1] - pa[1])
    ln = math.hypot(link[0], link[1]) or 1.0
    link_u = (link[0] / ln, link[1] / ln)
    ang_a = angle_deg(ta, link_u)
    ang_b = angle_deg(tb, (-link_u[0], -link_u[1]))
    if ang_a > max_angle or ang_b > max_angle:
        return None
    # Reject near-orthogonal jumps even if under max
    if ang_a + ang_b > max_angle * 1.6:
        return None
    return dist + 0.085 * (ang_a + ang_b) + 0.02 * abs(ang_a - ang_b)


def chain_paths(
    segments: List[PathPts],
    max_dist: float = DEFAULT_MAX_JOIN,
    max_angle: float = DEFAULT_MAX_ANGLE,
) -> Tuple[List[PathPts], int]:
    """Greedy endpoint chaining. Returns (chained_paths, join_count)."""
    paths = [list(s) for s in segments if len(s) >= 2]
    joins = 0
    changed = True
    while changed:
        changed = False
        best = None  # (score, i, i_start, j, j_start)
        for i in range(len(paths)):
            for j in range(i + 1, len(paths)):
                for a_start in (True, False):
                    for b_start in (True, False):
                        sc = join_score(paths[i], a_start, paths[j], b_start, max_dist, max_angle)
                        if sc is None:
                            continue
                        if best is None or sc < best[0]:
                            best = (sc, i, a_start, j, b_start)
        if best is None:
            break
        _, i, a_start, j, b_start = best
        a = list(reversed(paths[i])) if a_start else list(paths[i])
        b = list(paths[j]) if b_start else list(reversed(paths[j]))
        merged = a + b[1:]
        new_paths = []
        for k, p in enumerate(paths):
            if k == i:
                new_paths.append(merged)
            elif k == j:
                continue
            else:
                new_paths.append(p)
        paths = new_paths
        joins += 1
        changed = True
    return paths, joins


# --- Filter / stats ---------------------------------------------------------


def split_at_sharp_corners(pts: PathPts, max_turn_deg: float = 58.0) -> List[PathPts]:
    """Split a polyline where the local turn is too sharp (anti-scribble)."""
    if len(pts) < 4:
        return [list(pts)]
    cuts = [0]
    for i in range(1, len(pts) - 1):
        ax, ay = pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]
        bx, by = pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]
        na, nb = math.hypot(ax, ay), math.hypot(bx, by)
        if na < 1e-6 or nb < 1e-6:
            continue
        turn = angle_deg((ax / na, ay / na), (bx / nb, by / nb))
        if turn > max_turn_deg:
            cuts.append(i)
    cuts.append(len(pts) - 1)
    out: List[PathPts] = []
    for a, b in zip(cuts, cuts[1:]):
        chunk = pts[a : b + 1]
        if len(chunk) >= 2:
            out.append(chunk)
    return out if out else [list(pts)]


def mean_turn_deg(pts: PathPts) -> float:
    if len(pts) < 3:
        return 0.0
    turns = []
    for i in range(1, len(pts) - 1):
        ax, ay = pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]
        bx, by = pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]
        na, nb = math.hypot(ax, ay), math.hypot(bx, by)
        if na < 1e-6 or nb < 1e-6:
            continue
        turns.append(angle_deg((ax / na, ay / na), (bx / nb, by / nb)))
    return float(np.mean(turns)) if turns else 0.0


def face_band_keep(pts: PathPts, w: int, h: int, min_len: float, min_feature: float) -> bool:
    length = path_length(pts)
    cy = sum(p[1] for p in pts) / len(pts) / h
    cx = sum(p[0] for p in pts) / len(pts) / w
    in_face = 0.2 <= cy <= 0.78 and abs(cx - 0.5) < 0.45
    if in_face and length >= min_feature:
        return True
    if length >= min_len:
        return True
    return False


def filter_segments(
    segments: List[PathPts], w: int, h: int, min_len: float, min_feature: float
) -> List[PathPts]:
    kept = []
    for pts in segments:
        if len(pts) < 2:
            continue
        ys = [p[1] for p in pts]
        xs = [p[0] for p in pts]
        # drop ceiling/frame scraps
        if min(ys) < h * 0.04 and max(ys) < h * 0.14:
            continue
        # drop busy shirt pattern zone (keep only long near-outline pieces)
        cy = sum(ys) / len(ys) / h
        if cy > 0.84 and path_length(pts) < min_len * 2.5:
            continue
        if not face_band_keep(pts, w, h, min_len, min_feature):
            continue
        # drop near-zero bbox noise
        if max(xs) - min(xs) < 1.5 and max(ys) - min(ys) < 1.5:
            continue
        kept.append(pts)
    return kept


def finalize_paths(
    segments: List[PathPts],
    w: int,
    h: int,
    source: str,
    simplify: float,
    max_paths: int,
    outline: Optional[PathPts] = None,
    min_len: float = DEFAULT_MIN_LEN,
    min_feature: float = DEFAULT_MIN_FEATURE,
) -> List[StrokePath]:
    out: List[StrokePath] = []
    if outline and path_length(outline) > 40:
        out.append(StrokePath(outline, path_length(outline), "outline", source))

    for pts in segments:
        smooth = chaikin(pts, iterations=1)
        simp = douglas_peucker(smooth, simplify)
        if len(simp) < 2:
            continue
        length = path_length(simp)
        # Cap absurd loops / over-merged strokes
        if length > 0.85 * (w + h):
            continue
        kind = classify_path(simp, w, h)
        if kind == "shadow":
            continue
        # Drop leftover short fragments after chaining
        cy = sum(p[1] for p in simp) / len(simp) / h
        cx = sum(p[0] for p in simp) / len(simp) / w
        in_face = 0.22 <= cy <= 0.76 and abs(cx - 0.5) < 0.42
        if length < min_len and not (in_face and length >= min_feature and kind == "feature"):
            continue
        if length < min_feature:
            continue
        out.append(StrokePath(simp, length, kind, source))

    # Drop shadow/other shirt clutter; keep outline/feature/hair preferentially
    rank = {"outline": 0, "feature": 1, "hair": 2, "other": 3}
    out.sort(key=lambda p: (rank.get(p.kind, 9), -p.length))
    kept: List[StrokePath] = []
    other_n = 0
    for p in out:
        if p.kind == "other":
            other_n += 1
            if other_n > 12:
                continue
        kept.append(p)
        if len(kept) >= max_paths:
            break
    return kept


def compute_stats(raw_n: int, filtered: List[PathPts], joins: int, final: List[StrokePath], max_join: float, max_ang: float) -> PipelineStats:
    lengths = [path_length(p) for p in filtered] if filtered else [0.0]
    fl = [p.length for p in final] or [0.0]
    return PipelineStats(
        raw_segments=raw_n,
        after_filter=len(filtered),
        joins=joins,
        final_paths=len(final),
        mean_len=float(np.mean(fl)),
        median_len=float(np.median(fl)),
        longest=float(max(fl)),
        under_10=sum(1 for L in fl if L < 10),
        under_20=sum(1 for L in fl if L < 20),
        max_join_distance=max_join,
        max_angle_deg=max_ang,
    )


# --- Extract centerline paths from line-art ---------------------------------


def lineart_to_ink(line_dark_on_light: np.ndarray, reconnect: int = 1) -> np.ndarray:
    """
    Binary ink (white strokes). reconnect=1 closes tiny gaps without
    bloating into blobs (heavy dilate creates bad medial-axis zigzags).
    """
    ink = 255 - line_dark_on_light
    _, ink = cv2.threshold(ink, 127, 255, cv2.THRESH_BINARY)
    if cv2.countNonZero(ink) < 40:
        _, ink = cv2.threshold(255 - line_dark_on_light, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if reconnect > 0:
        ink = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8), iterations=reconnect)
        # single-pixel bridge for near-touching endpoints
        ink = cv2.dilate(ink, np.ones((2, 2), np.uint8), iterations=1)
        ink = cv2.erode(ink, np.ones((2, 2), np.uint8), iterations=1)
    return ink


def hough_band_paths(
    ink: np.ndarray,
    y0: float,
    y1: float,
    *,
    min_len: int = 14,
    max_gap: int = 10,
    threshold: int = 18,
    mode: str = "horizontal",
) -> List[PathPts]:
    """Extract relatively straight strokes in a face band via HoughLinesP."""
    h, w = ink.shape
    ya, yb = int(h * y0), int(h * y1)
    roi = np.zeros_like(ink)
    roi[ya:yb, int(w * 0.15) : int(w * 0.85)] = ink[ya:yb, int(w * 0.15) : int(w * 0.85)]
    lines = cv2.HoughLinesP(
        roi,
        rho=1,
        theta=np.pi / 180,
        threshold=threshold,
        minLineLength=min_len,
        maxLineGap=max_gap,
    )
    out: List[PathPts] = []
    if lines is None:
        return out
    for line in lines:
        x1, y1_, x2, y2_ = map(float, line[0])
        ang = abs(math.degrees(math.atan2(y2_ - y1_, x2 - x1))) % 180
        if mode == "horizontal" and not (ang <= 32 or ang >= 148):
            continue
        if mode == "vertical" and not (55 <= ang <= 125):
            continue
        out.append([(x1, y1_), (x2, y2_)])
    return out


def extract_centerline_paths(
    line_dark_on_light: np.ndarray,
    gray: np.ndarray,
    source: str,
    *,
    simplify: float = DEFAULT_SIMPLIFY,
    min_len: float = DEFAULT_MIN_LEN,
    min_feature: float = DEFAULT_MIN_FEATURE,
    max_paths: int = 80,
    max_join: float = DEFAULT_MAX_JOIN,
    max_angle: float = DEFAULT_MAX_ANGLE,
    bgr: Optional[np.ndarray] = None,
    face_only_ink: bool = False,
) -> Tuple[List[StrokePath], PipelineStats]:
    h, w = line_dark_on_light.shape
    ink = lineart_to_ink(line_dark_on_light, reconnect=1)
    ink[int(h * 0.86) :, :] = 0
    if face_only_ink:
        mask = np.zeros_like(ink)
        mask[int(h * 0.18) : int(h * 0.78), int(w * 0.12) : int(w * 0.88)] = 255
        ink = cv2.bitwise_and(ink, mask)

    # Prefer skeleton centerlines; edge-follow only when ink is sparse (XDoG-like)
    thin = morphological_skeleton(ink)
    raw: List[PathPts] = []
    if cv2.countNonZero(thin) >= 40:
        raw.extend(skeleton_polylines(thin))
    ink_density = cv2.countNonZero(ink) / float(ink.size)
    if ink_density < 0.08:
        raw.extend(edge_follow_polylines(ink, min_points=6))
    # Hough strokes in face bands (clean lids / mouth / nose hints)
    raw.extend(hough_band_paths(ink, 0.26, 0.46, min_len=16, max_gap=12, threshold=16, mode="horizontal"))
    raw.extend(hough_band_paths(ink, 0.56, 0.72, min_len=14, max_gap=10, threshold=14, mode="horizontal"))
    raw.extend(hough_band_paths(ink, 0.40, 0.62, min_len=12, max_gap=8, threshold=14, mode="vertical"))

    raw_n = len(raw)
    filtered = filter_segments(raw, w, h, min_len * 0.3, min_feature * 0.45)
    chained, joins = chain_paths(filtered, max_dist=max_join, max_angle=max_angle)
    chained, j2 = chain_paths(chained, max_dist=max_join * 1.2, max_angle=max_angle + 6)
    joins += j2
    # Undo scribble: split sharp corners after chaining
    split: List[PathPts] = []
    for p in chained:
        split.extend(split_at_sharp_corners(p, max_turn_deg=55.0))
    # Drop high-scribble strokes (long + jittery)
    split = [p for p in split if not (mean_turn_deg(p) > 40 and path_length(p) > 35)]
    calm = []
    for p in split:
        mt = mean_turn_deg(p)
        if path_length(p) > 70 and mt > 32:
            calm.extend(split_at_sharp_corners(p, max_turn_deg=38.0))
        else:
            calm.append(p)
    chained = calm

    outline = silhouette_outline(gray, simplify, bgr) if not face_only_ink else None
    if outline:
        outline = chaikin(douglas_peucker(outline, max(simplify, 2.0)), iterations=2)
        if outline and outline[0] != outline[-1]:
            outline = outline + [outline[0]]

    final = finalize_paths(
        chained,
        w,
        h,
        source,
        simplify,
        max_paths,
        outline,
        min_len=min_len,
        min_feature=min_feature,
    )
    stats = compute_stats(raw_n, filtered, joins, final, max_join, max_angle)
    stats.notes.append("skeleton (+sparse edge-follow) + Hough bands + tangent chaining + corner split")
    return final, stats


def extract_hybrid_region_paths(
    gray: np.ndarray,
    bgr: np.ndarray,
    *,
    simplify: float,
    min_len: float,
    min_feature: float,
    max_paths: int,
    max_join: float,
    max_angle: float,
) -> Tuple[List[StrokePath], PipelineStats, np.ndarray]:
    """
    Smarter hybrid: silhouette/hair mass from Canny, face strokes from XDoG centerline.
    """
    h, w = gray.shape
    canny = suppress_background(canny_lineart(gray), gray, bgr)
    xdog_img = suppress_background(xdog(gray), gray, bgr)

    # Combined preview lineart
    combo = cv2.min(canny, xdog_img)  # darker wins

    outline = silhouette_outline(gray, simplify, bgr)

    # Hair/outer from canny (upper region)
    canny_ink = lineart_to_ink(canny, reconnect=1)
    canny_ink[int(h * 0.55) :, :] = 0  # hair / upper only
    thin_c = morphological_skeleton(canny_ink)
    raw_c = skeleton_polylines(thin_c)
    filt_c = filter_segments(raw_c, w, h, min_len * 0.4, min_feature)
    chain_c, j1 = chain_paths(filt_c, max_join, max_angle)

    # Face features from xdog via skeleton + hough
    x_ink = lineart_to_ink(xdog_img, reconnect=1)
    mask = np.zeros_like(x_ink)
    mask[int(h * 0.22) : int(h * 0.78), int(w * 0.14) : int(w * 0.86)] = 255
    x_ink = cv2.bitwise_and(x_ink, mask)
    x_ink[int(h * 0.84) :, :] = 0
    thin_x = morphological_skeleton(x_ink)
    raw_x = skeleton_polylines(thin_x)
    if cv2.countNonZero(x_ink) / float(x_ink.size) < 0.08:
        raw_x.extend(edge_follow_polylines(x_ink, min_points=5))
    raw_x.extend(hough_band_paths(x_ink, 0.26, 0.46, min_len=14, max_gap=12, mode="horizontal"))
    raw_x.extend(hough_band_paths(x_ink, 0.56, 0.72, min_len=12, max_gap=10, mode="horizontal"))
    filt_x = filter_segments(raw_x, w, h, min_len * 0.35, min_feature * 0.6)
    chain_x, j2 = chain_paths(filt_x, max_join, max_angle)
    chain_x, j3 = chain_paths(chain_x, max_join * 1.3, max_angle + 6)

    merged_segs = chain_c + chain_x
    # Cross-chain once more across sources
    merged_segs, j4 = chain_paths(merged_segs, max_join * 1.2, max_angle + 4)
    joins = j1 + j2 + j3 + j4
    raw_n = len(raw_c) + len(raw_x)
    filtered_n = len(filt_c) + len(filt_x)

    final = finalize_paths(
        merged_segs,
        w,
        h,
        "hybrid-region",
        simplify,
        max_paths,
        outline,
        min_len=min_len,
        min_feature=min_feature,
    )
    stats = compute_stats(raw_n, merged_segs, joins, final, max_join, max_angle)
    stats.after_filter = filtered_n
    stats.notes.append("region hybrid: Canny upper/hair + XDoG face centerlines + silhouette")
    return final, stats, combo


# --- AutoTrace --------------------------------------------------------------


def parse_svg_paths(svg_text: str) -> List[PathPts]:
    """Parse SVG path data; each M/m starts a new subpath."""
    paths: List[PathPts] = []
    for m in re.finditer(r'\bd="([^"]+)"', svg_text):
        d = m.group(1)
        tokens = re.findall(r"[MmLlCcQqHhVvZz]|[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", d)
        i = 0
        cmd = "L"
        cx = cy = 0.0
        start: Optional[Point] = None
        pts: PathPts = []

        def flush():
            nonlocal pts, start
            if len(pts) >= 2:
                paths.append(pts)
            pts = []
            start = None

        def num():
            nonlocal i
            v = float(tokens[i])
            i += 1
            return v

        while i < len(tokens):
            t = tokens[i]
            if re.match(r"[A-Za-z]", t):
                cmd = t
                i += 1
                if cmd in "Zz":
                    if start and pts and (pts[-1][0] != start[0] or pts[-1][1] != start[1]):
                        pts.append(start)
                    flush()
                continue
            if cmd in "Mm":
                x, y = num(), num()
                if cmd == "m" and start is not None:
                    x += cx
                    y += cy
                # new subpath
                if pts:
                    flush()
                cx, cy = x, y
                pts.append((cx, cy))
                start = (cx, cy)
                cmd = "l" if cmd == "m" else "L"
            elif cmd in "Ll":
                x, y = num(), num()
                if cmd == "l":
                    x += cx
                    y += cy
                cx, cy = x, y
                pts.append((cx, cy))
            elif cmd in "Hh":
                x = num()
                if cmd == "h":
                    x += cx
                cx = x
                pts.append((cx, cy))
            elif cmd in "Vv":
                y = num()
                if cmd == "v":
                    y += cy
                cy = y
                pts.append((cx, cy))
            elif cmd in "Cc":
                nums = [num() for _ in range(6)]
                if cmd == "c":
                    nums = [
                        nums[0] + cx,
                        nums[1] + cy,
                        nums[2] + cx,
                        nums[3] + cy,
                        nums[4] + cx,
                        nums[5] + cy,
                    ]
                x0, y0 = cx, cy
                x1, y1, x2, y2, x3, y3 = nums
                for s in range(1, 7):
                    tt = s / 6
                    mt = 1 - tt
                    bx = mt**3 * x0 + 3 * mt**2 * tt * x1 + 3 * mt * tt**2 * x2 + tt**3 * x3
                    by = mt**3 * y0 + 3 * mt**2 * tt * y1 + 3 * mt * tt**2 * y2 + tt**3 * y3
                    pts.append((bx, by))
                cx, cy = x3, y3
            elif cmd in "Qq":
                nums = [num() for _ in range(4)]
                if cmd == "q":
                    nums = [nums[0] + cx, nums[1] + cy, nums[2] + cx, nums[3] + cy]
                x0, y0 = cx, cy
                x1, y1, x2, y2 = nums
                for s in range(1, 5):
                    tt = s / 5
                    mt = 1 - tt
                    bx = mt**2 * x0 + 2 * mt * tt * x1 + tt**2 * x2
                    by = mt**2 * y0 + 2 * mt * tt * y1 + tt**2 * y2
                    pts.append((bx, by))
                cx, cy = x2, y2
            else:
                i += 1
        flush()
    return paths


def autotrace_centerline(
    line_dark_on_light: np.ndarray,
    gray: np.ndarray,
    *,
    simplify: float,
    min_len: float,
    min_feature: float,
    max_paths: int,
    max_join: float,
    max_angle: float,
    bgr: Optional[np.ndarray] = None,
) -> Tuple[List[StrokePath], PipelineStats, Optional[str]]:
    exe = shutil.which("autotrace")
    if not exe:
        stats = PipelineStats(notes=["autotrace not found on PATH"])
        return [], stats, None

    h, w = line_dark_on_light.shape
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        png = td_path / "in.png"
        svg = td_path / "out.svg"
        cv2.imwrite(str(png), line_dark_on_light)
        cmd = [
            exe,
            "-centerline",
            "-background-color",
            "FFFFFF",
            "-despeckle-level",
            "8",
            "-despeckle-tightness",
            "2.5",
            "-filter-iterations",
            "4",
            "-error-threshold",
            "1.2",
            "-output-format",
            "svg",
            "-output-file",
            str(svg),
            str(png),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=60)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as err:
            stats = PipelineStats(notes=[f"autotrace failed: {err}"])
            return [], stats, None
        svg_text = svg.read_text(encoding="utf-8", errors="ignore")

    raw = parse_svg_paths(svg_text)
    # AutoTrace may use bottom-left origin in some builds; detect & flip if needed
    if raw:
        ys = [p[1] for path in raw for p in path]
        if ys and max(ys) <= h * 1.2:
            # assume top-left already (ImageMagick/SVG style)
            pass
        # If coordinates look inverted relative to image, flip
        # Heuristic: mean y of paths should be mid-portrait; if clustered top of SVG with head at bottom in photo — skip
    raw_n = len(raw)
    filtered = filter_segments(raw, w, h, min_len * 0.5, min_feature * 0.6)
    chained, joins = chain_paths(filtered, max_join * 1.2, max_angle + 5)
    outline = silhouette_outline(gray, simplify, bgr)
    final = finalize_paths(
        chained,
        w,
        h,
        "autotrace",
        simplify,
        max_paths,
        outline,
        min_len=min_len,
        min_feature=min_feature,
    )
    stats = compute_stats(raw_n, filtered, joins, final, max_join, max_angle)
    stats.notes.append("autotrace -centerline (GPL-2.0+)")
    return final, stats, svg_text


# --- Output -----------------------------------------------------------------


def paths_to_norm(paths: List[StrokePath], w: int, h: int) -> List[dict]:
    return [
        {
            "id": i,
            "kind": p.kind,
            "source": p.source,
            "length": round(p.length, 2),
            "points": [[round(x / w, 5), round(y / h, 5)] for x, y in p.points],
        }
        for i, p in enumerate(paths)
    ]


def write_svg(paths: List[StrokePath], w: int, h: int, path: Path, title: str = "") -> None:
    colors = {
        "outline": "#b03a2e",
        "feature": "#1a5276",
        "hair": "#6c3483",
        "other": "#566573",
        "shadow": "#1e8449",
    }
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">',
        '<rect width="100%" height="100%" fill="#f5f1ea"/>',
    ]
    if title:
        parts.append(f'<text x="12" y="22" font-size="14" fill="#333">{title}</text>')
    for p in paths:
        d = "M " + " L ".join(f"{x:.2f} {y:.2f}" for x, y in p.points)
        parts.append(
            f'<path d="{d}" fill="none" stroke="{colors.get(p.kind, "#333")}" '
            f'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" data-kind="{p.kind}"/>'
        )
    parts.append("</svg>")
    path.write_text("\n".join(parts), encoding="utf-8")


def write_preview_png(paths: List[StrokePath], w: int, h: int, path: Path) -> None:
    img = np.full((h, w, 3), (241, 241, 234), dtype=np.uint8)  # approx #f5f1ea BGR later
    img[:] = (234, 241, 245)
    colors = {
        "outline": (46, 58, 176),
        "feature": (118, 82, 26),
        "hair": (131, 52, 108),
        "other": (115, 101, 86),
    }
    for p in paths:
        pts = np.array([[int(round(x)), int(round(y))] for x, y in p.points], dtype=np.int32)
        if len(pts) < 2:
            continue
        cv2.polylines(img, [pts], False, colors.get(p.kind, (40, 40, 40)), 1, cv2.LINE_AA)
    cv2.imwrite(str(path), img)


def acceptance_gate(paths: List[StrokePath], w: int, h: int) -> Tuple[bool, List[str]]:
    """Hard gate: preview must look like a reduced drawing before plotting."""
    reasons = []
    kinds = {k: [] for k in ("outline", "feature", "hair", "other")}
    for p in paths:
        kinds.setdefault(p.kind, []).append(p)

    if not kinds["outline"]:
        reasons.append("keine Außenkontur")

    feats = kinds["feature"]
    if len(feats) < 4:
        reasons.append(f"zu wenige Feature-Pfade ({len(feats)})")

    def band_paths(y0, y1):
        out = []
        for p in feats:
            cy = sum(y for _, y in p.points) / len(p.points) / h
            if y0 <= cy <= y1:
                out.append(p)
        return out

    eyes = band_paths(0.24, 0.48)
    mouth = band_paths(0.55, 0.74)
    nose = band_paths(0.42, 0.62)

    eye_long = sum(1 for p in eyes if p.length >= 18)
    if eye_long < 2:
        reasons.append(f"Augen-/Brauen: braucht ≥2 längere Pfade, hat {eye_long}")

    if sum(p.length for p in mouth) < 18 and not any(p.length >= 16 for p in mouth):
        reasons.append("Mund nicht als zusammenhängende Struktur")

    if sum(p.length for p in nose) < 10 and len(nose) == 0:
        reasons.append("Nasenregion leer")

    short = sum(1 for p in paths if p.length < 14 and p.kind != "outline")
    if short > max(6, len(paths) // 4):
        reasons.append(f"zu viele Mini-Segmente ({short})")

    median = float(np.median([p.length for p in paths])) if paths else 0
    if median < 22:
        reasons.append(f"Median-Länge zu klein ({median:.1f})")

    # Confetti / scribble heuristic
    if feats:
        long_f = sum(1 for p in feats if p.length >= 22)
        tiny_f = sum(1 for p in feats if p.length < 14)
        if tiny_f > long_f * 2 + 2:
            reasons.append(f"Feature-Konfetti ({tiny_f} kurz vs {long_f} lang)")
        scribble = sum(1 for p in feats if mean_turn_deg(p.points) > 28 and p.length > 25)
        if scribble >= 2:
            reasons.append(f"zu viele Kritzel-Pfade ({scribble})")
        avg_turn = float(np.mean([mean_turn_deg(p.points) for p in feats])) if feats else 99
        if avg_turn > 30:
            reasons.append(f"Feature-Mittelknick zu hoch ({avg_turn:.1f}°)")

    ok = len(reasons) == 0
    return ok, reasons


# --- Process all variants ---------------------------------------------------


def process(
    input_path: Path,
    outdir: Path,
    *,
    simplify: float = DEFAULT_SIMPLIFY,
    min_len: float = DEFAULT_MIN_LEN,
    min_feature: float = DEFAULT_MIN_FEATURE,
    max_paths: int = 70,
    max_join: float = DEFAULT_MAX_JOIN,
    max_angle: float = DEFAULT_MAX_ANGLE,
) -> dict:
    ensure_dir(outdir)
    bgr, gray = load_portrait(input_path)
    h, w = gray.shape

    linearts = {
        "canny": suppress_background(canny_lineart(gray), gray, bgr),
        "xdog": suppress_background(xdog(gray), gray, bgr),
        "hybrid": suppress_background(hybrid_lineart(gray), gray, bgr),
    }

    results = {}

    for name, la in linearts.items():
        cv2.imwrite(str(outdir / f"portrait-lineart-{name}.png"), la)
        paths, stats = extract_centerline_paths(
            la,
            gray,
            name,
            simplify=simplify,
            min_len=min_len,
            min_feature=min_feature,
            max_paths=max_paths,
            max_join=max_join,
            max_angle=max_angle,
            bgr=bgr,
        )
        svg = outdir / f"paths-{name}.svg"
        png = outdir / f"paths-{name}.png"
        write_svg(paths, w, h, svg, title=name)
        write_preview_png(paths, w, h, png)
        # also keep legacy names
        write_svg(paths, w, h, outdir / f"portrait-paths-{name}.svg", title=name)
        (outdir / f"portrait-paths-{name}.json").write_text(
            json.dumps(
                {
                    "width": w,
                    "height": h,
                    "variant": name,
                    "stats": stats.to_dict(),
                    "paths": paths_to_norm(paths, w, h),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        ok, reasons = acceptance_gate(paths, w, h)
        results[name] = {
            "lineart": str(outdir / f"portrait-lineart-{name}.png"),
            "svg": str(svg),
            "png": str(png),
            "stats": stats.to_dict(),
            "n_paths": len(paths),
            "previewReady": ok,
            "rejectReasons": reasons,
        }

    # Region hybrid
    hpaths, hstats, hla = extract_hybrid_region_paths(
        gray,
        bgr,
        simplify=simplify,
        min_len=min_len,
        min_feature=min_feature,
        max_paths=max_paths,
        max_join=max_join,
        max_angle=max_angle,
    )
    cv2.imwrite(str(outdir / "portrait-lineart-hybrid-region.png"), hla)
    write_svg(hpaths, w, h, outdir / "paths-hybrid.svg", title="hybrid-region")
    write_preview_png(hpaths, w, h, outdir / "paths-hybrid.png")
    # Overwrite hybrid entry with region-aware version (better)
    ok, reasons = acceptance_gate(hpaths, w, h)
    results["hybrid"] = {
        "lineart": str(outdir / "portrait-lineart-hybrid-region.png"),
        "svg": str(outdir / "paths-hybrid.svg"),
        "png": str(outdir / "paths-hybrid.png"),
        "stats": hstats.to_dict(),
        "n_paths": len(hpaths),
        "previewReady": ok,
        "rejectReasons": reasons,
        "note": "region-aware hybrid (Canny hair/upper + XDoG face)",
    }
    (outdir / "portrait-paths-hybrid.json").write_text(
        json.dumps(
            {
                "width": w,
                "height": h,
                "variant": "hybrid-region",
                "stats": hstats.to_dict(),
                "paths": paths_to_norm(hpaths, w, h),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # Custom centerline alias (best of xdog extraction labeled)
    shutil.copyfile(outdir / "paths-xdog.svg", outdir / "paths-custom-centerline.svg")
    shutil.copyfile(outdir / "paths-xdog.png", outdir / "paths-custom-centerline.png")

    # AutoTrace on Canny lineart (denser edges than XDoG for this photo)
    at_paths, at_stats, _ = autotrace_centerline(
        linearts["canny"],
        gray,
        simplify=simplify,
        min_len=min_len,
        min_feature=min_feature,
        max_paths=max_paths,
        max_join=max_join,
        max_angle=max_angle,
        bgr=bgr,
    )
    write_svg(at_paths, w, h, outdir / "paths-autotrace-centerline.svg", title="autotrace-centerline")
    write_preview_png(at_paths, w, h, outdir / "paths-autotrace-centerline.png")
    ok_at, reasons_at = acceptance_gate(at_paths, w, h)
    results["autotrace"] = {
        "svg": str(outdir / "paths-autotrace-centerline.svg"),
        "png": str(outdir / "paths-autotrace-centerline.png"),
        "stats": at_stats.to_dict(),
        "n_paths": len(at_paths),
        "previewReady": ok_at,
        "rejectReasons": reasons_at,
        "license": "GPL-2.0+",
    }
    (outdir / "portrait-paths-autotrace.json").write_text(
        json.dumps(
            {
                "width": w,
                "height": h,
                "variant": "autotrace",
                "stats": at_stats.to_dict(),
                "paths": paths_to_norm(at_paths, w, h),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # Choose best ready variant; else best by continuity heuristic
    def continuity_score(entry: dict) -> float:
        st = entry.get("stats") or {}
        score = 0.0
        score += st.get("medianLength", 0) * 1.2
        score += min(40, st.get("joins", 0))
        score -= st.get("pathsUnder10px", 0) * 4
        score -= st.get("pathsUnder20px", 0) * 0.8
        # Prefer fewer, calmer path sets over dense wire tangles
        n = entry.get("n_paths", 0)
        if 12 <= n <= 40:
            score += 25
        elif n > 45:
            score -= (n - 45) * 1.5
        if entry.get("previewReady"):
            score += 80
        # Prefer xdog/autotrace when ready (cleaner than dense canny)
        return score

    ranked = sorted(results.keys(), key=lambda k: continuity_score(results[k]), reverse=True)
    best = ranked[0]
    best_ready = results[best].get("previewReady", False)

    # Resolve paths list for chosen
    chosen_json_src = {
        "canny": outdir / "portrait-paths-canny.json",
        "xdog": outdir / "portrait-paths-xdog.json",
        "hybrid": outdir / "portrait-paths-hybrid.json",
        "autotrace": outdir / "portrait-paths-autotrace.json",
    }[best]
    chosen = json.loads(chosen_json_src.read_text(encoding="utf-8"))
    (outdir / "portrait-paths-chosen.json").write_text(json.dumps(chosen, indent=2), encoding="utf-8")

    best_svg = Path(results[best]["svg"])
    shutil.copyfile(best_svg, outdir / "portrait-path-preview.svg")
    best_png = results[best].get("png")
    if best_png and Path(best_png).exists():
        shutil.copyfile(best_png, outdir / "portrait-path-preview.png")

    # lineart source
    if best == "autotrace":
        shutil.copyfile(outdir / "portrait-lineart-xdog.png", outdir / "portrait-lineart-source.png")
    elif best == "hybrid":
        shutil.copyfile(outdir / "portrait-lineart-hybrid-region.png", outdir / "portrait-lineart-source.png")
    else:
        shutil.copyfile(outdir / f"portrait-lineart-{best}.png", outdir / "portrait-lineart-source.png")

    plot_allowed = bool(best_ready)
    (outdir / "PLOT_READY").write_text("yes\n" if plot_allowed else "no\n", encoding="utf-8")
    if not plot_allowed:
        (outdir / "PLOT_BLOCKED.txt").write_text(
            "Path preview failed acceptance gate. Do NOT run portrait-plot.\n"
            + f"best={best}\n"
            + "\n".join(results[best].get("rejectReasons") or []),
            encoding="utf-8",
        )

    summary = {
        "input": str(input_path),
        "size": [w, h],
        "chosenVariant": best,
        "plotAllowed": plot_allowed,
        "rootCausePrevious": (
            "Ribbon-Konturhälften + frühes Douglas-Peucker auf Mini-Segmente; "
            "Skeleton-Walk stoppte an Junctions ohne Richtungs-Chaining → Konfetti."
        ),
        "chaining": {
            "method": "greedy endpoint join scored by distance + outward-tangent angle",
            "maxJoinDistance": max_join,
            "maxAngleDeg": max_angle,
            "order": "raw centerline → filter → chain → chaikin → douglas-peucker",
        },
        "variants": results,
        "ranking": ranked,
        "chosenJson": str(outdir / "portrait-paths-chosen.json"),
        "pathPreviewSvg": str(outdir / "portrait-path-preview.svg"),
        "pathPreviewPng": str(outdir / "portrait-path-preview.png"),
        "params": {
            "simplify": simplify,
            "minLen": min_len,
            "minFeature": min_feature,
            "maxPaths": max_paths,
            "maxJoin": max_join,
            "maxAngle": max_angle,
        },
        "licenses": {
            "opencv": "Apache-2.0",
            "numpy": "BSD",
            "autotrace": "GPL-2.0+",
            "potrace": "GPL-2.0+ (installed, outline-only — not used as primary)",
            "xdog_algorithm": "Winnemöller et al. CAG 2012 (local reimplementation)",
        },
        "previousBaseline": {"finalPathsCanny": 78, "note": "fragmented ribbon/contour mix"},
    }
    (outdir / "preprocess-summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description="Portrait path pipeline (preview only, no plot)")
    ap.add_argument("--input", required=True, type=Path)
    ap.add_argument("--outdir", required=True, type=Path)
    ap.add_argument("--simplify", type=float, default=DEFAULT_SIMPLIFY)
    ap.add_argument("--min-len", type=float, default=DEFAULT_MIN_LEN)
    ap.add_argument("--min-feature", type=float, default=DEFAULT_MIN_FEATURE)
    ap.add_argument("--max-paths", type=int, default=70)
    ap.add_argument("--max-join", type=float, default=DEFAULT_MAX_JOIN)
    ap.add_argument("--max-angle", type=float, default=DEFAULT_MAX_ANGLE)
    args = ap.parse_args()
    print(
        json.dumps(
            process(
                args.input,
                args.outdir,
                simplify=args.simplify,
                min_len=args.min_len,
                min_feature=args.min_feature,
                max_paths=args.max_paths,
                max_join=args.max_join,
                max_angle=args.max_angle,
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
