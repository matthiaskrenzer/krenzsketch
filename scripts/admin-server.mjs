/**
 * Local admin server: serves production dist/ + admin/ tools.
 * Not part of the PWA build. Does not modify production UI.
 *
 *   node scripts/admin-server.mjs
 *   → http://127.0.0.1:4174/
 *   → http://127.0.0.1:4174/admin/portrait-preprocess.html
 */
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const dist = path.join(root, "dist");
const admin = path.join(root, "admin");
const tmp = path.join(root, "tmp");
const port = Number(process.env.ADMIN_PORT) || 4174;

const types = {
	".html": "text/html; charset=utf-8",
	".css": "text/css; charset=utf-8",
	".js": "text/javascript; charset=utf-8",
	".mjs": "text/javascript; charset=utf-8",
	".json": "application/json; charset=utf-8",
	".png": "image/png",
	".svg": "image/svg+xml",
	".jpg": "image/jpeg",
	".jpeg": "image/jpeg",
	".webmanifest": "application/manifest+json; charset=utf-8",
};

function resolveFile(urlPath) {
	if (urlPath === "/" || urlPath === "") {
		return path.join(dist, "index.html");
	}
	if (urlPath.startsWith("/admin/")) {
		return path.join(admin, urlPath.slice("/admin/".length));
	}
	if (urlPath.startsWith("/tmp/")) {
		return path.join(tmp, urlPath.slice("/tmp/".length));
	}
	return path.join(dist, urlPath);
}

if (!fs.existsSync(dist)) {
	console.error("dist/ fehlt. Bitte zuerst `npm run build` ausführen.");
	process.exit(1);
}

const server = http.createServer((req, res) => {
	const urlPath = decodeURIComponent((req.url ?? "/").split("?")[0]);
	let filePath = resolveFile(urlPath);

	const allowedRoots = [dist, admin, tmp];
	if (!allowedRoots.some((r) => filePath.startsWith(r + path.sep) || filePath === r)) {
		res.writeHead(403).end("Forbidden");
		return;
	}

	if (fs.existsSync(filePath) && fs.statSync(filePath).isDirectory()) {
		filePath = path.join(filePath, "index.html");
	}

	fs.readFile(filePath, (err, data) => {
		if (err) {
			res.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" }).end("Not found");
			return;
		}
		const ext = path.extname(filePath);
		const headers = { "Content-Type": types[ext] ?? "application/octet-stream" };
		if (path.basename(filePath) === "sw.js") {
			headers["Cache-Control"] = "no-cache, no-store, must-revalidate";
			headers["Service-Worker-Allowed"] = "/";
		} else {
			headers["Cache-Control"] = "no-cache, must-revalidate";
		}
		res.writeHead(200, headers).end(data);
	});
});

server.listen(port, "127.0.0.1", () => {
	console.log(`KrenzSketch admin: http://127.0.0.1:${port}/`);
	console.log(`Preprocess UI:     http://127.0.0.1:${port}/admin/portrait-preprocess.html`);
});
