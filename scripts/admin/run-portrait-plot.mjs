/**
 * Plot chosen portrait paths into KrenzSketch via real PointerEvents.
 *
 * Prerequisites:
 *   npm run build
 *   PYTHONPATH=admin/preprocess/vendor python3 admin/preprocess/lineart.py ...
 *   node scripts/admin-server.mjs   (or this script starts it)
 *
 * Usage:
 *   node scripts/admin/run-portrait-plot.mjs [paths.json]
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
const PATHS_JSON =
	process.argv[2] || path.join(PRE_DIR, "portrait-paths-chosen.json");

const KIND_ORDER = ["outline", "feature", "hair", "other"];

const BRUSH_FOR = {
	outline: { mode: "sketchy", size: 2.5, color: "#1a1a1a", pressure: 0.55, incompleteness: 0.0, passes: 1 },
	feature: { mode: "sketchy", size: 1.5, color: "#151515", pressure: 0.45, incompleteness: 0.02, passes: 1 },
	hair: { mode: "sketchy", size: 2.0, color: "#1a1a1a", pressure: 0.4, incompleteness: 0.05, passes: 1 },
	other: { mode: "sketchy", size: 1.4, color: "#222222", pressure: 0.35, incompleteness: 0.08, passes: 1 },
};

function uniqueName(base) {
	return base; // caller creates OUT_DIR fresh-ish; we append suffixes if exists
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

async function waitForServer(url, ms = 15000) {
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
		const r = await fetch(`${BASE}/admin/portrait-preprocess.html`);
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
	await waitForServer(`${BASE}/admin/portrait-preprocess.html`);
	return child;
}

function sortPaths(paths) {
	return [...paths].sort((a, b) => {
		const ka = KIND_ORDER.indexOf(a.kind);
		const kb = KIND_ORDER.indexOf(b.kind);
		if (ka !== kb) return ka - kb;
		return (b.length || 0) - (a.length || 0);
	});
}

async function main() {
	await mkdir(OUT_DIR, { recursive: true });
	const readyPath = path.join(PRE_DIR, "PLOT_READY");
	try {
		const ready = (await readFile(readyPath, "utf8")).trim();
		if (ready !== "yes") {
			console.error(
				"PLOT blocked: path preview failed acceptance gate.\n" +
					`See ${path.join(PRE_DIR, "PLOT_BLOCKED.txt")} and portrait-path-preview.svg`,
			);
			process.exit(2);
		}
	} catch {
		console.error("PLOT blocked: missing PLOT_READY — run lineart.py first.");
		process.exit(2);
	}

	const data = JSON.parse(await readFile(PATHS_JSON, "utf8"));
	const paths = sortPaths(data.paths || []);
	if (!paths.length) throw new Error("Keine Pfade in " + PATHS_JSON);

	await ensureServer();

	const browser = await puppeteer.launch({
		executablePath: CHROME,
		headless: "new",
		args: ["--no-sandbox", `--window-size=1100,1400`],
		defaultViewport: { width: 1000, height: 1300, deviceScaleFactor: 1 },
	});

	const page = await browser.newPage();
	await page.goto(`${BASE}/`, { waitUntil: "networkidle0", timeout: 60000 });
	await page.waitForSelector("#canvas");
	// Disable SW interference for deterministic drawing
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
			const driver = mod.createStrokeDriver(document);
			window.__portraitDriver = driver;
			window.__portraitPayload = payload;
			driver.setPortraitSize(payload.width, payload.height);
			await driver.clearDrawing();
		},
		driverSrc,
		{ width: data.width, height: data.height, paths },
	);

	const drawn = { outline: 0, feature: 0, hair: 0, shadow: 0, other: 0 };
	const exports = {};

	async function drawKinds(kinds, label) {
		await page.evaluate(
			async (kinds, brushFor) => {
				const driver = window.__portraitDriver;
				const paths = window.__portraitPayload.paths.filter((p) => kinds.includes(p.kind));
				for (const p of paths) {
					const cfg = brushFor[p.kind] || brushFor.other;
					let mode = cfg.mode;
					driver.setBrush(mode, cfg.size, cfg.color);
					await new Promise((r) => setTimeout(r, 10));
					const pts = p.points;
					const passes = cfg.passes || 1;
					for (let pass = 0; pass < passes; pass++) {
						if ((p.kind === "outline" || p.kind === "hair") && pts.length > 24 && pass === 0) {
							const thirds = Math.ceil(pts.length / 3);
							for (let s = 0; s < 3; s++) {
								const slice = pts.slice(Math.max(0, s * thirds - 1), (s + 1) * thirds + 1);
								if (slice.length > 2) {
									driver.strokeNorm(slice, {
										pressure: cfg.pressure,
										incompleteness: cfg.incompleteness,
										jitter: 0.0022,
									});
									await new Promise((r) => setTimeout(r, 4));
								}
							}
						} else {
							driver.strokeNorm(pts, {
								pressure: cfg.pressure * (pass === 0 ? 1 : 0.75),
								incompleteness: cfg.incompleteness,
								jitter: 0.002 + pass * 0.0008,
							});
						}
						await new Promise((r) => setTimeout(r, 4));
					}
					await new Promise((r) => setTimeout(r, 4));
				}
			},
			kinds,
			BRUSH_FOR,
		);
		for (const p of paths) {
			if (kinds.includes(p.kind)) drawn[p.kind] = (drawn[p.kind] || 0) + 1;
		}
		const b64 = await page.evaluate(async () => {
			const blob = await window.__portraitDriver.exportPngBlob();
			const buf = await blob.arrayBuffer();
			const bytes = new Uint8Array(buf);
			let binary = "";
			for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
			return btoa(binary);
		});
		const buf = Buffer.from(b64, "base64");
		exports[label] = await writeUnique(OUT_DIR, label, buf);
	}

	await drawKinds(["outline"], "portrait-krenzsketch-outline.png");
	await drawKinds(["feature"], "portrait-krenzsketch-features.png");
	await drawKinds(["hair", "shadow", "other"], "portrait-krenzsketch-final.png");

	// Also copy chosen lineart/svg references into out dir notes
	const report = {
		pathsJson: PATHS_JSON,
		variant: data.variant,
		pathCount: paths.length,
		drawn,
		exports,
		brushes: BRUSH_FOR,
		strokesOnly: true,
		note: "Final PNG consists only of KrenzSketch brush engine strokes via PointerEvents; photo never drawn onto canvas.",
	};
	await writeFile(path.join(OUT_DIR, "plot-report.json"), JSON.stringify(report, null, 2));

	console.log(JSON.stringify(report, null, 2));
	await browser.close();
}

main().catch((err) => {
	console.error(err);
	process.exit(1);
});
