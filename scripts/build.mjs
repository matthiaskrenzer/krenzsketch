import { cp, mkdir, readdir, readFile, rm, writeFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const dist = path.join(root, "dist");
const src = path.join(root, "src");

process.env.COPYFILE_DISABLE = "1";

await rm(dist, { recursive: true, force: true });
await mkdir(dist, { recursive: true });
await cp(src, dist, { recursive: true });
await stripJunk(dist);

const icons = spawnSync(process.execPath, [path.join(root, "scripts/generate-icons.mjs"), dist], {
	stdio: "inherit",
});

if (icons.status !== 0) {
	process.exit(icons.status ?? 1);
}

const buildId = Date.now().toString(36);
const swPath = path.join(dist, "sw.js");
let sw = await readFile(swPath, "utf8");
sw = sw.replace("__BUILD_ID__", buildId);
await writeFile(swPath, sw, "utf8");

await stripJunk(dist);
console.log(`Build complete: dist/  (build ${buildId})`);

async function stripJunk(dir) {
	const entries = await readdir(dir, { withFileTypes: true });
	for (const entry of entries) {
		const target = path.join(dir, entry.name);
		if (entry.name.startsWith("._") || entry.name === ".DS_Store") {
			await rm(target, { force: true });
			continue;
		}
		if (entry.isDirectory()) {
			await stripJunk(target);
		}
	}
}
