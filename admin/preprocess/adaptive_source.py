"""
Adaptive Zeichenkarte (structure map) for Photo → Sketch.

Generic: people, animals, objects, landscapes — not face-tuned.

Principles:
- Analyze input metrics first
- Work at a consistent internal short-edge scale
- Prefer relative (fraction of short edge) over fixed px
- Soft XDoG multi-scale; late binarization only for path probes
- No production brush / chaining changes here

Usage:
  from adaptive_source import build_adaptive_source
  soft, meta = build_adaptive_source(bgr)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional, Tuple

import cv2
import numpy as np


# ---------------------------------------------------------------------------
# Working resolution (consistent filter scale)
# ---------------------------------------------------------------------------

# Short edge targets: small sources upsampled moderately; large ones capped.
WORK_SHORT_MIN = 900
WORK_SHORT_MAX = 1400
WORK_SHORT_DEFAULT = 1200


@dataclass
class ImageMetrics:
    width: int
    height: int
    short_edge: int
    long_edge: int
    mean: float
    std: float
    p05: float
    p95: float
    dynamic_range: float
    bright_frac: float
    dark_frac: float
    local_contrast: float
    noise_est: float
    sharpness: float
    profile: str  # low_contrast | noisy | sharp_detail | small | dark | bright | balanced


@dataclass
class AdaptiveParams:
    work_short: int
    scale_from_native: float
    clahe_clip: float
    bilateral_d: int
    bilateral_sigma: float
    gauss_sigma_rel: float  # × short_edge
    sigma_coarse_rel: float
    sigma_mid_rel: float
    sigma_fine_rel: float
    xdog_p: float
    xdog_phi: float
    fine_weight: float
    mid_weight: float
    coarse_weight: float
    late_thr_pct: float  # percentile of soft map for late ink (lower = more ink)
    notes: str


def _rel_sigma(short: int, frac: float) -> float:
    """Gaussian sigma from fraction of short edge; clamp to sane range."""
    return float(np.clip(short * frac, 0.4, 8.0))


def _odd_k(x: float) -> int:
    k = int(round(x))
    if k < 3:
        return 3
    return k if k % 2 else k + 1


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def analyze_image(gray: np.ndarray) -> ImageMetrics:
    h, w = gray.shape
    short, long_ = min(h, w), max(h, w)
    g = gray.astype(np.float32)
    mean = float(g.mean())
    std = float(g.std())
    p05, p95 = float(np.percentile(g, 5)), float(np.percentile(g, 95))
    dynamic = max(1.0, p95 - p05)
    bright_frac = float((g > 240).mean())
    dark_frac = float((g < 20).mean())

    # Local contrast: std of (gray - box blur)
    blur = cv2.blur(g, (max(5, short // 40),) * 2)
    local_contrast = float(np.std(g - blur))

    # Noise estimate: high-pass residual MAD
    hp = g - cv2.GaussianBlur(g, (0, 0), max(0.8, short * 0.0015))
    noise_est = float(np.median(np.abs(hp - np.median(hp))) * 1.4826)

    # Sharpness: variance of Laplacian (normalized by dynamic range)
    lap = cv2.Laplacian(gray, cv2.CV_32F, ksize=3)
    sharpness = float(lap.var() / (dynamic ** 2 + 1e-6))

    # Profile selection (ordered, mutually exclusive-ish)
    if short < 500:
        profile = "small"
    elif std < 28 or dynamic < 55:
        profile = "low_contrast"
    elif noise_est > 8.5 and sharpness < 0.015:
        profile = "noisy"
    elif mean < 55:
        profile = "dark"
    elif mean > 200:
        profile = "bright"
    elif sharpness > 0.04 and local_contrast > 22:
        profile = "sharp_detail"
    else:
        profile = "balanced"

    return ImageMetrics(
        width=w,
        height=h,
        short_edge=short,
        long_edge=long_,
        mean=round(mean, 2),
        std=round(std, 2),
        p05=round(p05, 2),
        p95=round(p95, 2),
        dynamic_range=round(dynamic, 2),
        bright_frac=round(bright_frac, 4),
        dark_frac=round(dark_frac, 4),
        local_contrast=round(local_contrast, 2),
        noise_est=round(noise_est, 2),
        sharpness=round(sharpness, 5),
        profile=profile,
    )


def choose_work_short(metrics: ImageMetrics) -> int:
    """
    Consistent working short-edge.
    Upscale small inputs only so morphology/filters share a scale —
    not as a claim of new detail.
    """
    s = metrics.short_edge
    if s < WORK_SHORT_MIN:
        return WORK_SHORT_MIN
    if s > WORK_SHORT_MAX:
        return WORK_SHORT_MAX
    # Prefer ~DEFAULT when already mid-sized
    if abs(s - WORK_SHORT_DEFAULT) < 150:
        return s
    return int(np.clip(s, WORK_SHORT_MIN, WORK_SHORT_MAX))


def derive_params(metrics: ImageMetrics) -> AdaptiveParams:
    work = choose_work_short(metrics)
    scale = work / float(metrics.short_edge)

    # Defaults (balanced)
    clahe = 1.25
    bil_d = _odd_k(work * 0.0045)  # ~5 @1200
    bil_sig = 38.0
    gauss_rel = 0.0004  # ~0.48 @1200
    sc, sm, sf = 0.0020, 0.0010, 0.00065
    p, phi = 0.975, 10.0
    w_c, w_m, w_f = 0.28, 0.50, 0.22
    late_pct = 18.0
    notes = []

    prof = metrics.profile
    if prof == "low_contrast":
        clahe = 2.2
        phi = 11.5
        p = 0.97
        w_f = 0.28
        late_pct = 22.0
        notes.append("CLAHE↑ + phi↑ for weak edges")
    elif prof == "noisy":
        clahe = 1.05
        bil_d = _odd_k(work * 0.007)
        bil_sig = 55.0
        gauss_rel = 0.0007
        w_f = 0.10
        w_c = 0.35
        sc = 0.0024
        late_pct = 15.0
        notes.append("denoise↑ fine↓")
    elif prof == "sharp_detail":
        clahe = 1.1
        gauss_rel = 0.00055
        w_f = 0.12
        w_c = 0.38
        sc = 0.0022
        sm = 0.00115
        late_pct = 16.0
        notes.append("detail reduction via coarse↑ fine↓")
    elif prof == "small":
        clahe = 1.35
        # softer response; avoid early harshness after upscale
        phi = 8.5
        p = 0.98
        w_f = 0.15
        gauss_rel = 0.00055
        late_pct = 20.0
        notes.append("small source: soft phi, work upscale for filter scale only")
    elif prof == "dark":
        clahe = 1.8
        phi = 11.0
        late_pct = 24.0
        notes.append("dark: CLAHE + more ink percentile")
    elif prof == "bright":
        clahe = 1.5
        p = 0.97
        late_pct = 20.0
        notes.append("bright: mild CLAHE")

    # Continuous tweaks from metrics (not one-image tuned)
    if metrics.noise_est > 6:
        bil_sig = min(70.0, bil_sig + (metrics.noise_est - 6) * 2.5)
        w_f = max(0.08, w_f - 0.04)
    if metrics.std < 40:
        clahe = min(2.6, clahe + (40 - metrics.std) * 0.02)

    # Normalize weights
    s = w_c + w_m + w_f
    w_c, w_m, w_f = w_c / s, w_m / s, w_f / s

    return AdaptiveParams(
        work_short=work,
        scale_from_native=round(scale, 4),
        clahe_clip=round(clahe, 3),
        bilateral_d=int(bil_d),
        bilateral_sigma=round(bil_sig, 1),
        gauss_sigma_rel=gauss_rel,
        sigma_coarse_rel=sc,
        sigma_mid_rel=sm,
        sigma_fine_rel=sf,
        xdog_p=p,
        xdog_phi=phi,
        fine_weight=round(w_f, 3),
        mid_weight=round(w_m, 3),
        coarse_weight=round(w_c, 3),
        late_thr_pct=late_pct,
        notes="; ".join(notes) or "balanced defaults",
    )


# ---------------------------------------------------------------------------
# Soft multi-scale XDoG (no early Otsu / no Canny)
# ---------------------------------------------------------------------------

def to_work_size(bgr: np.ndarray, gray: np.ndarray, work_short: int) -> Tuple[np.ndarray, np.ndarray, float]:
    h, w = gray.shape
    short = min(h, w)
    if short == work_short:
        return bgr.copy(), gray.copy(), 1.0
    scale = work_short / float(short)
    nw, nh = int(round(w * scale)), int(round(h * scale))
    inter = cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC
    return (
        cv2.resize(bgr, (nw, nh), interpolation=inter),
        cv2.resize(gray, (nw, nh), interpolation=inter),
        scale,
    )


def prep_gray(gray: np.ndarray, params: AdaptiveParams) -> np.ndarray:
    short = min(gray.shape)
    g = gray
    if params.clahe_clip > 0:
        clahe = cv2.createCLAHE(clipLimit=params.clahe_clip, tileGridSize=(8, 8))
        g = clahe.apply(g)
    if params.bilateral_d >= 3:
        g = cv2.bilateralFilter(g, params.bilateral_d, params.bilateral_sigma, params.bilateral_sigma)
    gs = _rel_sigma(short, params.gauss_sigma_rel)
    g = cv2.GaussianBlur(g, (0, 0), gs)
    return g


def xdog_soft(
    gray: np.ndarray,
    *,
    sigma: float,
    k: float = 1.6,
    p: float = 0.975,
    phi: float = 10.0,
    eps: float = 0.01,
) -> np.ndarray:
    """Continuous dark-on-light uint8 map (no Otsu)."""
    g = gray.astype(np.float32) / 255.0
    g1 = cv2.GaussianBlur(g, (0, 0), max(0.35, sigma))
    g2 = cv2.GaussianBlur(g, (0, 0), max(0.5, sigma * k))
    dog = g1 - p * g2
    dog_n = dog / (np.abs(dog).max() + 1e-8)
    e = 1.0 + np.tanh(phi * (dog_n + eps))
    line = np.clip(1.0 - e, 0, 1)
    return ((1.0 - line) * 255.0).astype(np.uint8)


def soft_multiscale(gray: np.ndarray, params: AdaptiveParams) -> np.ndarray:
    short = min(gray.shape)
    g = prep_gray(gray, params)
    coarse = xdog_soft(
        g,
        sigma=_rel_sigma(short, params.sigma_coarse_rel),
        p=params.xdog_p,
        phi=max(6.0, params.xdog_phi - 2.0),
        eps=0.014,
    )
    mid = xdog_soft(
        g,
        sigma=_rel_sigma(short, params.sigma_mid_rel),
        p=params.xdog_p,
        phi=params.xdog_phi,
        eps=0.01,
    )
    fine = xdog_soft(
        g,
        sigma=_rel_sigma(short, params.sigma_fine_rel),
        p=min(0.985, params.xdog_p + 0.005),
        phi=min(14.0, params.xdog_phi + 1.5),
        eps=0.008,
    )

    def strength(s: np.ndarray) -> np.ndarray:
        return (255.0 - s.astype(np.float32)) / 255.0

    comb = (
        params.coarse_weight * strength(coarse)
        + params.mid_weight * strength(mid)
        + params.fine_weight * strength(fine)
    )
    comb = np.clip(comb, 0, 1)

    # Suppress weak texture (relative floor) — generic, not motif-specific
    nz = comb[comb > 1e-4]
    if nz.size > 200:
        # keep upper response mass; floor scales with profile via late_thr_pct
        keep_pct = float(np.clip(55 + (25 - params.late_thr_pct), 50, 78))
        floor = float(np.percentile(nz, keep_pct))
        comb = np.where(comb >= floor, comb, 0.0)

    # Soft reconnect in strength domain (anti-alias / bridge tiny gaps)
    comb = cv2.GaussianBlur(comb, (0, 0), _rel_sigma(short, 0.0009))
    # re-normalize peak
    peak = float(comb.max()) + 1e-8
    comb = np.clip(comb / peak, 0, 1)

    paper = ((1.0 - comb) * 255.0).astype(np.uint8)
    # Soft paper: lift only near-white; keep AA gray on strokes
    lift = int(np.percentile(paper, 94))
    paper = np.where(paper >= max(lift, 250), 255, paper)
    return paper


def subject_mask_generic(bgr: np.ndarray, gray: np.ndarray) -> np.ndarray:
    """
    Soft subject prior — GrabCut when it works, else center-weighted ellipse.
    Not face-specific.
    """
    h, w = gray.shape
    mask = np.zeros(gray.shape, np.uint8)
    rect = (int(w * 0.04), int(h * 0.04), int(w * 0.92), int(h * 0.92))
    bgd = np.zeros((1, 65), np.float64)
    fgd = np.zeros((1, 65), np.float64)
    try:
        cv2.grabCut(bgr, mask, rect, bgd, fgd, 2, cv2.GC_INIT_WITH_RECT)
        subject = np.where((mask == 2) | (mask == 0), 0, 255).astype(np.uint8)
        k = _odd_k(min(h, w) * 0.012)
        subject = cv2.morphologyEx(subject, cv2.MORPH_CLOSE, np.ones((k, k), np.uint8))
        if cv2.countNonZero(subject) > 0.05 * w * h:
            return subject
    except cv2.error:
        pass
    yy, xx = np.ogrid[:h, :w]
    return (
        ((xx - w * 0.5) / (w * 0.48)) ** 2 + ((yy - h * 0.5) / (h * 0.48)) ** 2 <= 1
    ).astype(np.uint8) * 255


def suppress_bg_soft(soft: np.ndarray, subject: np.ndarray) -> np.ndarray:
    k = _odd_k(min(soft.shape) * 0.01)
    sub = cv2.dilate(subject, np.ones((k, k), np.uint8), iterations=1)
    out = soft.copy()
    out[sub == 0] = 255
    m = max(2, min(soft.shape) // 60)
    out[:m, :] = 255
    out[-m:, :] = 255
    out[:, :m] = 255
    out[:, -m:] = 255
    return out


def soft_to_ink(soft: np.ndarray, params: AdaptiveParams) -> np.ndarray:
    """Late binarization for path probes only — percentile of dark response."""
    # ink where paper is darker than percentile of non-white pixels
    vals = soft[soft < 250]
    if vals.size < 100:
        thr = 200
    else:
        thr = float(np.percentile(vals, min(95.0, params.late_thr_pct + 55.0)))
        # late_thr_pct ~15–25 → thr around mid-dark soft values
        thr = float(np.percentile(soft, 100.0 - params.late_thr_pct))
    thr = float(np.clip(thr, 140, 235))
    return np.where(soft < thr, 255, 0).astype(np.uint8)


def build_adaptive_source(
    bgr: np.ndarray,
    *,
    suppress_background: bool = True,
) -> Tuple[np.ndarray, Dict[str, Any]]:
    """
    Returns soft dark-on-light Zeichenkarte at working resolution + metadata.
    """
    if bgr.ndim == 2:
        gray0 = bgr
        bgr0 = cv2.cvtColor(bgr, cv2.COLOR_GRAY2BGR)
    else:
        bgr0 = bgr
        gray0 = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

    metrics = analyze_image(gray0)
    params = derive_params(metrics)
    bgr_w, gray_w, scale = to_work_size(bgr0, gray0, params.work_short)
    params.scale_from_native = round(scale, 4)

    soft = soft_multiscale(gray_w, params)
    subject = subject_mask_generic(bgr_w, gray_w) if suppress_background else np.full(gray_w.shape, 255, np.uint8)
    if suppress_background:
        soft = suppress_bg_soft(soft, subject)

    meta = {
        "metrics": asdict(metrics),
        "params": asdict(params),
        "workSize": {"w": soft.shape[1], "h": soft.shape[0]},
        "philosophy": "adaptive soft multi-scale XDoG; late binary only for paths",
    }
    return soft, meta


def load_bgr(path: str) -> np.ndarray:
    bgr = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if bgr is None:
        raise FileNotFoundError(path)
    return bgr
