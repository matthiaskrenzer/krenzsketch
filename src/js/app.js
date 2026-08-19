/*
 * KrenzSketch — application shell
 * Copyright (C) 2026 Matthias Krenzer
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Original KrenzSketch code (PWA shell, pointer input, undo/redo, export,
 * view transform, local persistence). Procedural drawing modes live in
 * brushes.js and are derived from Harmony.
 */

import { BRUSHES, createBrush, createEraser } from "./brushes.js";
import { canvasToPngBlob, clearWorkspace, loadWorkspace, saveWorkspace } from "./persist.js";

const SETTINGS_KEY = "krenzsketch-settings";
const MAX_HISTORY = 12;
const MIN_ZOOM = 0.25;
const MAX_ZOOM = 8;
const SAVE_DELAY_MS = 400;
const COMPACT_QUERIES = ["(hover: none)", "(pointer: coarse)", "(max-width: 1024px)", "(max-height: 700px)"];

const canvas = document.getElementById("canvas");
const stage = document.getElementById("stage");
const viewport = document.getElementById("viewport");
const ctx = canvas.getContext("2d", { alpha: true, willReadFrequently: false });

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
	resetView: document.getElementById("reset-view"),
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
	gesturing: false,
	panning: false,
	spaceDown: false,
};

const view = {
	scale: 1,
	x: 0,
	y: 0,
};

const pointers = new Map();
let pinch = null;
let panOrigin = null;
let brush = null;
const undoStack = [];
const redoStack = [];
let saveTimer = 0;
let saveQueued = false;
let saveGeneration = 0;
let persistEnabled = false;

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
	syncEraseUi();
}

function syncEraseUi() {
	ui.eraser.setAttribute("aria-pressed", state.erasing ? "true" : "false");
	ui.eraser.classList.toggle("is-active", state.erasing);
	canvas.classList.toggle("is-erasing", state.erasing);
}

function currentStyle(pressure) {
	return {
		color: state.color,
		size: state.size,
		pressure,
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
	if (rect.width < 1 || rect.height < 1) return { x: 0, y: 0 };
	return {
		x: ((event.clientX - rect.left) / rect.width) * canvas.clientWidth,
		y: ((event.clientY - rect.top) / rect.height) * canvas.clientHeight,
	};
}

function eventOnCanvas(event) {
	const rect = canvas.getBoundingClientRect();
	return event.clientX >= rect.left && event.clientX <= rect.right && event.clientY >= rect.top && event.clientY <= rect.bottom;
}

function snapshotCanvas() {
	const copy = document.createElement("canvas");
	copy.width = canvas.width;
	copy.height = canvas.height;
	const copyCtx = copy.getContext("2d");
	copyCtx.globalCompositeOperation = "source-over";
	copyCtx.drawImage(canvas, 0, 0);
	return copy;
}

function restoreSnapshot(snapshot) {
	ctx.save();
	ctx.globalCompositeOperation = "source-over";
	ctx.setTransform(1, 0, 0, 1, 0, 0);
	ctx.clearRect(0, 0, canvas.width, canvas.height);
	if (snapshot) {
		ctx.drawImage(snapshot, 0, 0, canvas.width, canvas.height);
	}
	ctx.restore();
	fitContext();
	ctx.globalCompositeOperation = state.erasing ? "destination-out" : "source-over";
}

function fitContext() {
	const dpr = Math.max(1, window.devicePixelRatio || 1);
	ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

function resizeCanvas() {
	const prev = snapshotCanvas();
	const dpr = Math.max(1, window.devicePixelRatio || 1);
	const cssW = Math.max(1, canvas.clientWidth);
	const cssH = Math.max(1, canvas.clientHeight);
	const width = Math.max(1, Math.round(cssW * dpr));
	const height = Math.max(1, Math.round(cssH * dpr));
	if (canvas.width === width && canvas.height === height) return;

	canvas.width = width;
	canvas.height = height;
	restoreSnapshot(prev.width ? prev : null);
}

function applyView() {
	viewport.style.transform = `translate(${view.x}px, ${view.y}px) scale(${view.scale})`;
}

function resetView() {
	view.scale = 1;
	view.x = 0;
	view.y = 0;
	applyView();
}

function zoomAt(clientX, clientY, nextScale) {
	const scale = clamp(nextScale, MIN_ZOOM, MAX_ZOOM);
	const rect = stage.getBoundingClientRect();
	const sx = clientX - rect.left;
	const sy = clientY - rect.top;
	const contentX = (sx - view.x) / view.scale;
	const contentY = (sy - view.y) / view.scale;
	view.scale = scale;
	view.x = sx - contentX * scale;
	view.y = sy - contentY * scale;
	applyView();
}

function resetBrush() {
	brush = state.erasing ? createEraser(ctx) : createBrush(state.mode, ctx);
}

function pushHistory() {
	redoStack.length = 0;
	undoStack.push(snapshotCanvas());
	while (undoStack.length > MAX_HISTORY) undoStack.shift();
	updateHistoryButtons();
}

function undo() {
	if (undoStack.length < 2) return;
	redoStack.push(undoStack.pop());
	restoreSnapshot(undoStack[undoStack.length - 1]);
	resetBrush();
	updateHistoryButtons();
	scheduleSave();
}

function redo() {
	if (!redoStack.length) return;
	const snapshot = redoStack.pop();
	undoStack.push(snapshot);
	restoreSnapshot(snapshot);
	resetBrush();
	updateHistoryButtons();
	scheduleSave();
}

function updateHistoryButtons() {
	ui.undo.disabled = undoStack.length < 2;
	ui.redo.disabled = redoStack.length === 0;
}

function resetHistory() {
	undoStack.length = 0;
	redoStack.length = 0;
	pushHistory();
}

function clearCanvas() {
	ctx.save();
	ctx.globalCompositeOperation = "source-over";
	ctx.setTransform(1, 0, 0, 1, 0, 0);
	ctx.clearRect(0, 0, canvas.width, canvas.height);
	ctx.restore();
	fitContext();
	resetBrush();
	resetHistory();
	resetView();
}

function exportPng() {
	const out = document.createElement("canvas");
	out.width = canvas.width;
	out.height = canvas.height;
	const outCtx = out.getContext("2d");
	outCtx.globalCompositeOperation = "source-over";
	outCtx.fillStyle = cssRgb(state.background);
	outCtx.fillRect(0, 0, out.width, out.height);
	outCtx.drawImage(canvas, 0, 0);
	out.toBlob((blob) => {
		if (!blob) return;
		const url = URL.createObjectURL(blob);
		const link = document.createElement("a");
		link.href = url;
		link.download = "krenzsketch.png";
		document.body.appendChild(link);
		link.click();
		link.remove();
		URL.revokeObjectURL(url);
	}, "image/png");
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
		const blob = await canvasToPngBlob(canvas);
		if (generation !== saveGeneration) return;
		await saveWorkspace({
			blob,
			background: state.background.slice(),
			width: canvas.width,
			height: canvas.height,
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
		restoreSnapshot(bitmap);
		if (typeof bitmap.close === "function") bitmap.close();
	} catch (err) {
		console.warn("Could not restore drawing:", err);
	}
}

function touchPointers() {
	const list = [];
	for (const pointer of pointers.values()) {
		if (pointer.type === "touch") list.push(pointer);
	}
	return list;
}

function trackPointer(event) {
	pointers.set(event.pointerId, {
		id: event.pointerId,
		type: event.pointerType || "mouse",
		x: event.clientX,
		y: event.clientY,
	});
}

function beginPinch() {
	const pts = touchPointers();
	if (pts.length < 2) return;
	state.gesturing = true;
	const [a, b] = pts;
	pinch = {
		startDist: Math.hypot(b.x - a.x, b.y - a.y) || 1,
		startScale: view.scale,
		startMid: { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 },
		startView: { x: view.x, y: view.y },
	};
}

function updatePinch() {
	const pts = touchPointers();
	if (pts.length < 2 || !pinch) return;
	const [a, b] = pts;
	const dist = Math.hypot(b.x - a.x, b.y - a.y) || 1;
	const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
	const nextScale = clamp(pinch.startScale * (dist / pinch.startDist), MIN_ZOOM, MAX_ZOOM);
	const rect = stage.getBoundingClientRect();
	const sx = pinch.startMid.x - rect.left;
	const sy = pinch.startMid.y - rect.top;
	const contentX = (sx - pinch.startView.x) / pinch.startScale;
	const contentY = (sy - pinch.startView.y) / pinch.startScale;
	view.scale = nextScale;
	view.x = sx - contentX * nextScale + (mid.x - pinch.startMid.x);
	view.y = sy - contentY * nextScale + (mid.y - pinch.startMid.y);
	applyView();
}

function endPinch() {
	state.gesturing = false;
	pinch = null;
}

function beginMousePan(event) {
	state.panning = true;
	state.pointerId = event.pointerId;
	panOrigin = { x: event.clientX - view.x, y: event.clientY - view.y };
	document.body.classList.add("is-panning-active");
	try {
		stage.setPointerCapture(event.pointerId);
	} catch {
		/* ignore */
	}
}

function cancelTouchStroke() {
	if (!state.drawing || state.pointerType === "pen") return;
	state.drawing = false;
	state.pointerId = null;
	if (typeof brush.strokeEnd === "function") brush.strokeEnd();
	restoreSnapshot(undoStack[undoStack.length - 1] ?? null);
	resetBrush();
}

function startDraw(event) {
	event.preventDefault();
	if (event.pointerType !== "touch") {
		try {
			canvas.setPointerCapture(event.pointerId);
		} catch {
			/* ignore */
		}
	}
	state.drawing = true;
	state.pointerId = event.pointerId;
	state.pointerType = event.pointerType || "mouse";
	const { x, y } = canvasPoint(event);
	brush.strokeStart(x, y);
}

function onPointerDown(event) {
	trackPointer(event);

	if (isCompactUi() && document.documentElement.classList.contains("tools-open")) {
		setToolsOpen(false);
	}

	if (event.pointerType === "touch" && state.drawing && state.pointerType === "pen") {
		return;
	}

	if (event.pointerType === "touch" && touchPointers().length >= 2) {
		cancelTouchStroke();
		beginPinch();
		event.preventDefault();
		return;
	}

	if (state.gesturing || state.panning || state.drawing) return;

	const mouse = !event.pointerType || event.pointerType === "mouse";
	if (mouse && state.spaceDown && (event.button == null || event.button === 0)) {
		beginMousePan(event);
		event.preventDefault();
		return;
	}

	if (event.button != null && event.button !== 0) return;
	if (!eventOnCanvas(event)) return;
	startDraw(event);
}

function onPointerMove(event) {
	if (pointers.has(event.pointerId)) {
		const pointer = pointers.get(event.pointerId);
		pointer.x = event.clientX;
		pointer.y = event.clientY;
	}

	if (state.gesturing) {
		event.preventDefault();
		updatePinch();
		return;
	}

	if (state.panning && event.pointerId === state.pointerId && panOrigin) {
		event.preventDefault();
		view.x = event.clientX - panOrigin.x;
		view.y = event.clientY - panOrigin.y;
		applyView();
		return;
	}

	if (!state.drawing || event.pointerId !== state.pointerId) return;
	event.preventDefault();
	const events = typeof event.getCoalescedEvents === "function" ? event.getCoalescedEvents() : [event];
	const list = events.length ? events : [event];
	for (const ev of list) {
		const { x, y } = canvasPoint(ev);
		brush.stroke(x, y, currentStyle(pointerPressure(ev)));
	}
}

function onPointerUp(event) {
	pointers.delete(event.pointerId);

	if (state.gesturing) {
		if (touchPointers().length < 2) endPinch();
		event.preventDefault();
		return;
	}

	if (state.panning && event.pointerId === state.pointerId) {
		state.panning = false;
		state.pointerId = null;
		panOrigin = null;
		document.body.classList.remove("is-panning-active");
		event.preventDefault();
		return;
	}

	if (!state.drawing || event.pointerId !== state.pointerId) return;
	event.preventDefault();
	brush.strokeEnd();
	state.drawing = false;
	state.pointerId = null;
	pushHistory();
	scheduleSave(true);
}

function onWheel(event) {
	event.preventDefault();
	const intensity = event.ctrlKey ? -event.deltaY * 0.01 : -event.deltaY * 0.0025;
	zoomAt(event.clientX, event.clientY, view.scale * Math.exp(intensity));
}

function onGesture(event) {
	event.preventDefault();
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
		syncEraseUi();
		saveSettings();
		collapseToolsIfCompact();
	});

	ui.eraser.addEventListener("click", () => {
		state.erasing = !state.erasing;
		resetBrush();
		syncEraseUi();
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
		undo();
		collapseToolsIfCompact();
	});
	ui.redo.addEventListener("click", () => {
		redo();
		collapseToolsIfCompact();
	});
	ui.exportBtn.addEventListener("click", () => {
		exportPng();
		collapseToolsIfCompact();
	});
	ui.resetView.addEventListener("click", () => {
		resetView();
		collapseToolsIfCompact();
	});

	ui.clear.addEventListener("click", () => {
		ui.confirm.showModal();
	});
	ui.confirmOk.addEventListener("click", () => {
		ui.confirm.close();
		clearCanvas();
		void discardSavedDrawing();
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

	for (const link of document.querySelectorAll(".tool-legal a, #menu-dialog a")) {
		link.addEventListener("click", () => {
			saveQueued = true;
			void flushSave();
		});
	}
}

function bindCanvas() {
	stage.addEventListener("pointerdown", onPointerDown);
	stage.addEventListener("pointermove", onPointerMove);
	stage.addEventListener("pointerup", onPointerUp);
	stage.addEventListener("pointercancel", onPointerUp);
	stage.addEventListener("wheel", onWheel, { passive: false });
	stage.addEventListener("contextmenu", (event) => event.preventDefault());
	stage.addEventListener("gesturestart", onGesture);
	stage.addEventListener("gesturechange", onGesture);
	stage.addEventListener("gestureend", onGesture);
	document.addEventListener("gesturestart", onGesture);
	document.addEventListener("gesturechange", onGesture);
}

function isTypingTarget(event) {
	const target = event.target;
	if (!(target instanceof Element)) return false;
	return Boolean(target.closest("input, select, textarea, button, dialog"));
}

function bindKeys() {
	window.addEventListener("keydown", (event) => {
		if (event.code === "Space" && !event.repeat && !isTypingTarget(event)) {
			event.preventDefault();
			state.spaceDown = true;
			document.body.classList.add("is-panning");
		}
		if (event.key === "1" && !isTypingTarget(event) && !event.metaKey && !event.ctrlKey) {
			resetView();
		}
		const meta = event.metaKey || event.ctrlKey;
		if (meta && event.key.toLowerCase() === "z") {
			event.preventDefault();
			if (event.shiftKey) redo();
			else undo();
		}
		if (meta && event.key.toLowerCase() === "y") {
			event.preventDefault();
			redo();
		}
	});
	window.addEventListener("keyup", (event) => {
		if (event.code === "Space") {
			state.spaceDown = false;
			document.body.classList.remove("is-panning");
		}
	});
	window.addEventListener("blur", () => {
		state.spaceDown = false;
		document.body.classList.remove("is-panning");
	});
}

async function registerWorker() {
	if (!("serviceWorker" in navigator)) return;
	try {
		await navigator.serviceWorker.register("./sw.js", { updateViaCache: "none" });
	} catch (err) {
		console.warn("Service Worker not registered:", err);
	}
}

async function init() {
	loadSettings();
	bindUi();
	syncUi();
	syncCompactClass();
	bindCanvas();
	bindKeys();
	window.addEventListener("resize", resizeCanvas);
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
	await new Promise((resolve) => requestAnimationFrame(resolve));
	resizeCanvas();
	resetBrush();
	await restoreWorkspace();
	pushHistory();
	persistEnabled = true;
	registerWorker();
}

init();
