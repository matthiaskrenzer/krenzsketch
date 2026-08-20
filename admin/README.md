# KrenzSketch Admin — Zeichenkarte / Sketchy-Plot

Lokales Tooling. **Nicht** Teil der PWA (`dist/` = nur `src/`).

## Grundsatz

Die Vorverarbeitung erzeugt **keine** fertige Zeichnung.

```
Foto → Zeichenkarte (Struktur + optional Tonwert)
     → Bewegungsbahnen
     → Sketchy zeichnet die eigentliche Interpretation
```

Keine Bildgenerierung, kein Line-Art-Import auf den Canvas, keine fertigen Vektorformen als Ergebnis.

## Pipeline (aktuell)

```bash
# 1) Zeichenkarte + Stroke-Plan
PYTHONPATH=admin/preprocess/vendor python3 admin/preprocess/zeichnkarte.py \
  --photo /pfad/foto.png \
  --outdir tmp/portrait-preprocess
# bevorzugt: portrait-lineart-hybrid.png (reicher als at-in)

# 2) Build + Sketchy A/B/C
npm run build
npm run portrait-sketchy
```

- **A:** nur Struktur, leichte Punktdichte, 1 Pass  
- **B:** dichtere Punkte, Augen/Kontur/Mund 2 Pässe  
- **C:** B + Tonwert-Schraffur  

Brush: **nur Sketchy**.

## Outputs

| Datei | Inhalt |
|-------|--------|
| `zeichnkarte-source.png` | genutzte Strukturkarte |
| `portrait-strokeplan.json` | Bewegungsbahnen (normiert) |
| `portrait-strokeplan-a.png` | Preview Struktur |
| `portrait-strokeplan-preview.png` | Struktur + Schraffur-Zonen |
| `portrait-sketchy-a/b/c-shading.png` | echte KrenzSketch-Plots |

## Legacy

`lineart.py` / AutoTrace-/Canny-Pfadvergleiche bleiben zum Vergleich. Plotten über `portrait-plot` nur bei `PLOT_READY=yes`.

## Später entfernen

`admin/`, `scripts/admin-server.mjs`, `scripts/admin/`, `admin/preprocess/vendor/`, `tmp/portrait-*`
