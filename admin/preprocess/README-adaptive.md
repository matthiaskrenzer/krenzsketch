# Adaptive Photo → Zeichenkarte

Generische Source-Erzeugung für späteres `Photo → Sketch`.

## Prinzip

1. Bildmetriken messen (Kontrast, Rauschen, Schärfe, Dynamik, Größe)
2. Arbeits-Short-Edge ~900–1400 (Upscale nur für Filter-Skala, nicht „neue Details“)
3. Soft Multi-Scale XDoG (keine frühe Otsu-/Canny-Binarisierung)
4. Relative Sigmas (`× short_edge`)
5. Späte Binarisierung nur für Path-Probes

## Module

| Datei | Rolle |
|-------|--------|
| `adaptive_source.py` | Analyse + adaptive Soft-Source |
| `source_quality.py` | Testset-Lauf + Legacy-Vergleich |
| `zeichnkarte.py` | nutzt adaptive Source, wenn keine `--karte` |

## Testset

```bash
PYTHONPATH=admin/preprocess/vendor python3 admin/preprocess/source_quality.py
```

Outputs: `tmp/photo-sketch-source/`, Manifest: `tmp/photo-sketch-testset/manifest.json`

Noch **kein** Sketchy-Plot in diesem Schritt.
