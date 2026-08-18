import { cp, mkdir, readdir, rm } from "node:fs/promises";
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

await stripJunk(dist);
console.log("Build complete: dist/");

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
