/*
 * KrenzSketch — application shell
 * Copyright (C) 2026 Matthias Krenzer
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Original KrenzSketch code (PWA shell, pointer input, undo/redo, export).
 * Procedural drawing modes live in brushes.js and are derived from Harmony.
 */

import { BRUSHES, createBrush, createEraser } from "./brushes.js";

const SETTINGS_KEY = "krenzsketch-settings";
const MAX_HISTORY = 12;
const COMPACT_UI = "(max-width: 720px), (max-height: 540px)";

const canvas = document.getElementById("canvas");
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
	erasing: false,
};

let brush = null;
const undoStack = [];
const redoStack = [];

function clamp(n, min, max) {
	return Math.min(max, Math.max(min, n));
}

function isCompactUi() {
	return window.matchMedia(COMPACT_UI).matches;
}

function setToolsOpen(open) {
	document.documentElement.classList.toggle("tools-open", open);
	ui.menuBtn.setAttribute("aria-expanded", open ? "true" : "false");
	ui.menuBtn.classList.toggle("is-active", open && isCompactUi());
}

function collapseToolsIfCompact() {
	if (isCompactUi()) setToolsOpen(false);
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
	ui.sizeValue.textContent = String(state.size).replace(".", ",");
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
	return {
		x: event.clientX - rect.left,
		y: event.clientY - rect.top,
	};
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
	const rect = canvas.getBoundingClientRect();
	const dpr = Math.max(1, window.devicePixelRatio || 1);
	const width = Math.max(1, Math.round(rect.width * dpr));
	const height = Math.max(1, Math.round(rect.height * dpr));
	if (canvas.width === width && canvas.height === height) return;

	canvas.width = width;
	canvas.height = height;
	restoreSnapshot(prev.width ? prev : null);
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
}

function redo() {
	if (!redoStack.length) return;
	const snapshot = redoStack.pop();
	undoStack.push(snapshot);
	restoreSnapshot(snapshot);
	resetBrush();
	updateHistoryButtons();
}

function updateHistoryButtons() {
	ui.undo.disabled = undoStack.length < 2;
	ui.redo.disabled = redoStack.length === 0;
}

function clearCanvas() {
	ctx.save();
	ctx.globalCompositeOperation = "source-over";
	ctx.setTransform(1, 0, 0, 1, 0, 0);
	ctx.clearRect(0, 0, canvas.width, canvas.height);
	ctx.restore();
	fitContext();
	resetBrush();
	pushHistory();
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

function onPointerDown(event) {
	if (isCompactUi() && document.documentElement.classList.contains("tools-open")) {
		setToolsOpen(false);
	}
	if (state.drawing) return;
	if (event.button != null && event.button !== 0) return;
	event.preventDefault();
	canvas.setPointerCapture(event.pointerId);
	state.drawing = true;
	state.pointerId = event.pointerId;
	const { x, y } = canvasPoint(event);
	brush.strokeStart(x, y);
}

function onPointerMove(event) {
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
	if (!state.drawing || event.pointerId !== state.pointerId) return;
	event.preventDefault();
	brush.strokeEnd();
	state.drawing = false;
	state.pointerId = null;
	pushHistory();
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
	});
	ui.paper.addEventListener("change", collapseToolsIfCompact);

	ui.size.addEventListener("input", () => {
		state.size = Number(ui.size.value);
		ui.sizeValue.textContent = String(state.size).replace(".", ",");
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

	ui.clear.addEventListener("click", () => {
		ui.confirm.showModal();
	});
	ui.confirmOk.addEventListener("click", () => {
		ui.confirm.close();
		clearCanvas();
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
			if (event.shiftKey) redo();
			else undo();
		}
		if (meta && event.key.toLowerCase() === "y") {
			event.preventDefault();
			redo();
		}
	});
}

async function registerWorker() {
	if (!("serviceWorker" in navigator)) return;
	try {
		await navigator.serviceWorker.register("./sw.js", { updateViaCache: "none" });
	} catch (err) {
		console.warn("Service Worker nicht registriert:", err);
	}
}

function init() {
	loadSettings();
	bindUi();
	syncUi();
	resizeCanvas();
	resetBrush();
	pushHistory();
	bindCanvas();
	bindKeys();
	window.addEventListener("resize", resizeCanvas);
	window.matchMedia(COMPACT_UI).addEventListener("change", (event) => {
		if (!event.matches) setToolsOpen(false);
	});
	registerWorker();
}

init();
