/**
 * Soft Sketchy D (balanced) / E (pencil) from B-based stroke plans.
 * Does not regenerate soft source. Sketchy only. PointerEvents only.
 *
 *   node scripts/admin/run-soft-sketchy-de.mjs
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
const OUT_DIR = path.join(ROOT, "tmp", "soft-sketchy");

const VARIANTS = [
	{
		key: "D",
		plan: "ref-strokeplan-d.json",
		out: "portrait-soft-sketchy-d-balanced.png",
	},
	{
		key: "E",
		plan: "ref-strokeplan-e.json",
		out: "portrait-soft-sketchy-e-pencil.png",
	},
];

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

async function drawStrokes(page, strokes, variant, playback) {
	const secondPasses = await page.evaluate(
		async (strokes, variant, playback) => {
			const driver = window.__portraitDriver;
			const ORDER = ["hatch", "feature", "outline", "hair", "other"];
			let secondPassCount = 0;

			function cfg(s) {
				const kind = s.kind;
				const region = s.region;
				const strength = s.strength || 0.3;
				const sizes = {
					outline: playback.outlineSize,
					feature: playback.featureSize,
					hair: playback.hairSize,
					other: playback.featureSize * 0.95,
					hatch: playback.hatchSize,
				};
				const pressures = {
					outline: playback.outlinePressure,
					feature: 0.34,
					hair: 0.28,
					other: 0.28,
					hatch: 0.16,
				};
				const base = {
					size: sizes[kind] || 1.25,
					pressure: pressures[kind] || 0.3,
					jitter: kind === "hatch" ? 0.0028 : 0.0014,
				};

				// strength modulates lightly — do NOT amplify dark regions hard
				base.pressure = Math.min(
					variant === "E" ? 0.42 : 0.48,
					base.pressure * (0.85 + 0.35 * strength),
				);

				if (region === "eyes") {
					base.size = Math.min(base.size, variant === "E" ? 1.15 : 1.25);
					base.pressure = Math.min(base.pressure, variant === "E" ? 0.36 : 0.4);
				}
				if (region === "nose") {
					base.size = Math.min(base.size, 1.1);
					base.pressure = Math.min(base.pressure, 0.26);
				}
				if (region === "mouth") {
					base.size = Math.min(base.size, 1.25);
				}
				if (kind === "outline") {
					// incomplete contour feel — not a full felt-tip ring
					base.jitter = 0.0018;
				}
				if (kind === "hatch") {
					base.pressure = Math.min(0.34, 0.18 + strength);
					base.jitter = 0.0032;
					base.size = Math.max(base.size, variant === "E" ? 1.1 : 1.2);
				}

				const densifyStep = playback.densify;
				let passes = 1;
				if (s.allowSecondPass && (variant === "D" || variant === "E")) {
					// E: even rarer second pass
					if (variant === "D" || (variant === "E" && (region === "eyes" || region === "mouth"))) {
						passes = 2;
					}
				}
				return {
					...base,
					densifyStep,
					passes,
					incompleteness: kind === "outline" ? (variant === "E" ? 0.18 : 0.12) : kind === "hatch" ? 0.14 : 0.04,
				};
			}

			const sorted = [...strokes].sort((a, b) => {
				const ka = ORDER.indexOf(a.kind);
				const kb = ORDER.indexOf(b.kind);
				if (ka !== kb) return ka - kb;
				// features before hair; prefer mouths/eyes slightly
				const pr = (s) =>
					({ eyes: 5, mouth: 4, jaw: 3, silhouette: 2, nose: 1, hair: 0, tone: -1, form: 0 }[s.region] || 0);
				if (pr(b) !== pr(a)) return pr(b) - pr(a);
				return (b.strength || 0) - (a.strength || 0);
			});

			for (const s of sorted) {
				const c = cfg(s);
				driver.setBrush("sketchy", c.size, "#1c1c1c");
				await new Promise((r) => setTimeout(r, 2));
				const pts = s.points;
				for (let pass = 0; pass < c.passes; pass++) {
					if (pass === 1) secondPassCount += 1;
					// light second pass: shorter / incomplete, not full redraw
					const usePts =
						pass === 1 && pts.length > 10
							? pts.slice(0, Math.max(6, Math.floor(pts.length * 0.65)))
							: pts;
					driver.strokeNorm(usePts, {
						pressure: c.pressure * (pass === 0 ? 1 : 0.55),
						jitter: c.jitter + pass * 0.0007,
						incompleteness: c.incompleteness + (pass === 1 ? 0.08 : 0),
						densifyStep: c.densifyStep,
					});
					await new Promise((r) => setTimeout(r, 2 + Math.random() * 3));
				}
			}
			return secondPassCount;
		},
		strokes,
		variant,
		playback,
	);
	return secondPasses;
}

async function runVariant(browser, v) {
	const plan = JSON.parse(await readFile(path.join(OUT_DIR, v.plan), "utf8"));
	const strokes = [...(plan.structure || []), ...(plan.hatch || [])];
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
	await new Promise((r) => setTimeout(r, 300));

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

	await page.evaluate(async () => {
		await window.__portraitDriver.clearDrawing();
	});
	const secondPasses = await drawStrokes(page, strokes, v.key, plan.playback);
	const file = await exportPng(page, OUT_DIR, v.out);
	await page.close();

	return {
		variant: v.key,
		file,
		stats: plan.stats,
		playback: plan.playback,
		secondPassesExecuted: secondPasses,
		pointerEvents: true,
		basedOn: plan.basedOn,
	};
}

async function main() {
	await mkdir(OUT_DIR, { recursive: true });
	await ensureServer();
	const browser = await puppeteer.launch({
		executablePath: CHROME,
		headless: "new",
		args: ["--no-sandbox", "--window-size=1100,1400"],
		defaultViewport: { width: 1000, height: 1300, deviceScaleFactor: 1 },
	});

	const results = [];
	for (const v of VARIANTS) {
		console.log("plotting", v.key);
		results.push(await runVariant(browser, v));
	}
	await browser.close();

	const report = {
		philosophy: "B is the base; D/E improve stroke planning only",
		frozenB: "tmp/soft-sketchy-frozen/portrait-soft-sketchy-b-frozen.png",
		frozenC: "tmp/soft-sketchy-frozen/portrait-soft-sketchy-c-hatch-frozen.png",
		results,
	};
	await writeFile(path.join(OUT_DIR, "SOFT_SKETCHY_DE_REPORT.json"), JSON.stringify(report, null, 2));
	console.log(JSON.stringify(report, null, 2));
}

main().catch((err) => {
	console.error(err);
	process.exit(1);
});
