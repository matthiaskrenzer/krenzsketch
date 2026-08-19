/*
 * KrenzSketch — local drawing persistence
 * Copyright (C) 2026 Matthias Krenzer
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Stores the current working image in IndexedDB as a PNG blob.
 * Tool settings stay in localStorage (handled in app.js).
 */

const DB_NAME = "krenzsketch";
const DB_VERSION = 1;
const STORE = "workspace";
const RECORD_KEY = "current";

function openDb() {
	return new Promise((resolve, reject) => {
		if (!("indexedDB" in window)) {
			reject(new Error("IndexedDB unavailable"));
			return;
		}
		const request = indexedDB.open(DB_NAME, DB_VERSION);
		request.onupgradeneeded = () => {
			const db = request.result;
			if (!db.objectStoreNames.contains(STORE)) {
				db.createObjectStore(STORE);
			}
		};
		request.onsuccess = () => resolve(request.result);
		request.onerror = () => reject(request.error ?? new Error("IndexedDB open failed"));
	});
}

function idbRequest(request) {
	return new Promise((resolve, reject) => {
		request.onsuccess = () => resolve(request.result);
		request.onerror = () => reject(request.error ?? new Error("IndexedDB request failed"));
	});
}

export async function saveWorkspace(record) {
	const db = await openDb();
	try {
		const tx = db.transaction(STORE, "readwrite");
		tx.objectStore(STORE).put(record, RECORD_KEY);
		await new Promise((resolve, reject) => {
			tx.oncomplete = resolve;
			tx.onerror = () => reject(tx.error);
			tx.onabort = () => reject(tx.error);
		});
	} finally {
		db.close();
	}
}

export async function loadWorkspace() {
	const db = await openDb();
	try {
		const tx = db.transaction(STORE, "readonly");
		const record = await idbRequest(tx.objectStore(STORE).get(RECORD_KEY));
		return record ?? null;
	} finally {
		db.close();
	}
}

export async function clearWorkspace() {
	const db = await openDb();
	try {
		const tx = db.transaction(STORE, "readwrite");
		tx.objectStore(STORE).delete(RECORD_KEY);
		await new Promise((resolve, reject) => {
			tx.oncomplete = resolve;
			tx.onerror = () => reject(tx.error);
			tx.onabort = () => reject(tx.error);
		});
	} finally {
		db.close();
	}
}

export function canvasToPngBlob(source) {
	return new Promise((resolve, reject) => {
		source.toBlob((blob) => {
			if (blob) resolve(blob);
			else reject(new Error("PNG encoding failed"));
		}, "image/png");
	});
}
