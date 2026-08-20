/**
 * Soft-source → Sketchy A/B/C via real PointerEvents.
 *
 * Frozen soft sources only. Sketchy only. No canvas/SVG import.
 *
 *   node scripts/admin/run-soft-sketchy.mjs
 *   node scripts/admin/run-soft-sketchy.mjs ref
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
const PLAN_DIR = path.join(ROOT, "tmp", "soft-sketchy");
const OUT_DIR = path.join(ROOT, "tmp", "soft-sketchy");

const JOBS = {
	ref: {
		plan: "ref-strokeplan.json",
		a: "portrait-soft-sketchy-a.png",
		b: "portrait-soft-sketchy-b.png",
		c: "portrait-soft-sketchy-c-hatch.png",
	},
	"dark-portrait": {
		plan: "dark-portrait-strokeplan.json",
		a: "dark-portrait-soft-sketchy-a.png",
		b: "dark-portrait-soft-sketchy-b.png",
		c: "dark-portrait-soft-sketchy-c-hatch.png",
	},
	"scene-tango": {
		plan: "scene-tango-strokeplan.json",
		a: "scene-tango-soft-sketchy-a.png",
		b: "scene-tango-soft-sketchy-b.png",
		c: "scene-tango-soft-sketchy-c-hatch.png",
	},
};

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

async function waitForServer(url, ms = 25000) {
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
		if (r.ok) return;
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

			function cfg(s) {
				const kind = s.kind;
				const region = s.region;
				const strength = s.strength || 0.4;
				const base = {
					outline: { size: 1.9, pressure: 0.42, jitter: 0.0016 },
					feature: { size: 1.45, pressure: 0.4, jitter: 0.0012 },
					hair: { size: 1.7, pressure: 0.34, jitter: 0.002 },
					other: { size: 1.35, pressure: 0.32, jitter: 0.0014 },
					hatch: { size: 1.25, pressure: 0.22, jitter: 0.0026 },
				}[kind] || { size: 1.4, pressure: 0.35, jitter: 0.0015 };

				// soft gray → priority: stronger source → slightly higher pressure
				base.pressure = Math.min(0.62, base.pressure * (0.75 + 0.55 * strength));

				let densifyStep = variant === "A" ? 0.0065 : 0.0038;
				if (kind === "hatch") densifyStep = 0.005;

				let passes = 1;
				if (variant === "B" || variant === "C") {
					if (kind === "outline" || region === "eyes" || region === "mouth" || region === "jaw") {
						passes = strength > 0.28 ? 2 : 1;
					}
					if (kind === "hair" && strength > 0.45) passes = 2;
				}
				if (region === "nose") {
					base.size = 1.2;
					base.pressure = Math.min(base.pressure, 0.3);
					passes = 1;
				}
				if (region === "eyes") {
					base.size = 1.35;
					base.pressure = Math.min(0.55, base.pressure + 0.06);
				}
				if (kind === "hatch") {
					passes = 1;
					base.pressure = 0.16 + Math.min(0.2, strength * 0.28);
				}

				// A: light pencil — drop weakest
				if (variant === "A" && strength < 0.12 && kind !== "outline") {
					return null;
				}
				return {
					...base,
					densifyStep,
					passes,
					incompleteness: kind === "hatch" ? 0.12 : 0.03,
				};
			}

			const sorted = [...strokes].sort((a, b) => {
				const ka = ORDER.indexOf(a.kind);
				const kb = ORDER.indexOf(b.kind);
				if (ka !== kb) return ka - kb;
				return (b.strength || 0) - (a.strength || 0);
			});

			let drawn = 0;
			for (const s of sorted) {
				const c = cfg(s);
				if (!c) continue;
				driver.setBrush("sketchy", c.size, "#1c1c1c");
				await new Promise((r) => setTimeout(r, 3));
				const pts = s.points;
				for (let pass = 0; pass < c.passes; pass++) {
					// split long strokes into 2–3 natural pieces occasionally
					if ((s.kind === "hair" || s.kind === "outline") && pts.length > 28 && pass === 0 && Math.random() < 0.4) {
						const n = 2 + Math.floor(Math.random() * 2);
						const chunk = Math.ceil(pts.length / n);
						for (let i = 0; i < n; i++) {
							const slice = pts.slice(Math.max(0, i * chunk - 1), (i + 1) * chunk + 1);
							if (slice.length > 2) {
								driver.strokeNorm(slice, {
									pressure: c.pressure * (0.85 + 0.15 * Math.random()) * (pass === 0 ? 1 : 0.72),
									jitter: c.jitter + pass * 0.0005,
									incompleteness: c.incompleteness,
									densifyStep: c.densifyStep,
								});
								drawn += 1;
								await new Promise((r) => setTimeout(r, 2 + Math.random() * 4));
							}
						}
					} else {
						driver.strokeNorm(pts, {
							pressure: c.pressure * (pass === 0 ? 1 : 0.7),
							jitter: c.jitter + pass * 0.0005,
							incompleteness: c.incompleteness,
							densifyStep: c.densifyStep,
						});
						drawn += 1;
						await new Promise((r) => setTimeout(r, 2 + Math.random() * 3));
					}
				}
			}
			return drawn;
		},
		strokes,
		variant,
	);
}

async function runJob(browser, jobKey) {
	const job = JOBS[jobKey];
	const plan = JSON.parse(await readFile(path.join(PLAN_DIR, job.plan), "utf8"));
	const structure = plan.structure || [];
	const hatch = plan.hatch || [];
	if (structure.length < 15) {
		throw new Error(`${jobKey}: too few structure strokes (${structure.length})`);
	}

	const page = await browser.newPage();
	await page.setViewport({ width: 1000, height: 1300, deviceScaleFactor: 1 });
	await page.goto(`${BASE}/`, { waitUntil: "networkidle0", timeout: 60000 });
	await page.waitForSelector("#canvas");
	await page.evaluate(async () => {
		if (!("serviceWorker" in navigator)) return;
		const regs = await navigator.serviceWorker.getRegistrations();
		for (const r of regs) await r.unregister();
	});
	await page.reload({ waitUntil: "networkidle0" });
	await page.waitForSelector("#canvas");
	await new Promise((r) => setTimeout(r, 350));

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

	const files = {};

	await page.evaluate(async () => {
		await window.__portraitDriver.clearDrawing();
	});
	await drawStrokes(page, structure, "A");
	files.A = await exportPng(page, OUT_DIR, job.a);

	await page.evaluate(async () => {
		await window.__portraitDriver.clearDrawing();
	});
	await drawStrokes(page, structure, "B");
	files.B = await exportPng(page, OUT_DIR, job.b);

	await page.evaluate(async () => {
		await window.__portraitDriver.clearDrawing();
	});
	await drawStrokes(page, [...structure, ...hatch], "C");
	files.C = await exportPng(page, OUT_DIR, job.c);

	await page.close();

	const avgPts = structure.length
		? structure.reduce((s, x) => s + (x.nPoints || 0), 0) / structure.length
		: 0;

	return {
		job: jobKey,
		softSource: plan.softSource,
		method: plan.method,
		stats: plan.stats,
		avgPointsStructure: Math.round(avgPts * 10) / 10,
		sketchySizes: "outline~1.9 feature~1.45 hair~1.7 hatch~1.25 (strength-modulated pressure)",
		pointerEvents: true,
		noCanvasImport: true,
		noSvgImport: true,
		brush: "sketchy only",
		files,
		strokesA: structure.length,
		strokesB: structure.length,
		hatchC: hatch.length,
	};
}

async function main() {
	await mkdir(OUT_DIR, { recursive: true });
	const only = process.argv[2];
	const keys = only && JOBS[only] ? [only] : Object.keys(JOBS);

	await ensureServer();
	const browser = await puppeteer.launch({
		executablePath: CHROME,
		headless: "new",
		args: ["--no-sandbox", "--window-size=1100,1400"],
		defaultViewport: { width: 1000, height: 1300, deviceScaleFactor: 1 },
	});

	const reports = [];
	for (const key of keys) {
		console.log("plotting", key);
		reports.push(await runJob(browser, key));
	}
	await browser.close();

	const out = {
		philosophy: "Soft source is the roadmap; Sketchy draws the picture",
		reports,
	};
	await writeFile(path.join(OUT_DIR, "SOFT_SKETCHY_REPORT.json"), JSON.stringify(out, null, 2));
	console.log(JSON.stringify(out, null, 2));
}

main().catch((err) => {
	console.error(err);
	process.exit(1);
});
