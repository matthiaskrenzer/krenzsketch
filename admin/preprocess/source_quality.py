"""
Adaptive source quality harness + multi-image testset.

Does NOT change path-chaining / Sketchy / production code.
Compares legacy early-binary hybrid vs adaptive soft source across images.

  PYTHONPATH=admin/preprocess/vendor python3 admin/preprocess/source_quality.py
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import cv2
import numpy as np

from adaptive_source import (  # type: ignore
    analyze_image,
    build_adaptive_source,
    derive_params,
    soft_to_ink,
    subject_mask_generic,
    suppress_bg_soft,
)
from zeichnkarte import (  # type: ignore
    edge_follow,
    morphological_skeleton,
    path_length,
    skeleton_walk,
)

Point = Tuple[float, float]
PathPts = List[Point]

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "tmp" / "photo-sketch-source"
TESTSET_DIR = ROOT / "tmp" / "photo-sketch-testset"
MANIFEST = TESTSET_DIR / "manifest.json"

# Local real photos only (no generative images). Roles are descriptive, not face-locked.
TESTSET = [
    {
        "id": "ref-small-portrait",
        "role": "small_messenger_like_portrait",
        "path": "/Users/matthiaskrenzer/.cursor/projects/Volumes-Extreme-SSD-Projekte-krenztek/assets/Bildschirmfoto_2026-08-20_um_09.43.02-25c065d2-f660-4f1e-b81e-20067d0898e2.png",
    },
    {
        "id": "hires-iphone",
        "role": "highres_phone_scene_chess",
        "path": "/Users/matthiaskrenzer/Downloads/IMG_3028.jpeg",
    },
    {
        "id": "dark-portrait",
        "role": "dark_lower_key_portrait",
        "path": "/Users/matthiaskrenzer/Downloads/michaelhamburger_portrait.jpg",
    },
    {
        "id": "mid-portrait",
        "role": "medium_portrait",
        "path": "/Users/matthiaskrenzer/Downloads/IMGP0683-scaled-e1701099224350-862x1024.jpg",
    },
    {
        "id": "tiny-cutout",
        "role": "very_small_image",
        "path": "/Users/matthiaskrenzer/Downloads/fotodenise-removebg-preview.png",
    },
    {
        "id": "scene-tango",
        "role": "non_portrait_scene",
        "path": "/Users/matthiaskrenzer/Downloads/ad-tango-02-1024x768.jpg",
    },
    {
        "id": "doc-scan",
        "role": "flat_document_low_texture",
        "path": "/Users/matthiaskrenzer/Pictures/Krenzer, Matthias-Abi_Zeugnis-1987_1.jpeg",
    },
    {
        "id": "cover-detail",
        "role": "graphic_detail_rich",
        "path": "/Users/matthiaskrenzer/Downloads/Dynastiedergoetterband2_cover.png",
    },
]


def legacy_binary_hybrid(gray: np.ndarray) -> np.ndarray:
    """Current early-Otsu + Canny hybrid (baseline to beat)."""
    g = gray.astype(np.float32) / 255.0
    # Match load_portrait mild CLAHE+blur historically used before hybrid
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    gray2 = clahe.apply(gray)
    gray2 = cv2.GaussianBlur(gray2, (0, 0), 0.6)
    g = gray2.astype(np.float32) / 255.0
    g1 = cv2.GaussianBlur(g, (0, 0), 0.9)
    g2 = cv2.GaussianBlur(g, (0, 0), 0.9 * 1.6)
    dog = g1 - 0.97 * g2
    dog_n = dog / (np.abs(dog).max() + 1e-8)
    e = 1.0 + np.tanh(18.0 * (dog_n + 0.02))
    out = ((1.0 - np.clip(e, 0, 1)) * 255.0).astype(np.uint8)
    _, bw = cv2.threshold(out, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if np.mean(bw) < 127:
        bw = 255 - bw
    canny = 255 - cv2.Canny(gray2, 40, 120, L2gradient=True)
    combo = cv2.max(255 - bw, 255 - canny)
    combo = cv2.morphologyEx(combo, cv2.MORPH_CLOSE, np.ones((2, 2), np.uint8), iterations=1)
    combo = cv2.morphologyEx(combo, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8), iterations=1)
    return 255 - combo


def extract_raw(ink: np.ndarray) -> List[PathPts]:
    """Unchanged path extractors from zeichnkarte — no chaining."""
    h, w = ink.shape
    linked = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8), iterations=2)
    linked = cv2.dilate(linked, np.ones((3, 3), np.uint8), iterations=1)
    raw: List[PathPts] = []
    raw.extend(edge_follow(linked, min_pts=4))
    thin = morphological_skeleton(linked)
    raw.extend(skeleton_walk(thin))
    return raw


def filter_min(raw: List[PathPts], min_len_rel: float, short: int) -> List[PathPts]:
    min_len = max(3.0, short * min_len_rel)
    out = []
    for pts in raw:
        if path_length(pts) >= min_len:
            out.append(pts)
    return out


def stats(segs: List[PathPts], short: int) -> Dict[str, Any]:
    if not segs:
        return {"count": 0, "medianLen": 0, "medianLenRel": 0, "pct_lt5": 0, "pct_lt10": 0, "pct_lt20": 0, "pct_lt_0p01short": 0, "maxLen": 0}
    lens = np.array([path_length(s) for s in segs], dtype=np.float64)
    thr_rel = short * 0.01
    return {
        "count": int(len(lens)),
        "medianLen": round(float(np.median(lens)), 2),
        "medianLenRel": round(float(np.median(lens) / short), 4),
        "pct_lt5": round(100 * float((lens < 5).mean()), 1),
        "pct_lt10": round(100 * float((lens < 10).mean()), 1),
        "pct_lt20": round(100 * float((lens < 20).mean()), 1),
        "pct_lt_0p01short": round(100 * float((lens < thr_rel).mean()), 1),
        "maxLen": round(float(lens.max()), 1),
        "maxLenRel": round(float(lens.max() / short), 4),
    }


def write_zooms(img: np.ndarray, stem: Path) -> None:
    h, w = img.shape[:2]
    vis = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR) if img.ndim == 2 else img
    # Generic quadrants (not face-named in filenames for non-portraits — still useful)
    crops = {
        "tl": (0.05, 0.45, 0.05, 0.55),
        "tr": (0.05, 0.45, 0.45, 0.95),
        "mid": (0.30, 0.70, 0.25, 0.75),
        "br": (0.50, 0.95, 0.45, 0.95),
    }
    for name, (y0, y1, x0, x1) in crops.items():
        crop = vis[int(h * y0) : int(h * y1), int(w * x0) : int(w * x1)]
        zoom = cv2.resize(crop, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_NEAREST)
        cv2.imwrite(str(stem.parent / f"{stem.name}-zoom-{name}.png"), zoom)


def setup_testset() -> List[Dict[str, Any]]:
    TESTSET_DIR.mkdir(parents=True, exist_ok=True)
    entries = []
    missing = []
    for item in TESTSET:
        src = Path(item["path"])
        if not src.exists():
            missing.append(item)
            continue
        dest = TESTSET_DIR / f"{item['id']}{src.suffix.lower()}"
        if not dest.exists():
            shutil.copy2(src, dest)
        bgr = cv2.imread(str(dest))
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        m = analyze_image(gray)
        entry = {**item, "local": str(dest), "native": {"w": m.width, "h": m.height}, "metrics": m.__dict__}
        entries.append(entry)
    MANIFEST.write_text(
        json.dumps(
            {
                "entries": entries,
                "missing": missing,
                "gaps": [
                    "kontrastarmes Outdoor-Portrait",
                    "stark verrauschtes Low-Light Smartphone",
                    "Tierfoto",
                    "Gebäude/Architektur klar",
                    "Landschaft mit Horizont",
                    "Brille Nahaufnahme",
                ],
                "note": "Keine Bildgenerierung; nur vorhandene lokale Dateien. Gaps dokumentiert.",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return entries


def process_one(entry: Dict[str, Any], out_root: Path) -> Dict[str, Any]:
    oid = entry["id"]
    odir = out_root / oid
    odir.mkdir(parents=True, exist_ok=True)
    bgr = cv2.imread(entry["local"])
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    metrics = analyze_image(gray)
    params = derive_params(metrics)

    # --- legacy baseline at native (historical failure mode) ---
    t0 = time.time()
    legacy = legacy_binary_hybrid(gray)
    sub_n = subject_mask_generic(bgr, gray)
    legacy = suppress_bg_soft(legacy, sub_n)
    ink_l = np.where(legacy < 128, 255, 0).astype(np.uint8)
    ink_l[sub_n == 0] = 0
    raw_l = extract_raw(ink_l)
    fil_l = filter_min(raw_l, 0.004, min(legacy.shape))
    st_l = stats(fil_l, min(legacy.shape))
    cv2.imwrite(str(odir / "source-legacy-binary.png"), legacy)
    t_legacy = round(time.time() - t0, 3)

    # --- adaptive soft ---
    from adaptive_source import AdaptiveParams

    t1 = time.time()
    soft, meta = build_adaptive_source(bgr, suppress_background=True)
    apar = AdaptiveParams(**meta["params"])
    ink_a = soft_to_ink(soft, apar)
    raw_a = extract_raw(ink_a)
    fil_a = filter_min(raw_a, 0.004, min(soft.shape))
    st_a = stats(fil_a, min(soft.shape))
    cv2.imwrite(str(odir / "source-adaptive-soft.png"), soft)
    cv2.imwrite(str(odir / "source-adaptive-late-ink.png"), 255 - ink_a)
    write_zooms(soft, odir / "source-adaptive-soft")
    write_zooms(legacy, odir / "source-legacy-binary")
    t_ad = round(time.time() - t1, 3)

    # path overlay previews
    def overlay(base, segs, path):
        img = cv2.cvtColor(base, cv2.COLOR_GRAY2BGR)
        img = (img.astype(np.float32) * 0.8 + 50).clip(0, 255).astype(np.uint8)
        for pts in segs[:600]:
            arr = np.array([[int(x), int(y)] for x, y in pts], np.int32)
            if len(arr) > 1:
                cv2.polylines(img, [arr], False, (40, 80, 200), 1, cv2.LINE_AA)
        cv2.imwrite(str(path), img)

    overlay(legacy, fil_l, odir / "pathprobe-legacy.png")
    overlay(soft, fil_a, odir / "pathprobe-adaptive.png")

    result = {
        "id": oid,
        "role": entry["role"],
        "metrics": meta["metrics"],
        "params": meta["params"],
        "workSize": meta["workSize"],
        "legacy": {"unique": int(len(np.unique(legacy))), "stats": st_l, "raw": len(raw_l), "seconds": t_legacy},
        "adaptive": {
            "unique": int(len(np.unique(soft))),
            "stats": st_a,
            "raw": len(raw_a),
            "seconds": t_ad,
            "inkPct": round(float((soft < 220).mean() * 100), 2),
        },
        "delta": {
            "filtCount_legacy_minus_adaptive": st_l["count"] - st_a["count"],
            "medianLenRel_gain": round(st_a["medianLenRel"] - st_l["medianLenRel"], 4),
            "pct_lt_0p01short_legacy": st_l["pct_lt_0p01short"],
            "pct_lt_0p01short_adaptive": st_a["pct_lt_0p01short"],
        },
    }
    (odir / "report.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    entries = setup_testset()
    results = []
    for e in entries:
        print(f"… {e['id']} ({e['native']['w']}x{e['native']['h']}, {e['metrics']['profile']})")
        results.append(process_one(e, OUT))

    # Reference aliases requested earlier (from ref-small-portrait)
    ref = OUT / "ref-small-portrait"
    if ref.exists():
        shutil.copy2(ref / "source-legacy-binary.png", OUT / "zeichnkarte-source-current.png")
        shutil.copy2(ref / "source-adaptive-soft.png", OUT / "zeichnkarte-source-soft.png")
        shutil.copy2(ref / "source-adaptive-soft.png", OUT / "zeichnkarte-source-multiscale.png")
        shutil.copy2(ref / "source-adaptive-soft.png", OUT / "zeichnkarte-source-hires-xdog.png")

    summary = {
        "goal": "Reliable Photo→Zeichenkarte across diverse inputs; not one-portrait tuning",
        "testset": str(MANIFEST),
        "results": results,
        "recommendation": {
            "source": "adaptive soft multi-scale XDoG (admin/preprocess/adaptive_source.py)",
            "why": (
                "Avoids early Otsu/Canny confetti; adapts CLAHE/denoise/scale weights from metrics; "
                "relative sigmas; consistent work short-edge; generic subject mask"
            ),
            "pathLogic": (
                "Keep chaining unchanged for now; switch path probe to late threshold on soft map "
                "(soft_to_ink). Absolute min_len px should become short_edge-relative next."
            ),
            "nextStep": (
                "Wire zeichnkarte.process to build_adaptive_source; re-run path stats; "
                "then one Sketchy plot only after visual source OK on ≥3 test images"
            ),
        },
        "missingTests": json.loads(MANIFEST.read_text())["gaps"],
    }
    (OUT / "ADAPTIVE_SOURCE_REPORT.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print("\n=== COMPARISON (filt count | medianLenRel | % < 0.01·short) ===")
    for r in results:
        L, A = r["legacy"]["stats"], r["adaptive"]["stats"]
        print(
            f"{r['id']:22s} profile={r['metrics']['profile']:12s} "
            f"legacy {L['count']:5d}/{L['medianLenRel']:.4f}/{L['pct_lt_0p01short']:5.1f}%  "
            f"adapt {A['count']:5d}/{A['medianLenRel']:.4f}/{A['pct_lt_0p01short']:5.1f}%  "
            f"work={r['workSize']['w']}x{r['workSize']['h']}"
        )


if __name__ == "__main__":
    main()
