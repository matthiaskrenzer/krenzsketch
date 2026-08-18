# KrenzSketch

KrenzSketch ist eine eigenständige, installierbare Zeichen-PWA von Matthias Krenzer.

Es ist **nicht** Harmony und **nicht** LiveSketch HD.

## Lizenz

KrenzSketch steht unter der **GNU GPL v3 oder später**.

Der unveränderte offizielle GNU-GPL-v3-Text liegt in [`LICENSE`](LICENSE).

Die GPL gilt, weil die Zeichenmodi Harmony-Algorithmen reimplementieren bzw. davon abgeleitet sind. KrenzSketch ist damit ein GPL-kompatibles, eigenständiges Programm, kein unverändertes Harmony.

Copyright an den neu geschriebenen KrenzSketch-Dateien: Matthias Krenzer, 2026.

## Harmony

Harmony ist das **ursprüngliche** Open-Source-Zeichenprojekt von Mr.doob:

- Repository: [https://github.com/mrdoob/harmony](https://github.com/mrdoob/harmony)
- Harmony, Procedural Drawing Tool
- Copyright (C) 2010 Mr.doob
- Lizenz: GNU GPL v3 oder später

Die originale Harmony-Lizenznotiz liegt unverändert in [`third_party/harmony/LICENSE`](third_party/harmony/LICENSE).

Harmony speichert die Punkte eines Strichs und verbindet neue Punkte mit früheren Nachbarpunkten. Sketchy, Shaded, Fur, Web und Chrome sind Varianten dieses Nachbarpunkt-Prinzips.

KrenzSketch übernimmt diese geometrischen Regeln in einer modernen Neuimplementierung (`src/js/brushes.js`). Die Harmony-Oberfläche von 2010, Menü, Farbkreis und Anwendungsrahmen wurden nicht übernommen.

Harmony-Quellen, die die Pinselregeln informiert haben: `js/brushes/sketchy.js`, `shaded.js`, `fur.js`, `web.js`, `simple.js`, `chrome.js`.

Nicht übernommen: `js/main.js`, `js/menu.js`, `js/about.js`, `js/colorselector.js`, `js/palette.js`, `js/colorutils.js`, Wacom-Plugin, Local-Storage der ganzen Leinwand.

Neu in KrenzSketch: Pointer Events (Maus, Touch, Pencil-Druck soweit der Browser das liefert), Undo/Redo, native Farbfelder, PWA mit Service Worker, eigene dunkle Oberfläche.

## LiveSketch HD

LiveSketch HD diente ausschließlich als historische Inspiration für eine ruhige, installierbare Zeichenanwendung auf dem iPad. Es wurden **kein Code, keine Assets, keine Icons, keine Texte, keine Oberflächen und keine Marken** aus LiveSketch HD übernommen.

## Technik

Vanilla HTML, CSS und JavaScript. Kein Frontend-Framework, keine externen Schriftarten, keine Tracker, keine Cookies.

Zeichnungen bleiben auf dem Gerät. Optional werden nur Werkzeugeinstellungen im Local Storage gespeichert. Export als PNG erfolgt lokal.

## Entwicklung

```bash
npm run build
npm run preview
```

Build-Ausgabe: `dist/`. Node 18 oder neuer.

Noch kein GitHub-Remote und kein Firebase-Projekt. `firebase.json` ist vorbereitet (`public`: `dist`). Es liegen keine Secrets im Repository.

## Funktionen

- freie Zeichenfläche
- Maus, Touch, Pointer/Pencil
- Modi Sketchy, Shaded, Fur, Web, Linie, Chrome
- Stift- und Hintergrundfarbe
- Strichstärke
- Löschen, Undo, Redo
- PNG-Export
- installierbare PWA, offline nutzbar
- Impressum, Datenschutz, Credits
