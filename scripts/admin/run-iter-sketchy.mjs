/**
 * Render iterative stroke plans with real Sketchy PointerEvents.
 * feature/photo-sketch only. No source/brush changes.
 *
 *   node scripts/admin/run-iter-sketchy.mjs v3
 */
import puppeteer from "puppeteer-core";
import { spawn } from "node:child_process";
import { mkdir, readFile, writeFile, access, unlink } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const ADMIN_PORT = Number(process.env.ADMIN_PORT) || 4174;
const BASE = `http://127.0.0.1:${ADMIN_PORT}`;
const CHROME =
	process.env.CHROME_PATH ||
	"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const VARIANT = process.argv[2] || "v3";
const OUT = path.join(ROOT, "tmp", "iter-stroke", VARIANT);
const MOTIFS = ["duo-council", "painter-studio", "heron-pond"];

async function exists(p) {
	try {
		await access(p);
		return true;
	} catch {
		return false;
	}
}

async function waitForServer(url, ms = 25000) {
	const start = Date.now();
	while (Date.now() - start < ms) {
		try {
			const r = await fetch(url);
			if (r.ok || r.status === 404) return;
		} catch {
			/* */
		}
		await new Promise((r) => setTimeout(r, 200));
	}
	throw new Error(`server missing ${url}`);
}

async function ensureServer() {
	try {
		const r = await fetch(`${BASE}/`);
		if (r.ok) return;
	} catch {
		/* */
	}
	const child = spawn(process.execPath, [path.join(ROOT, "scripts/admin-server.mjs")], {
		cwd: ROOT,
		stdio: "ignore",
		detached: true,
	});
	child.unref();
	await waitForServer(`${BASE}/`);
}

async function exportPng(page, filePath) {
	const b64 = await page.evaluate(async () => {
		const blob = await window.__portraitDriver.exportPngBlob();
		const buf = await blob.arrayBuffer();
		const bytes = new Uint8Array(buf);
		let binary = "";
		for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
		return btoa(binary);
	});
	if (await exists(filePath)) await unlink(filePath);
	await writeFile(filePath, Buffer.from(b64, "base64"));
	return filePath;
}

async function draw(page, strokes, playback) {
	return page.evaluate(
		async (strokes, playback) => {
			const driver = window.__portraitDriver;
			// hatch first (under), then structure — lighter overall
			const ORDER = ["hatch", "feature", "other", "hair", "outline"];
			let second = 0;
			const sorted = [...strokes].sort((a, b) => {
				const ka = ORDER.indexOf(a.kind);
				const kb = ORDER.indexOf(b.kind);
				if (ka !== kb) return ka - kb;
				return (b.strength || 0) - (a.strength || 0);
			});
			for (const s of sorted) {
				const kind = s.kind;
				let size =
					kind === "outline"
						? playback.outlineSize
						: kind === "hatch"
							? playback.hatchSize
							: kind === "hair"
								? playback.hairSize
								: kind === "feature"
									? playback.featureSize
									: playback.otherSize;
				let pressure =
					kind === "outline"
						? playback.outlinePressure
						: kind === "hatch"
							? playback.hatchPressure
							: playback.featurePressure;
				// strength modulates lightly — never amplify darks hard
				pressure = Math.min(0.42, pressure * (0.88 + 0.28 * (s.strength || 0.3)));
				if (kind === "hatch") pressure = Math.min(0.28, pressure);
				if ((s.localTone || 0) > 0.7) pressure *= 0.75;
				const incompleteness = kind === "outline" ? 0.16 : kind === "hatch" ? 0.12 : 0.04;
				let passes = 1;
				if (s.allowSecondPass) passes = 2;
				driver.setBrush("sketchy", size, "#1c1c1c");
				await new Promise((r) => setTimeout(r, 2));
				for (let pass = 0; pass < passes; pass++) {
					if (pass === 1) second += 1;
					const pts =
						pass === 1 && s.points.length > 10
							? s.points.slice(0, Math.max(5, Math.floor(s.points.length * 0.55)))
							: s.points;
					driver.strokeNorm(pts, {
						pressure: pressure * (pass === 0 ? 1 : 0.5),
						jitter: kind === "hatch" ? 0.003 : 0.0015,
						incompleteness: incompleteness + (pass === 1 ? 0.1 : 0),
						densifyStep: playback.densify,
					});
					await new Promise((r) => setTimeout(r, 1 + Math.random() * 2));
				}
			}
			return second;
		},
		strokes,
		playback,
	);
}

async function runMotif(browser, motif) {
	const plan = JSON.parse(await readFile(path.join(OUT, `${motif}-plan.json`), "utf8"));
	const strokes = [...(plan.hatch || []), ...(plan.structure || [])];
	const page = await browser.newPage();
	await page.setViewport({ width: 1100, height: 1400, deviceScaleFactor: 1 });
	await page.goto(`${BASE}/`, { waitUntil: "networkidle0", timeout: 60000 });
	await page.waitForSelector("#canvas");
	await page.evaluate(async () => {
		if (!("serviceWorker" in navigator)) return;
		for (const r of await navigator.serviceWorker.getRegistrations()) await r.unregister();
	});
	await page.reload({ waitUntil: "networkidle0" });
	await page.waitForSelector("#canvas");
	await new Promise((r) => setTimeout(r, 250));

	const driverSrc = await readFile(path.join(ROOT, "admin/lib/stroke-driver.js"), "utf8");
	await page.evaluate(
		async (driverSrc, payload) => {
			const blob = new Blob([driverSrc], { type: "text/javascript" });
			const url = URL.createObjectURL(blob);
			const mod = await import(url);
			window.__portraitDriver = mod.createStrokeDriver(document);
			window.__portraitDriver.setPortraitSize(payload.w, payload.h);
			await window.__portraitDriver.clearDrawing();
		},
		driverSrc,
		{ w: plan.width, h: plan.height },
	);

	await page.evaluate(async () => await window.__portraitDriver.clearDrawing());
	const second = await draw(page, strokes, plan.playback);
	const outFile = path.join(OUT, `${motif}-sketchy.png`);
	await exportPng(page, outFile);
	await page.close();
	return { motif, file: outFile, stats: plan.stats, secondPasses: second };
}

async function main() {
	await mkdir(OUT, { recursive: true });
	await ensureServer();
	const browser = await puppeteer.launch({
		executablePath: CHROME,
		headless: "new",
		args: ["--no-sandbox", "--window-size=1200,1500"],
		defaultViewport: { width: 1100, height: 1400, deviceScaleFactor: 1 },
	});
	const results = [];
	for (const m of MOTIFS) {
		console.log("render", VARIANT, m);
		results.push(await runMotif(browser, m));
	}
	await browser.close();
	await writeFile(path.join(OUT, "render-report.json"), JSON.stringify({ variant: VARIANT, results }, null, 2));
	console.log(JSON.stringify({ variant: VARIANT, results }, null, 2));
}

main().catch((e) => {
	console.error(e);
	process.exit(1);
});
