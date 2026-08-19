# KrenzSketch

Standalone, installable drawing PWA by Matthias Krenzer.

**Live:** [https://krenzsketch.web.app](https://krenzsketch.web.app)

## Features

- Fixed 2048×2048 document canvas, independent of screen size
- Procedural brushes: Sketchy, Shaded, Fur, Web, Line, Chrome, Squares, Circles, Triangles
- Procedural eraser
- Drawing color and paper color pickers
- Adjustable stroke size with pen pressure support
- Zoom, pan, and fit (pinch, scroll wheel, Space + drag)
- Undo / Redo (full session history)
- PNG export (always full document)
- Local autosave and persistence (IndexedDB)
- Clear with confirmation
- Installable offline PWA with controlled update mechanism
- Compact collapsible toolbar on touch devices

## Tech

Vanilla HTML, CSS, and JavaScript. No framework, no external fonts, no trackers, no cookies.

Hosted on Firebase Hosting. Firebase project: `krenzsketch`.

## Harmony

The procedural brush algorithms are derived from **Harmony** by Mr.doob:

- [github.com/mrdoob/harmony](https://github.com/mrdoob/harmony)
- Harmony, Procedural Drawing Tool
- Copyright (C) 2010 Mr.doob
- GNU GPL v3 or later

The original Harmony license is preserved in [`third_party/harmony/LICENSE`](third_party/harmony/LICENSE).

KrenzSketch reimplements the neighbour-point geometry rules in [`src/js/brushes.js`](src/js/brushes.js). The Harmony UI, color wheel, and application shell were not used.

## License

**GNU GPL v3 or later** — see [`LICENSE`](LICENSE).

Copyright (C) 2026 Matthias Krenzer.

## Development

```bash
npm run build      # → dist/
npm run preview    # local preview server
```

Requires Node 18+.

## Deploy

```bash
firebase deploy --only hosting
```
