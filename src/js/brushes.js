/*
 * KrenzSketch — procedural brushes
 * Copyright (C) 2026 Matthias Krenzer
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * This file is a modern KrenzSketch implementation. It is not the original
 * Harmony source. The neighbour-points drawing rules (distance thresholds,
 * random links, strokeStart / stroke / strokeEnd) are derived from and
 * informed by the Harmony algorithms:
 *
 *   Harmony, Procedural Drawing Tool
 *   Copyright (C) 2010 Mr.doob
 *   https://github.com/mrdoob/harmony
 *   GNU GPL v3 or later
 *
 * Harmony originals that informed this rewrite:
 *   js/brushes/sketchy.js
 *   js/brushes/shaded.js
 *   js/brushes/fur.js
 *   js/brushes/web.js
 *   js/brushes/simple.js
 *   js/brushes/chrome.js
 *
 * Harmony originals that informed the geometric brushes:
 *   js/brushes/squares.js
 *   js/brushes/circles.js
 *
 * SquaresBrush and CirclesBrush are modern reimplementations derived from
 * the Harmony squares and circles algorithms. TrianglesBrush is an original
 * KrenzSketch extension using the same procedural drawing principle.
 *
 * EraserBrush reuses the Sketchy neighbour-point geometry with
 * destination-out compositing; it is original KrenzSketch code.
 */

function rgba(color, alpha) {
	const a = Math.max(0, Math.min(1, alpha));
	return `rgba(${color[0]}, ${color[1]}, ${color[2]}, ${a})`;
}

function applyLine(ctx, size) {
	ctx.lineWidth = size;
	ctx.lineCap = size <= 1 ? "butt" : "round";
	ctx.lineJoin = "round";
}

class NeighbourBrush {
	constructor(ctx) {
		this.ctx = ctx;
		this.points = [];
		this.count = 0;
		this.prevX = 0;
		this.prevY = 0;
	}

	reset() {
		this.points = [];
		this.count = 0;
	}

	strokeStart(x, y) {
		this.prevX = x;
		this.prevY = y;
	}

	strokeEnd() {}
}

/**
 * Sketchy: faint stroke plus shortened links to nearby earlier points.
 * Original: js/brushes/sketchy.js
 */
class SketchyBrush extends NeighbourBrush {
	stroke(x, y, style) {
		const { ctx } = this;
		this.points.push([x, y]);
		applyLine(ctx, style.size);
		ctx.strokeStyle = rgba(style.color, 0.05 * style.pressure);

		ctx.beginPath();
		ctx.moveTo(this.prevX, this.prevY);
		ctx.lineTo(x, y);
		ctx.stroke();

		const current = this.points[this.count];
		for (let i = 0; i < this.points.length; i++) {
			const dx = this.points[i][0] - current[0];
			const dy = this.points[i][1] - current[1];
			const d = dx * dx + dy * dy;
			if (d < 4000 && Math.random() > d / 2000) {
				ctx.beginPath();
				ctx.moveTo(current[0] + dx * 0.3, current[1] + dy * 0.3);
				ctx.lineTo(this.points[i][0] - dx * 0.3, this.points[i][1] - dy * 0.3);
				ctx.stroke();
			}
		}

		this.prevX = x;
		this.prevY = y;
		this.count += 1;
	}
}

/**
 * Shaded: links to nearby points with opacity falling off by distance.
 * Original: js/brushes/shaded.js
 */
class ShadedBrush extends NeighbourBrush {
	stroke(x, y, style) {
		const { ctx } = this;
		this.points.push([x, y]);
		applyLine(ctx, style.size);

		const current = this.points[this.count];
		for (let i = 0; i < this.points.length; i++) {
			const dx = this.points[i][0] - current[0];
			const dy = this.points[i][1] - current[1];
			const d = dx * dx + dy * dy;
			if (d < 1000) {
				ctx.strokeStyle = rgba(style.color, (1 - d / 1000) * 0.1 * style.pressure);
				ctx.beginPath();
				ctx.moveTo(current[0], current[1]);
				ctx.lineTo(this.points[i][0], this.points[i][1]);
				ctx.stroke();
			}
		}

		this.prevX = x;
		this.prevY = y;
		this.count += 1;
	}
}

/**
 * Fur: main stroke plus strands through the current point toward neighbours.
 * Original: js/brushes/fur.js
 */
class FurBrush extends NeighbourBrush {
	stroke(x, y, style) {
		const { ctx } = this;
		this.points.push([x, y]);
		applyLine(ctx, style.size);
		ctx.strokeStyle = rgba(style.color, 0.1 * style.pressure);

		ctx.beginPath();
		ctx.moveTo(this.prevX, this.prevY);
		ctx.lineTo(x, y);
		ctx.stroke();

		const current = this.points[this.count];
		for (let i = 0; i < this.points.length; i++) {
			const dx = this.points[i][0] - current[0];
			const dy = this.points[i][1] - current[1];
			const d = dx * dx + dy * dy;
			if (d < 2000 && Math.random() > d / 2000) {
				ctx.beginPath();
				ctx.moveTo(x + dx * 0.5, y + dy * 0.5);
				ctx.lineTo(x - dx * 0.5, y - dy * 0.5);
				ctx.stroke();
			}
		}

		this.prevX = x;
		this.prevY = y;
		this.count += 1;
	}
}

/**
 * Web: main stroke plus occasional full links to nearby points.
 * Original: js/brushes/web.js
 */
class WebBrush extends NeighbourBrush {
	stroke(x, y, style) {
		const { ctx } = this;
		this.points.push([x, y]);
		applyLine(ctx, style.size);
		ctx.strokeStyle = rgba(style.color, 0.5 * style.pressure);

		ctx.beginPath();
		ctx.moveTo(this.prevX, this.prevY);
		ctx.lineTo(x, y);
		ctx.stroke();

		ctx.strokeStyle = rgba(style.color, 0.1 * style.pressure);
		const current = this.points[this.count];
		for (let i = 0; i < this.points.length; i++) {
			const dx = this.points[i][0] - current[0];
			const dy = this.points[i][1] - current[1];
			const d = dx * dx + dy * dy;
			if (d < 2500 && Math.random() > 0.9) {
				ctx.beginPath();
				ctx.moveTo(current[0], current[1]);
				ctx.lineTo(this.points[i][0], this.points[i][1]);
				ctx.stroke();
			}
		}

		this.prevX = x;
		this.prevY = y;
		this.count += 1;
	}
}

/**
 * Chrome: main stroke plus randomly tinted short links to nearby points.
 * Original: js/brushes/chrome.js
 * Note: Harmony used `darker` compositing in AppleWebKit. That mode is
 * omitted here; source-over is used on all browsers.
 */
class ChromeBrush extends NeighbourBrush {
	stroke(x, y, style) {
		const { ctx } = this;
		this.points.push([x, y]);
		applyLine(ctx, style.size);
		ctx.strokeStyle = rgba(style.color, 0.1 * style.pressure);

		ctx.beginPath();
		ctx.moveTo(this.prevX, this.prevY);
		ctx.lineTo(x, y);
		ctx.stroke();

		const current = this.points[this.count];
		for (let i = 0; i < this.points.length; i++) {
			const dx = this.points[i][0] - current[0];
			const dy = this.points[i][1] - current[1];
			const d = dx * dx + dy * dy;
			if (d < 1000) {
				ctx.strokeStyle = rgba(
					[
						Math.floor(Math.random() * style.color[0]),
						Math.floor(Math.random() * style.color[1]),
						Math.floor(Math.random() * style.color[2]),
					],
					0.1 * style.pressure,
				);
				ctx.beginPath();
				ctx.moveTo(current[0] + dx * 0.2, current[1] + dy * 0.2);
				ctx.lineTo(this.points[i][0] - dx * 0.2, this.points[i][1] - dy * 0.2);
				ctx.stroke();
			}
		}

		this.prevX = x;
		this.prevY = y;
		this.count += 1;
	}
}

/**
 * Line: single polyline rendered on a lightweight overlay canvas during the
 * stroke, then composited onto the main canvas at strokeEnd.
 * Original: js/brushes/simple.js
 */
class SimpleBrush {
	constructor(ctx, { displayCanvas, continuousToMaster = false, alphaScale = 0.5 } = {}) {
		this.ctx = ctx;
		this.displayCanvas = displayCanvas || ctx.canvas;
		this.continuousToMaster = continuousToMaster;
		this.alphaScale = alphaScale;
		this.points = [];
		this.overlay = document.createElement("canvas");
		this.overlayCtx = this.overlay.getContext("2d");
		this.lastStyle = null;
	}

	reset() {
		this.points = [];
	}

	strokeStart(x, y) {
		this.points = [{ x, y }];
		const ref = this.displayCanvas;
		const dpr = ref.width / parseInt(ref.style.width);
		this.overlay.width = ref.width;
		this.overlay.height = ref.height;
		this.overlayCtx.setTransform(dpr, 0, 0, dpr, 0, 0);

		this.overlay.style.cssText = ref.style.cssText;
		this.overlay.style.pointerEvents = "none";
		this.overlay.style.position = "absolute";
		ref.parentNode.appendChild(this.overlay);
	}

	stroke(x, y, style) {
		this.points.push({ x, y });
		this.lastStyle = style;

		if (this.points.length >= 2) {
			const prev = this.points[this.points.length - 2];
			const curr = this.points[this.points.length - 1];
			const { ctx } = this;
			applyLine(ctx, style.size);
			ctx.strokeStyle = rgba(style.color, this.alphaScale * style.pressure);
			ctx.beginPath();
			ctx.moveTo(prev.x, prev.y);
			ctx.lineTo(curr.x, curr.y);
			ctx.stroke();
		}
	}

	strokeEnd() {
		this.points = [];
		this.lastStyle = null;
		if (this.overlay.parentNode) this.overlay.parentNode.removeChild(this.overlay);
	}
}

/**
 * Airbrush: soft particle spray with radial falloff. Many small semi-transparent
 * dots accumulate gradually; pointer-hold keeps spraying via app.js tick loop.
 */
class AirbrushBrush {
	constructor(ctx) {
		this.ctx = ctx;
	}

	reset() {}

	strokeStart() {}

	stroke(x, y, style) {
		this.spray(x, y, style);
	}

	spray(x, y, style) {
		const { ctx } = this;
		const radius = Math.max(3, style.size * 3.2);
		const pressure = style.pressure ?? 1;
		const density = style.densityScale ?? 1;
		const count = Math.max(12, Math.round((12 + style.size * 3.5) * (0.75 + pressure * 0.35) * density));

		for (let i = 0; i < count; i++) {
			const u = Math.random();
			const r = Math.sqrt(u) * radius;
			const angle = Math.random() * Math.PI * 2;
			const px = x + Math.cos(angle) * r;
			const py = y + Math.sin(angle) * r;

			const centerWeight = 1 - u;
			const baseAlpha = 0.055 + style.size * 0.006;
			const alpha = baseAlpha * (0.3 + 0.7 * centerWeight * centerWeight) * (0.85 + pressure * 0.2);

			// Keep dots large enough to render on high-DPR mobile screens.
			const dotR = Math.max(0.55, style.size * 0.11 * (0.45 + Math.random() * 0.55));

			ctx.fillStyle = rgba(style.color, Math.min(alpha, 0.16));
			ctx.beginPath();
			ctx.arc(px, py, dotR, 0, Math.PI * 2);
			ctx.fill();
		}
	}

	strokeEnd() {}
}

/**
 * Squares: parallelogram shapes between consecutive points, perpendicular
 * to the movement direction. Derived from Harmony js/brushes/squares.js.
 */
class SquaresBrush {
	constructor(ctx) {
		this.ctx = ctx;
		this.prevX = 0;
		this.prevY = 0;
	}

	reset() {}

	strokeStart(x, y) {
		this.prevX = x;
		this.prevY = y;
	}

	stroke(x, y, style) {
		const { ctx } = this;
		const dx = x - this.prevX;
		const dy = y - this.prevY;
		const px = -dy;
		const py = dx;

		ctx.lineWidth = style.size * 0.5;
		ctx.fillStyle = rgba(style.color, 0.12 * style.pressure);
		ctx.strokeStyle = rgba(style.color, 0.4 * style.pressure);

		ctx.beginPath();
		ctx.moveTo(this.prevX - px, this.prevY - py);
		ctx.lineTo(this.prevX + px, this.prevY + py);
		ctx.lineTo(x + px, y + py);
		ctx.lineTo(x - px, y - py);
		ctx.closePath();
		ctx.fill();
		ctx.stroke();

		this.prevX = x;
		this.prevY = y;
	}

	strokeEnd() {}
}

/**
 * Circles: concentric arcs at grid-snapped centres, radius derived from
 * movement distance. Derived from Harmony js/brushes/circles.js.
 */
class CirclesBrush {
	constructor(ctx) {
		this.ctx = ctx;
		this.prevX = 0;
		this.prevY = 0;
	}

	reset() {}

	strokeStart(x, y) {
		this.prevX = x;
		this.prevY = y;
	}

	stroke(x, y, style) {
		const { ctx } = this;
		const dx = x - this.prevX;
		const dy = y - this.prevY;
		const d = Math.sqrt(dx * dx + dy * dy) * 2;

		const gridSize = Math.max(20, style.size * 18);
		const cx = Math.floor(x / gridSize) * gridSize + gridSize * 0.5;
		const cy = Math.floor(y / gridSize) * gridSize + gridSize * 0.5;

		ctx.lineWidth = style.size * 0.5;
		ctx.strokeStyle = rgba(style.color, 0.08 * style.pressure);

		const steps = Math.floor(Math.random() * 8) + 1;
		const stepDelta = d / steps;

		for (let i = 0; i < steps; i++) {
			const r = (steps - i) * stepDelta;
			if (r < 0.5) continue;
			ctx.beginPath();
			ctx.arc(cx, cy, r, 0, Math.PI * 2);
			ctx.stroke();
		}

		this.prevX = x;
		this.prevY = y;
	}

	strokeEnd() {}
}

/**
 * Triangles: dynamic triangles along the drawing path. Size and orientation
 * derive from movement direction and distance. Original KrenzSketch code.
 */
class TrianglesBrush {
	constructor(ctx) {
		this.ctx = ctx;
		this.prevX = 0;
		this.prevY = 0;
		this.angle = 0;
	}

	reset() {
		this.angle = 0;
	}

	strokeStart(x, y) {
		this.prevX = x;
		this.prevY = y;
		this.angle = 0;
	}

	stroke(x, y, style) {
		const { ctx } = this;
		const dx = x - this.prevX;
		const dy = y - this.prevY;
		const dist = Math.sqrt(dx * dx + dy * dy);
		if (dist < 1) return;

		const moveAngle = Math.atan2(dy, dx);
		this.angle += 0.3 + Math.random() * 0.4;
		const r = dist * 0.8 + style.size * 2;

		const a1 = moveAngle + this.angle;
		const a2 = a1 + 2.094;
		const a3 = a2 + 2.094;

		const mx = (this.prevX + x) * 0.5;
		const my = (this.prevY + y) * 0.5;

		ctx.lineWidth = style.size * 0.4;
		ctx.fillStyle = rgba(style.color, 0.08 * style.pressure);
		ctx.strokeStyle = rgba(style.color, 0.3 * style.pressure);

		ctx.beginPath();
		ctx.moveTo(mx + Math.cos(a1) * r, my + Math.sin(a1) * r);
		ctx.lineTo(mx + Math.cos(a2) * r, my + Math.sin(a2) * r);
		ctx.lineTo(mx + Math.cos(a3) * r, my + Math.sin(a3) * r);
		ctx.closePath();
		ctx.fill();
		ctx.stroke();

		this.prevX = x;
		this.prevY = y;
	}

	strokeEnd() {}
}

/**
 * Procedural eraser: Sketchy neighbour-point geometry with destination-out.
 * Low alpha so one pass thins marks; repeats strengthen the erasure.
 */
class EraserBrush extends NeighbourBrush {
	strokeStart(x, y) {
		this.ctx.globalCompositeOperation = "destination-out";
		super.strokeStart(x, y);
	}

	stroke(x, y, style) {
		const { ctx } = this;
		this.points.push([x, y]);
		applyLine(ctx, style.size * 2);
		ctx.strokeStyle = rgba([0, 0, 0], 0.12 * style.pressure);

		ctx.beginPath();
		ctx.moveTo(this.prevX, this.prevY);
		ctx.lineTo(x, y);
		ctx.stroke();

		const current = this.points[this.count];
		for (let i = 0; i < this.points.length; i++) {
			const dx = this.points[i][0] - current[0];
			const dy = this.points[i][1] - current[1];
			const d = dx * dx + dy * dy;
			if (d < 4000 && Math.random() > d / 2000) {
				ctx.beginPath();
				ctx.moveTo(current[0] + dx * 0.3, current[1] + dy * 0.3);
				ctx.lineTo(this.points[i][0] - dx * 0.3, this.points[i][1] - dy * 0.3);
				ctx.stroke();
			}
		}

		this.prevX = x;
		this.prevY = y;
		this.count += 1;
	}
}

export const BRUSHES = [
	{ id: "sketchy", label: "Sketchy", create: (ctx) => new SketchyBrush(ctx) },
	{ id: "shaded", label: "Shaded", create: (ctx) => new ShadedBrush(ctx) },
	{ id: "fur", label: "Fur", create: (ctx) => new FurBrush(ctx) },
	{ id: "web", label: "Web", create: (ctx) => new WebBrush(ctx) },
	{ id: "airbrush", label: "Airbrush", create: (ctx) => new AirbrushBrush(ctx) },
	{ id: "simple", label: "Line", create: (ctx, opts) => new SimpleBrush(ctx, opts) },
	{ id: "chrome", label: "Chrome", create: (ctx) => new ChromeBrush(ctx) },
	{ id: "squares", label: "Squares", create: (ctx) => new SquaresBrush(ctx) },
	{ id: "circles", label: "Circles", create: (ctx) => new CirclesBrush(ctx) },
	{ id: "triangles", label: "Triangles", create: (ctx) => new TrianglesBrush(ctx) },
];

export function createBrush(id, ctx, opts) {
	const found = BRUSHES.find((item) => item.id === id) ?? BRUSHES[0];
	const brush = found.create(ctx, opts);
	ctx.globalCompositeOperation = "source-over";
	return brush;
}

export function createEraser(ctx) {
	const brush = new EraserBrush(ctx);
	ctx.globalCompositeOperation = "destination-out";
	return brush;
}
