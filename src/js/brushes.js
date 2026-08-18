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
 * Linie: single polyline, redrawn from a snapshot while the stroke is active.
 * Original: js/brushes/simple.js
 */
class SimpleBrush {
	constructor(ctx) {
		this.ctx = ctx;
		this.points = [];
		this.buffer = document.createElement("canvas");
		this.bufferCtx = this.buffer.getContext("2d");
	}

	reset() {
		this.points = [];
	}

	strokeStart(x, y) {
		this.points = [{ x, y }];
		this.buffer.width = this.ctx.canvas.width;
		this.buffer.height = this.ctx.canvas.height;
		this.bufferCtx.drawImage(this.ctx.canvas, 0, 0);
	}

	stroke(x, y, style) {
		this.points.push({ x, y });
		this.ctx.save();
		this.ctx.setTransform(1, 0, 0, 1, 0, 0);
		this.ctx.clearRect(0, 0, this.ctx.canvas.width, this.ctx.canvas.height);
		this.ctx.drawImage(this.buffer, 0, 0);
		this.ctx.restore();
		applyLine(this.ctx, style.size);
		this.ctx.strokeStyle = rgba(style.color, 0.5 * style.pressure);
		this.ctx.beginPath();
		this.ctx.moveTo(this.points[0].x, this.points[0].y);
		for (let i = 1; i < this.points.length; i++) {
			this.ctx.lineTo(this.points[i].x, this.points[i].y);
		}
		this.ctx.stroke();
	}

	strokeEnd() {
		this.points = [];
	}
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
	{ id: "simple", label: "Linie", create: (ctx) => new SimpleBrush(ctx) },
	{ id: "chrome", label: "Chrome", create: (ctx) => new ChromeBrush(ctx) },
];

export function createBrush(id, ctx) {
	const found = BRUSHES.find((item) => item.id === id) ?? BRUSHES[0];
	const brush = found.create(ctx);
	ctx.globalCompositeOperation = "source-over";
	return brush;
}

export function createEraser(ctx) {
	const brush = new EraserBrush(ctx);
	ctx.globalCompositeOperation = "destination-out";
	return brush;
}
