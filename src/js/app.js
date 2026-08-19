/*
 * KrenzSketch — application shell
 * Copyright (C) 2026 Matthias Krenzer
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Original KrenzSketch code (PWA shell, pointer input, undo/redo, export,
 * local persistence). Procedural drawing modes live in brushes.js and are
 * derived from Harmony.
 */

import { BRUSHES, createBrush, createEraser } from "./brushes.js";
import { clearWorkspace, loadWorkspace, saveWorkspace } from "./persist.js";

const SETTINGS_KEY = "krenzsketch-settings";
const MAX_HISTORY = 500;
const SAVE_DELAY_MS = 400;
const COMPACT_QUERIES = ["(hover: none)", "(pointer: coarse)", "(max-width: 1024px)", "(max-height: 700px)"];

const canvas = document.getElementById("canvas");
const stage = document.getElementById("stage");

const dpr = Math.max(1, window.devicePixelRatio || 1);

const master = document.createElement("canvas");
const masterCtx = master.getContext("2d", { alpha: true, willReadFrequently: false });

const displayCtx = canvas.getContext("2d", { alpha: true, willReadFrequently: false });

const ui = {
	mode: document.getElementById("mode"),
	ink: document.getElementById("ink"),
	paper: document.getElementById("paper"),
	size: document.getElementById("size"),
	sizeValue: document.getElementById("size-value"),
	undo: document.getElementById("undo"),
	redo: document.getElementById("redo"),
	clear: document.getElementById("clear"),
	exportBtn: document.getElementById("export"),
	shareBtn: document.getElementById("share"),
	menuBtn: document.getElementById("menu-btn"),
	menu: document.getElementById("menu-dialog"),
	confirm: document.getElementById("confirm-dialog"),
	confirmOk: document.getElementById("confirm-ok"),
	eraser: document.getElementById("eraser"),
};

const state = {
	mode: "sketchy",
	color: [28, 28, 28],
	background: [236, 232, 225],
	size: 1.5,
	drawing: false,
	pointerId: null,
	pointerType: "mouse",
	erasing: false,
};

let brush = null;
const undoStack = [];
const redoStack = [];
let saveTimer = 0;
let saveQueued = false;
let saveGeneration = 0;
let persistEnabled = false;
let lastHistoryBlob = null;

let historyChain = Promise.resolve();
function enqueueHistory(fn) {
	historyChain = historyChain.then(fn, fn);
}

function clamp(n, min, max) {
	return Math.min(max, Math.max(min, n));
}

function isCompactUi() {
	return COMPACT_QUERIES.some((query) => window.matchMedia(query).matches);
}

function setToolsOpen(open) {
	document.documentElement.classList.toggle("tools-open", open);
	ui.menuBtn.setAttribute("aria-expanded", open ? "true" : "false");
	ui.menuBtn.classList.toggle("is-active", open && isCompactUi());
}

function collapseToolsIfCompact() {
	if (isCompactUi()) setToolsOpen(false);
}

function syncCompactClass() {
	const compact = isCompactUi();
	document.documentElement.classList.toggle("compact-ui", compact);
	if (!compact) setToolsOpen(false);
}

function hexToRgb(hex) {
	const value = hex.replace("#", "");
	return [
		parseInt(value.slice(0, 2), 16),
		parseInt(value.slice(2, 4), 16),
		parseInt(value.slice(4, 6), 16),
	];
}

function rgbToHex([r, g, b]) {
	return `#${[r, g, b].map((c) => c.toString(16).padStart(2, "0")).join("")}`;
}

function cssRgb([r, g, b]) {
	return `rgb(${r}, ${g}, ${b})`;
}

function loadSettings() {
	try {
		const raw = localStorage.getItem(SETTINGS_KEY);
		if (!raw) return;
		const data = JSON.parse(raw);
		if (BRUSHES.some((item) => item.id === data.mode)) state.mode = data.mode;
		if (Array.isArray(data.color) && data.color.length === 3) state.color = data.color.map((n) => clamp(n | 0, 0, 255));
		if (Array.isArray(data.background) && data.background.length === 3) {
			state.background = data.background.map((n) => clamp(n | 0, 0, 255));
		}
		if (typeof data.size === "number") state.size = clamp(data.size, 1, 12);
	} catch {
		/* ignore unreadable settings */
	}
}

function saveSettings() {
	try {
		localStorage.setItem(
			SETTINGS_KEY,
			JSON.stringify({
				mode: state.mode,
				color: state.color,
				background: state.background,
				size: state.size,
			}),
		);
	} catch {
		/* private mode / quota */
	}
}

function applyPaper() {
	canvas.style.backgroundColor = cssRgb(state.background);
	document.documentElement.style.setProperty("--paper", cssRgb(state.background));
}

function syncUi() {
	ui.mode.value = state.mode;
	ui.ink.value = rgbToHex(state.color);
	ui.paper.value = rgbToHex(state.background);
	ui.size.value = String(state.size);
	ui.sizeValue.textContent = String(state.size);
	applyPaper();
	updateHistoryButtons();
	syncToolUi();
}

function syncToolUi() {
	ui.eraser.setAttribute("aria-pressed", state.erasing ? "true" : "false");
	ui.eraser.classList.toggle("is-active", state.erasing);
	canvas.classList.toggle("is-erasing", state.erasing);
}

const BRUSH_SIZE_FACTOR = 2.6;
const BRUSH_STAMP_INTERVAL_MS = 80;

function currentStyle(pressure) {
	const isBrushMode = state.mode === "brush" && !state.erasing;
	const size = state.size * (isBrushMode ? BRUSH_SIZE_FACTOR : 1);
	// Size-dependent transparency to avoid "marker" look on the first pass.
	// Repeated passes still accumulate visually.
	// Reduced alpha to keep the first pass visibly "ink/airbrush-light",
	// and let density build up mainly through repeated overdraw.
	const opacity = isBrushMode ? clamp(0.045 + state.size * 0.003, 0.045, 0.07) : 1;
	return {
		color: state.color,
		size,
		pressure,
		opacity,
	};
}

function pointerPressure(event) {
	if (event.pointerType === "pen" && typeof event.pressure === "number") {
		return clamp(event.pressure, 0.05, 1);
	}
	return 1;
}

function canvasPoint(event) {
	const rect = canvas.getBoundingClientRect();
	return {
		x: (event.clientX - rect.left) * (canvas.width / rect.width) / dpr,
		y: (event.clientY - rect.top) * (canvas.height / rect.height) / dpr,
	};
}

function fitMasterContext() {
	masterCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

function ensureMaster(logW, logH) {
	const needW = logW * dpr;
	const needH = logH * dpr;
	if (master.width >= needW && master.height >= needH) return;

	const newW = Math.max(master.width, needW);
	const newH = Math.max(master.height, needH);

	let saved = null;
	if (master.width > 0 && master.height > 0) {
		saved = masterCtx.getImageData(0, 0, master.width, master.height);
	}

	master.width = newW;
	master.height = newH;

	if (saved) {
		masterCtx.putImageData(saved, 0, 0);
	}

	fitMasterContext();
	masterCtx.globalCompositeOperation = state.erasing ? "destination-out" : "source-over";
}

function sizeDisplay() {
	const rect = stage.getBoundingClientRect();
	const w = Math.round(rect.width);
	const h = Math.round(rect.height);
	if (w < 1 || h < 1) return;

	const bmpW = w * dpr;
	const bmpH = h * dpr;

	if (canvas.width !== bmpW || canvas.height !== bmpH) {
		canvas.width = bmpW;
		canvas.height = bmpH;
		canvas.style.width = w + "px";
		canvas.style.height = h + "px";
	}

	ensureMaster(w, h);
	presentMaster();
}

function presentMaster() {
	displayCtx.setTransform(1, 0, 0, 1, 0, 0);
	displayCtx.clearRect(0, 0, canvas.width, canvas.height);
	const sw = Math.min(canvas.width, master.width);
	const sh = Math.min(canvas.height, master.height);
	if (sw > 0 && sh > 0) {
		displayCtx.drawImage(master, 0, 0, sw, sh, 0, 0, sw, sh);
	}
}

function snapshotCanvas() {
	return new Promise((resolve) => {
		master.toBlob((blob) => resolve(blob), "image/png");
	});
}

async function restoreSnapshot(snapshot) {
	masterCtx.save();
	masterCtx.globalCompositeOperation = "source-over";
	masterCtx.setTransform(1, 0, 0, 1, 0, 0);
	masterCtx.clearRect(0, 0, master.width, master.height);
	if (snapshot) {
		const source = snapshot instanceof Blob
			? await createImageBitmap(snapshot)
			: snapshot;
		masterCtx.drawImage(source, 0, 0);
		if (source !== snapshot && typeof source.close === "function") source.close();
	}
	masterCtx.restore();
	fitMasterContext();
	masterCtx.globalCompositeOperation = state.erasing ? "destination-out" : "source-over";
	presentMaster();
}

function resetBrush() {
	const opts = { displayCanvas: canvas };
	if (state.mode === "brush" && !state.erasing) {
		// Brush stays lighter than a marker; final alpha is also size-adjusted in currentStyle().
		opts.alphaScale = 0.18;
	}
	brush = state.erasing ? createEraser(masterCtx) : createBrush(state.mode, masterCtx, opts);
}

async function pushHistory() {
	redoStack.length = 0;
	const blob = await snapshotCanvas();
	undoStack.push(blob);
	lastHistoryBlob = blob;
	while (undoStack.length > MAX_HISTORY) undoStack.shift();
	updateHistoryButtons();
}

async function undo() {
	if (undoStack.length < 2) return;
	redoStack.push(undoStack.pop());
	await restoreSnapshot(undoStack[undoStack.length - 1]);
	lastHistoryBlob = null;
	resetBrush();
	updateHistoryButtons();
	scheduleSave();
}

async function redo() {
	if (!redoStack.length) return;
	const snapshot = redoStack.pop();
	undoStack.push(snapshot);
	await restoreSnapshot(snapshot);
	lastHistoryBlob = null;
	resetBrush();
	updateHistoryButtons();
	scheduleSave();
}

function updateHistoryButtons() {
	ui.undo.disabled = undoStack.length < 2;
	ui.redo.disabled = redoStack.length === 0;
}

async function resetHistory() {
	undoStack.length = 0;
	redoStack.length = 0;
	lastHistoryBlob = null;
	await pushHistory();
}

async function clearCanvas() {
	masterCtx.save();
	masterCtx.globalCompositeOperation = "source-over";
	masterCtx.setTransform(1, 0, 0, 1, 0, 0);
	masterCtx.clearRect(0, 0, master.width, master.height);
	masterCtx.restore();
	fitMasterContext();
	resetBrush();
	presentMaster();
	await resetHistory();
}

function getContentBounds() {
	const w = master.width;
	const h = master.height;
	const data = masterCtx.getImageData(0, 0, w, h).data;
	let top = h, left = w, bottom = -1, right = -1;
	for (let y = 0; y < h; y++) {
		for (let x = 0; x < w; x++) {
			if (data[(y * w + x) * 4 + 3] > 0) {
				if (y < top) top = y;
				if (y > bottom) bottom = y;
				if (x < left) left = x;
				if (x > right) right = x;
			}
		}
	}
	if (bottom < 0) return null;
	return {
		x: Math.floor(left / dpr),
		y: Math.floor(top / dpr),
		w: Math.ceil((right + 1) / dpr) - Math.floor(left / dpr),
		h: Math.ceil((bottom + 1) / dpr) - Math.floor(top / dpr),
	};
}

async function createPngBlob() {
	const bounds = getContentBounds();
	if (!bounds) {
		console.log("Nothing to export");
		return null;
	}
	const out = document.createElement("canvas");
	out.width = bounds.w;
	out.height = bounds.h;
	const outCtx = out.getContext("2d");
	outCtx.fillStyle = cssRgb(state.background);
	outCtx.fillRect(0, 0, bounds.w, bounds.h);
	outCtx.drawImage(
		master,
		bounds.x * dpr, bounds.y * dpr, bounds.w * dpr, bounds.h * dpr,
		0, 0, bounds.w, bounds.h,
	);

	return new Promise((resolve) => {
		out.toBlob((blob) => resolve(blob), "image/png");
	});
}

async function exportPng() {
	const blob = await createPngBlob();
	if (!blob) return;

	const url = URL.createObjectURL(blob);
	const link = document.createElement("a");
	link.href = url;
	link.download = "krenzsketch.png";
	document.body.appendChild(link);
	link.click();
	link.remove();
	URL.revokeObjectURL(url);
}

async function sharePng() {
	if (!("share" in navigator)) return;

	const blob = await createPngBlob();
	if (!blob) return;

	// File-Sharing requires a File object.
	if (typeof File === "undefined") return;
	const file = new File([blob], "krenzsketch.png", { type: "image/png" });

	const payload = {
		title: "KrenzSketch",
		files: [file],
	};

	try {
		await navigator.share(payload);
	} catch (err) {
		// User canceled the share sheet.
		if (err && err.name === "AbortError") return;
		console.warn("Could not share drawing:", err);
	}
}

function updateShareUi() {
	// Feature detection without UA sniffing.
	if (!ui.shareBtn) return;
	ui.shareBtn.hidden = true;

	if (!("share" in navigator) || typeof navigator.canShare !== "function") return;
	if (typeof File === "undefined") return;

	// Create a small dummy file for canShare().
	const testBlob = new Blob([""], { type: "image/png" });
	const testFile = new File([testBlob], "krenzsketch.png", { type: "image/png" });

	let ok = false;
	try {
		ok = navigator.canShare({ files: [testFile] });
	} catch {
		ok = false;
	}

	ui.shareBtn.hidden = !ok;
}

function scheduleSave(immediate = false) {
	saveQueued = true;
	window.clearTimeout(saveTimer);
	if (immediate) {
		saveTimer = 0;
		void flushSave();
		return;
	}
	saveTimer = window.setTimeout(() => {
		saveTimer = 0;
		void flushSave();
	}, SAVE_DELAY_MS);
}

async function flushSave() {
	if (!persistEnabled || !saveQueued) return;
	saveQueued = false;
	window.clearTimeout(saveTimer);
	saveTimer = 0;
	const generation = ++saveGeneration;
	try {
		const blob = lastHistoryBlob ?? await snapshotCanvas();
		lastHistoryBlob = null;
		if (generation !== saveGeneration) return;
		await saveWorkspace({
			blob,
			background: state.background.slice(),
			width: master.width,
			height: master.height,
			savedAt: Date.now(),
		});
	} catch (err) {
		console.warn("Could not save drawing:", err);
	}
}

async function discardSavedDrawing() {
	saveQueued = false;
	window.clearTimeout(saveTimer);
	saveTimer = 0;
	saveGeneration += 1;
	try {
		await clearWorkspace();
	} catch (err) {
		console.warn("Could not clear saved drawing:", err);
	}
}

async function restoreWorkspace() {
	try {
		const record = await loadWorkspace();
		if (!record?.blob) return;
		if (Array.isArray(record.background) && record.background.length === 3) {
			state.background = record.background.map((n) => clamp(n | 0, 0, 255));
			applyPaper();
			ui.paper.value = rgbToHex(state.background);
			saveSettings();
		}
		const bitmap = await createImageBitmap(record.blob);
		ensureMaster(
			Math.ceil(bitmap.width / dpr),
			Math.ceil(bitmap.height / dpr),
		);
		masterCtx.save();
		masterCtx.globalCompositeOperation = "source-over";
		masterCtx.setTransform(1, 0, 0, 1, 0, 0);
		masterCtx.clearRect(0, 0, master.width, master.height);
		masterCtx.drawImage(bitmap, 0, 0);
		masterCtx.restore();
		fitMasterContext();
		masterCtx.globalCompositeOperation = state.erasing ? "destination-out" : "source-over";
		if (typeof bitmap.close === "function") bitmap.close();
		presentMaster();
	} catch (err) {
		console.warn("Could not restore drawing:", err);
	}
}

function startDraw(event) {
	event.preventDefault();
	try {
		canvas.setPointerCapture(event.pointerId);
	} catch {
		/* ignore */
	}
	state.drawing = true;
	state.pointerId = event.pointerId;
	state.pointerType = event.pointerType || "mouse";
	const { x, y } = canvasPoint(event);
	brush.strokeStart(x, y);

	// Brush continuous stamping state
	brushAnim.active = state.mode === "brush";
	brushAnim.pointerX = x;
	brushAnim.pointerY = y;
	brushAnim.pointerPressure = pointerPressure(event);
	brushAnim.drawX = x;
	brushAnim.drawY = y;
	brushAnim.lastStampAt = 0;
	if (brushAnim.active) scheduleBrushTick();
}

const brushAnim = {
	active: false,
	pointerX: 0,
	pointerY: 0,
	pointerPressure: 1,
	drawX: 0,
	drawY: 0,
	lastStampAt: 0,
	timer: 0,
};

function scheduleBrushTick() {
	if (brushAnim.timer) return;
	brushAnim.timer = window.setTimeout(() => {
		brushAnim.timer = 0;
		brushTick();
	}, BRUSH_STAMP_INTERVAL_MS);
}

function brushTick(now = performance.now()) {
	if (!state.drawing || !brushAnim.active || state.mode !== "brush") return;

	const x2 = brushAnim.pointerX;
	const y2 = brushAnim.pointerY;
	const x1 = brushAnim.drawX;
	const y1 = brushAnim.drawY;
	const dx = x2 - x1;
	const dy = y2 - y1;
	const dist = Math.sqrt(dx * dx + dy * dy);

	const style = currentStyle(brushAnim.pointerPressure);
	// Step spacing ensures a continuous broad brush even during fast movement.
	// Keep brush strokes continuous while limiting stamp overlap (less "circle chain").
	const stepDist = Math.max(0.8, style.size * 0.4);

	let wrote = false;
	if (dist >= stepDist) {
		// Limit interpolated stamps per tick to avoid dense overlap.
		const steps = Math.min(16, Math.ceil(dist / stepDist));
		for (let i = 1; i <= steps; i++) {
			const t = i / steps;
			const xi = x1 + dx * t;
			const yi = y1 + dy * t;
			brush.stroke(xi, yi, style);
			wrote = true;
		}
	} else {
		brush.stroke(x2, y2, style);
		wrote = true;
	}

	if (wrote) {
		presentMaster();
		brushAnim.drawX = x2;
		brushAnim.drawY = y2;
		brushAnim.lastStampAt = now;
	}

	scheduleBrushTick();
}

function onPointerDown(event) {
	if (document.activeElement && document.activeElement !== document.body) {
		document.activeElement.blur();
	}

	if (isCompactUi() && document.documentElement.classList.contains("tools-open")) {
		setToolsOpen(false);
	}

	if (state.drawing) return;
	if (event.button != null && event.button !== 0) return;

	startDraw(event);
}

function onPointerMove(event) {
	if (!state.drawing || event.pointerId !== state.pointerId) return;
	event.preventDefault();
	const events = typeof event.getCoalescedEvents === "function" ? event.getCoalescedEvents() : [event];
	const list = events.length ? events : [event];

	if (state.mode === "brush") {
		// For brush we stamp continuously in RAF. Pointermove only updates target.
		const ev = list[list.length - 1];
		const { x, y } = canvasPoint(ev);
		brushAnim.pointerX = x;
		brushAnim.pointerY = y;
		brushAnim.pointerPressure = pointerPressure(ev);
		return;
	}

	for (const ev of list) {
		const { x, y } = canvasPoint(ev);
		brush.stroke(x, y, currentStyle(pointerPressure(ev)));
	}
	presentMaster();
}

function onPointerUp(event) {
	if (!state.drawing || event.pointerId !== state.pointerId) return;
	event.preventDefault();

	brushAnim.active = false;
	brush.strokeEnd();
	state.drawing = false;
	state.pointerId = null;
	presentMaster();
	enqueueHistory(async () => {
		await pushHistory();
		scheduleSave(true);
	});
}

function bindUi() {
	for (const item of BRUSHES) {
		const option = document.createElement("option");
		option.value = item.id;
		option.textContent = item.label;
		ui.mode.appendChild(option);
	}

	ui.mode.addEventListener("change", () => {
		state.mode = ui.mode.value;
		state.erasing = false;
		resetBrush();
		syncToolUi();
		saveSettings();
		collapseToolsIfCompact();
		ui.mode.blur();
	});

	ui.eraser.addEventListener("click", () => {
		state.erasing = !state.erasing;
		brushAnim.active = state.mode === "brush";
		resetBrush();
		syncToolUi();
		collapseToolsIfCompact();
	});

	ui.ink.addEventListener("input", () => {
		state.color = hexToRgb(ui.ink.value);
		saveSettings();
	});
	ui.ink.addEventListener("change", collapseToolsIfCompact);

	ui.paper.addEventListener("input", () => {
		state.background = hexToRgb(ui.paper.value);
		applyPaper();
		saveSettings();
		scheduleSave();
	});
	ui.paper.addEventListener("change", collapseToolsIfCompact);

	ui.size.addEventListener("input", () => {
		state.size = Number(ui.size.value);
		ui.sizeValue.textContent = String(state.size);
		saveSettings();
	});
	ui.size.addEventListener("change", collapseToolsIfCompact);

	ui.undo.addEventListener("click", () => {
		enqueueHistory(() => undo());
		collapseToolsIfCompact();
	});
	ui.redo.addEventListener("click", () => {
		enqueueHistory(() => redo());
		collapseToolsIfCompact();
	});
	ui.exportBtn.addEventListener("click", () => {
		void exportPng();
		collapseToolsIfCompact();
	});

	ui.shareBtn?.addEventListener("click", () => {
		void sharePng().finally(() => {
			collapseToolsIfCompact();
		});
	});

	ui.clear.addEventListener("click", () => {
		ui.confirm.showModal();
	});
	ui.confirmOk.addEventListener("click", () => {
		ui.confirm.close();
		void clearCanvas().then(() => discardSavedDrawing());
		collapseToolsIfCompact();
	});

	ui.menuBtn.addEventListener("click", () => {
		if (isCompactUi()) {
			setToolsOpen(!document.documentElement.classList.contains("tools-open"));
			return;
		}
		ui.menu.showModal();
	});
	ui.menu.addEventListener("click", (event) => {
		if (event.target === ui.menu) ui.menu.close();
	});
	ui.confirm.addEventListener("click", (event) => {
		if (event.target === ui.confirm) ui.confirm.close();
	});

	for (const link of document.querySelectorAll(".btn-about, .tool-legal a, #menu-dialog a")) {
		link.addEventListener("click", () => {
			saveQueued = true;
			void flushSave();
		});
	}
}

function bindCanvas() {
	canvas.addEventListener("pointerdown", onPointerDown);
	canvas.addEventListener("pointermove", onPointerMove);
	canvas.addEventListener("pointerup", onPointerUp);
	canvas.addEventListener("pointercancel", onPointerUp);
	canvas.addEventListener("contextmenu", (event) => event.preventDefault());
}

function bindKeys() {
	window.addEventListener("keydown", (event) => {
		const meta = event.metaKey || event.ctrlKey;
		if (meta && event.key.toLowerCase() === "z") {
			event.preventDefault();
			if (event.shiftKey) enqueueHistory(() => redo());
			else enqueueHistory(() => undo());
		}
		if (meta && event.key.toLowerCase() === "y") {
			event.preventDefault();
			enqueueHistory(() => redo());
		}
	});
}

async function registerWorker() {
	if (!("serviceWorker" in navigator)) return;
	try {
		const isLocalDev = location.hostname === "127.0.0.1" || location.hostname === "localhost";
		const autoSkipKey = "ks_auto_skip_sw";

		const reg = await navigator.serviceWorker.register("./sw.js", { updateViaCache: "none" });

		function onNewWorker(worker) {
			if (worker.state !== "installed") {
				worker.addEventListener("statechange", () => {
					if (worker.state === "installed") onNewWorker(worker);
				});
				return;
			}

			// Local dev: force the new SW to become active immediately,
			// so you see Brush tuning changes without manual "Update now".
			if (isLocalDev && sessionStorage.getItem(autoSkipKey) !== "1") {
				sessionStorage.setItem(autoSkipKey, "1");
				worker.postMessage("skipWaiting");
				window.location.reload();
				return;
			}

			showUpdateBanner(worker);
		}

		if (reg.waiting) onNewWorker(reg.waiting);
		reg.addEventListener("updatefound", () => {
			if (reg.installing) onNewWorker(reg.installing);
		});

		setInterval(() => { reg.update().catch(() => {}); }, 10 * 60 * 1000);
	} catch (err) {
		console.warn("Service Worker not registered:", err);
	}
}

function showUpdateBanner(worker) {
	const banner = document.getElementById("update-banner");
	const btnNow = document.getElementById("update-now");
	const btnLater = document.getElementById("update-later");
	if (!banner || !btnNow || !btnLater) return;

	banner.hidden = false;

	btnLater.onclick = () => { banner.hidden = true; };

	btnNow.onclick = async () => {
		btnNow.disabled = true;
		btnNow.textContent = "Saving…";
		saveQueued = true;
		await flushSave();
		worker.postMessage("skipWaiting");
	};

	let reloading = false;
	navigator.serviceWorker.addEventListener("controllerchange", () => {
		if (reloading) return;
		reloading = true;
		window.location.reload();
	});
}

let resizeRaf = 0;
function onResize() {
	if (resizeRaf) return;
	resizeRaf = requestAnimationFrame(() => {
		resizeRaf = 0;
		sizeDisplay();
	});
}

async function init() {
	loadSettings();
	bindUi();
	updateShareUi();
	syncUi();
	syncCompactClass();
	bindCanvas();
	bindKeys();

	window.addEventListener("resize", onResize);
	for (const query of COMPACT_QUERIES) {
		window.matchMedia(query).addEventListener("change", syncCompactClass);
	}
	window.addEventListener("pagehide", () => {
		if (!persistEnabled) return;
		saveQueued = true;
		void flushSave();
	});
	document.addEventListener("visibilitychange", () => {
		if (document.visibilityState === "hidden" && persistEnabled) {
			saveQueued = true;
			void flushSave();
		}
	});

	sizeDisplay();
	fitMasterContext();
	resetBrush();
	await restoreWorkspace();
	await pushHistory();
	persistEnabled = true;

	registerWorker();
}

init();
