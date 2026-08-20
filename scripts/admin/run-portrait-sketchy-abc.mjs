/**
 * Sketchy ABC portrait experiments from Zeichenkarte stroke-plan.
 *
 * A: structure strokes, light densify, 1 pass
 * B: same + denser points / selective 2nd pass on eyes & outline
 * C: B + tone-derived hatch strokes
 *
 * Sketchy only. No Fur/Shaded/Web/Airbrush.
 *
 *   node scripts/admin/run-portrait-sketchy-abc.mjs
 */
import puppeteer from "puppeteer-core";
import { spawn } from "node:child_process";
import { mkdir, readFile, writeFile, access } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const ADMIN_PORT = Number(process.env.ADMIN_PORT) || 4174;
const BASE = `http://127.0.0.1:${ADMIN_PORT}`;
const CHROME =
	process.env.CHROME_PATH ||
	"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const OUT_DIR = path.join(ROOT, "tmp", "portrait-krenzsketch");
const PRE_DIR = path.join(ROOT, "tmp", "portrait-preprocess");
const PLAN_JSON = path.join(PRE_DIR, "portrait-strokeplan.json");

const ORDER = ["outline", "feature", "hair", "other", "hatch"];

function cfgFor(kind, region, variant) {
	const base = {
		outline: { size: 2.2, pressure: 0.5, jitter: 0.0018 },
		feature: { size: 1.6, pressure: 0.42, jitter: 0.0014 },
		hair: { size: 1.9, pressure: 0.38, jitter: 0.0022 },
		other: { size: 1.5, pressure: 0.35, jitter: 0.0016 },
		hatch: { size: 1.35, pressure: 0.28, jitter: 0.0028 },
	}[kind] || { size: 1.6, pressure: 0.4, jitter: 0.0015 };

	let densifyStep = variant === "A" ? 0.007 : 0.0035;
	if (kind === "hatch") densifyStep = 0.0045;

	let passes = 1;
	if (variant !== "A") {
		if (kind === "outline" || region === "eyes") passes = 2;
		if (region === "mouth") passes = 2;
	}

	// eyes a bit denser
	if (region === "eyes") {
		base.size = Math.min(base.size, 1.5);
		base.pressure = 0.48;
	}
	if (region === "nose") {
		base.size = 1.35;
		base.pressure = 0.32;
		passes = 1;
	}
	if (kind === "hatch") {
		passes = 1;
		base.pressure = 0.22 + Math.min(0.2, (arguments[3] || 0.4) * 0.25);
	}

	return { mode: "sketchy", ...base, densifyStep, passes, incompleteness: kind === "hatch" ? 0.08 : 0.02 };
}

async function exists(p) {
	try {
		await access(p);
		return true;
	} catch {
		return false;
	}
}

async function writeUnique(dir, name, buffer) {
	let target = path.join(dir, name);
	if (await exists(target)) {
		const stem = name.replace(/\.png$/, "");
		let i = 2;
		while (await exists(path.join(dir, `${stem}-v${i}.png`))) i += 1;
		target = path.join(dir, `${stem}-v${i}.png`);
	}
	await writeFile(target, buffer);
	return target;
}

async function waitForServer(url, ms = 20000) {
	const start = Date.now();
	while (Date.now() - start < ms) {
		try {
			const r = await fetch(url);
			if (r.ok || r.status === 404) return;
		} catch {
			/* retry */
		}
		await new Promise((r) => setTimeout(r, 200));
	}
	throw new Error(`Admin server not reachable: ${url}`);
}

async function ensureServer() {
	try {
		const r = await fetch(`${BASE}/`);
		if (r.ok) return null;
	} catch {
		/* start */
	}
	const child = spawn(process.execPath, [path.join(ROOT, "scripts/admin-server.mjs")], {
		cwd: ROOT,
		stdio: "ignore",
		detached: true,
	});
	child.unref();
	await waitForServer(`${BASE}/`);
	return child;
}

function sortStrokes(list) {
	return [...list].sort((a, b) => {
		const ka = ORDER.indexOf(a.kind);
		const kb = ORDER.indexOf(b.kind);
		if (ka !== kb) return ka - kb;
		return (b.length || 0) - (a.length || 0);
	});
}

async function exportPng(page, dir, name) {
	const b64 = await page.evaluate(async () => {
		const blob = await window.__portraitDriver.exportPngBlob();
		const buf = await blob.arrayBuffer();
		const bytes = new Uint8Array(buf);
		let binary = "";
		for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
		return btoa(binary);
	});
	return writeUnique(dir, name, Buffer.from(b64, "base64"));
}

async function drawStrokes(page, strokes, variant) {
	await page.evaluate(
		async (strokes, variant) => {
			const driver = window.__portraitDriver;
			const ORDER = ["outline", "feature", "hair", "other", "hatch"];
			function cfg(kind, region, strength) {
				const base = {
					outline: { size: 2.2, pressure: 0.5, jitter: 0.0018 },
					feature: { size: 1.6, pressure: 0.42, jitter: 0.0014 },
					hair: { size: 1.9, pressure: 0.38, jitter: 0.0022 },
					other: { size: 1.5, pressure: 0.35, jitter: 0.0016 },
					hatch: { size: 1.35, pressure: 0.28, jitter: 0.0028 },
				}[kind] || { size: 1.6, pressure: 0.4, jitter: 0.0015 };
				let densifyStep = variant === "A" ? 0.007 : 0.0035;
				if (kind === "hatch") densifyStep = 0.0045;
				let passes = 1;
				if (variant !== "A") {
					if (kind === "outline" || region === "eyes" || region === "mouth") passes = 2;
				}
				if (region === "eyes") {
					base.size = 1.45;
					base.pressure = 0.48;
				}
				if (region === "nose") {
					base.size = 1.3;
					base.pressure = 0.3;
					passes = 1;
				}
				if (kind === "hatch") {
					passes = 1;
					base.pressure = 0.2 + Math.min(0.22, (strength || 0.4) * 0.3);
					base.jitter = 0.003;
				}
				return { ...base, densifyStep, passes, incompleteness: kind === "hatch" ? 0.1 : 0.02 };
			}

			const sorted = [...strokes].sort((a, b) => {
				const ka = ORDER.indexOf(a.kind);
				const kb = ORDER.indexOf(b.kind);
				if (ka !== kb) return ka - kb;
				return (b.length || 0) - (a.length || 0);
			});

			for (const s of sorted) {
				const c = cfg(s.kind, s.region, s.strength);
				driver.setBrush("sketchy", c.size, "#1c1c1c");
				await new Promise((r) => setTimeout(r, 4));
				const pts = s.points;
				for (let pass = 0; pass < c.passes; pass++) {
					// split long hair/outline into 2–3 loose strokes occasionally
					if ((s.kind === "hair" || s.kind === "outline") && pts.length > 30 && pass === 0 && Math.random() < 0.45) {
						const n = 2 + Math.floor(Math.random() * 2);
						const chunk = Math.ceil(pts.length / n);
						for (let i = 0; i < n; i++) {
							const slice = pts.slice(Math.max(0, i * chunk - 1), (i + 1) * chunk + 1);
							if (slice.length > 2) {
								driver.strokeNorm(slice, {
									pressure: c.pressure * (0.85 + 0.15 * Math.random()),
									jitter: c.jitter,
									incompleteness: c.incompleteness,
									densifyStep: c.densifyStep,
								});
								await new Promise((r) => setTimeout(r, 2));
							}
						}
					} else {
						driver.strokeNorm(pts, {
							pressure: c.pressure * (pass === 0 ? 1 : 0.7),
							jitter: c.jitter + pass * 0.0006,
							incompleteness: c.incompleteness,
							densifyStep: c.densifyStep,
						});
					}
					await new Promise((r) => setTimeout(r, 2));
				}
			}
		},
		strokes,
		variant,
	);
}

async function main() {
	await mkdir(OUT_DIR, { recursive: true });
	const plan = JSON.parse(await readFile(PLAN_JSON, "utf8"));
	const structure = plan.structure || [];
	const hatch = plan.hatch || [];
	if (structure.length < 40) {
		throw new Error("Stroke-Plan zu dünn — zuerst zeichnkarte.py ausführen");
	}

	await ensureServer();
	const browser = await puppeteer.launch({
		executablePath: CHROME,
		headless: "new",
		args: ["--no-sandbox", "--window-size=1100,1400"],
		defaultViewport: { width: 1000, height: 1300, deviceScaleFactor: 1 },
	});
	const page = await browser.newPage();
	await page.goto(`${BASE}/`, { waitUntil: "networkidle0", timeout: 60000 });
	await page.waitForSelector("#canvas");
	await page.evaluate(async () => {
		if (!("serviceWorker" in navigator)) return;
		const regs = await navigator.serviceWorker.getRegistrations();
		for (const r of regs) await r.unregister();
	});
	await page.reload({ waitUntil: "networkidle0" });
	await page.waitForSelector("#canvas");
	await new Promise((r) => setTimeout(r, 400));

	const driverSrc = await readFile(path.join(ROOT, "admin/lib/stroke-driver.js"), "utf8");
	await page.evaluate(
		async (driverSrc, payload) => {
			const blob = new Blob([driverSrc], { type: "text/javascript" });
			const url = URL.createObjectURL(blob);
			const mod = await import(url);
			window.__portraitDriver = mod.createStrokeDriver(document);
			window.__portraitDriver.setPortraitSize(payload.width, payload.height);
			await window.__portraitDriver.clearDrawing();
		},
		driverSrc,
		{ width: plan.width, height: plan.height },
	);

	const exports = {};

	// A
	await page.evaluate(async () => {
		await window.__portraitDriver.clearDrawing();
	});
	await drawStrokes(page, structure, "A");
	exports.A = await exportPng(page, OUT_DIR, "portrait-sketchy-a.png");

	// B denser
	await page.evaluate(async () => {
		await window.__portraitDriver.clearDrawing();
	});
	await drawStrokes(page, structure, "B");
	exports.B = await exportPng(page, OUT_DIR, "portrait-sketchy-b.png");

	// C + hatch
	await page.evaluate(async () => {
		await window.__portraitDriver.clearDrawing();
	});
	await drawStrokes(page, [...structure, ...hatch], "C");
	exports.C = await exportPng(page, OUT_DIR, "portrait-sketchy-c-shading.png");

	const report = {
		sourceKarte: plan.sourceKarte,
		sourcePhoto: plan.sourcePhoto,
		stats: plan.stats,
		sketchyOnly: true,
		variants: {
			A: { file: exports.A, note: "structure only, densifyStep≈0.007, 1 pass" },
			B: { file: exports.B, note: "structure denser densifyStep≈0.0035, eyes/outline 2 passes" },
			C: { file: exports.C, note: "B + tone hatch strokes" },
		},
		strokeplanPreview: path.join(PRE_DIR, "portrait-strokeplan-a.png"),
		brushes: "sketchy only",
	};
	await writeFile(path.join(OUT_DIR, "sketchy-abc-report.json"), JSON.stringify(report, null, 2));
	console.log(JSON.stringify(report, null, 2));
	await browser.close();
}

main().catch((err) => {
	console.error(err);
	process.exit(1);
});
