/**
 * Drive real KrenzSketch strokes via PointerEvents (never draw on ctx directly).
 * Coordinates are normalized 0..1 in portrait image space, mapped into the
 * canvas with contain-fit letterboxing.
 */

export function createStrokeDriver(doc) {
	let pid = 3000;
	const canvas = doc.getElementById("canvas");
	const view = doc.defaultView || window;
	const PE = view.PointerEvent || PointerEvent;

	let fit = { ox: 0, oy: 0, scale: 1, iw: 1, ih: 1 };

	function setPortraitSize(iw, ih) {
		fit.iw = iw;
		fit.ih = ih;
		layout();
	}

	function layout() {
		const rect = canvas.getBoundingClientRect();
		const scale = Math.min(rect.width / fit.iw, rect.height / fit.ih);
		const dw = fit.iw * scale;
		const dh = fit.ih * scale;
		fit.scale = scale;
		fit.ox = rect.left + (rect.width - dw) / 2;
		fit.oy = rect.top + (rect.height - dh) / 2;
		fit.rect = rect;
	}

	function imageToClient(nx, ny) {
		layout();
		return {
			x: fit.ox + nx * fit.iw * fit.scale,
			y: fit.oy + ny * fit.ih * fit.scale,
		};
	}

	function fire(type, x, y, id, pressure, buttons) {
		const target = type === "pointerdown" ? canvas : canvas;
		target.dispatchEvent(
			new PE(type, {
				bubbles: true,
				cancelable: true,
				clientX: x,
				clientY: y,
				pointerId: id,
				pointerType: "pen",
				pressure,
				button: 0,
				buttons,
			}),
		);
	}

	function setBrush(mode, size, colorHex) {
		const modeEl = doc.getElementById("mode");
		const sizeEl = doc.getElementById("size");
		const inkEl = doc.getElementById("ink");
		if (modeEl && modeEl.value !== mode) {
			modeEl.value = mode;
			modeEl.dispatchEvent(new Event("change", { bubbles: true }));
		}
		if (sizeEl) {
			sizeEl.value = String(size);
			sizeEl.dispatchEvent(new Event("input", { bubbles: true }));
		}
		if (inkEl && colorHex) {
			inkEl.value = colorHex;
			inkEl.dispatchEvent(new Event("input", { bubbles: true }));
		}
	}

	async function clearDrawing() {
		const clearBtn = doc.getElementById("clear");
		const ok = doc.getElementById("confirm-ok");
		if (!clearBtn || !ok) return;
		clearBtn.click();
		await new Promise((r) => setTimeout(r, 40));
		ok.click();
		await new Promise((r) => setTimeout(r, 80));
	}

	function densify(points, step = 0.006) {
		if (!points?.length) return [];
		const out = [points[0].slice()];
		for (let i = 1; i < points.length; i++) {
			const a = points[i - 1];
			const b = points[i];
			const dx = b[0] - a[0];
			const dy = b[1] - a[1];
			const dist = Math.hypot(dx, dy);
			const n = Math.max(1, Math.ceil(dist / step));
			for (let k = 1; k <= n; k++) {
				const t = k / n;
				out.push([a[0] + dx * t, a[1] + dy * t]);
			}
		}
		return out;
	}

	function strokeNorm(
		points,
		{ pressure = 0.45, jitter = 0.0015, incompleteness = 0, densifyStep = 0.005 } = {},
	) {
		if (!points?.length) return;
		layout();
		pid += 1;
		const id = pid;
		let pts = densify(points, densifyStep);
		if (incompleteness > 0 && pts.length > 8) {
			const drop = Math.floor(pts.length * incompleteness);
			pts = pts.slice(0, Math.max(4, pts.length - drop));
		}
		const start = imageToClient(
			pts[0][0] + (Math.random() - 0.5) * jitter,
			pts[0][1] + (Math.random() - 0.5) * jitter,
		);
		fire("pointerdown", start.x, start.y, id, pressure, 1);
		for (let i = 1; i < pts.length; i++) {
			const t = i / (pts.length - 1);
			const p = imageToClient(
				pts[i][0] + (Math.random() - 0.5) * jitter,
				pts[i][1] + (Math.random() - 0.5) * jitter,
			);
			const pr = pressure * (0.65 + 0.35 * Math.sin(t * Math.PI));
			fire("pointermove", p.x, p.y, id, pr, 1);
		}
		const end = imageToClient(pts[pts.length - 1][0], pts[pts.length - 1][1]);
		fire("pointerup", end.x, end.y, id, 0, 0);
	}

	function exportPngBlob() {
		const paperInput = doc.getElementById("paper");
		const paper = paperInput?.value ? paperInput.value : "#ece8e1";
		const out = doc.createElement("canvas");
		const w = canvas.width;
		const h = canvas.height;
		out.width = w;
		out.height = h;
		const octx = out.getContext("2d");
		octx.fillStyle = paper;
		octx.fillRect(0, 0, w, h);
		octx.drawImage(canvas, 0, 0);
		return new Promise((resolve) => out.toBlob((b) => resolve(b), "image/png"));
	}

	return { setPortraitSize, setBrush, clearDrawing, strokeNorm, exportPngBlob, imageToClient, layout };
}
