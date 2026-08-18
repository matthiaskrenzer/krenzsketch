import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "dist");
const port = Number(process.env.PORT) || 4173;

const types = {
	".html": "text/html; charset=utf-8",
	".css": "text/css; charset=utf-8",
	".js": "text/javascript; charset=utf-8",
	".webmanifest": "application/manifest+json; charset=utf-8",
	".json": "application/json; charset=utf-8",
	".png": "image/png",
	".svg": "image/svg+xml",
	".ico": "image/x-icon",
	".txt": "text/plain; charset=utf-8",
};

if (!fs.existsSync(root)) {
	console.error("dist/ fehlt. Bitte zuerst `npm run build` ausführen.");
	process.exit(1);
}

const server = http.createServer((req, res) => {
	const urlPath = decodeURIComponent((req.url ?? "/").split("?")[0]);
	let filePath = path.join(root, urlPath === "/" ? "index.html" : urlPath);

	if (!filePath.startsWith(root)) {
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
			headers["Cache-Control"] = "no-cache";
			headers["Service-Worker-Allowed"] = "/";
		}
		res.writeHead(200, headers).end(data);
	});
});

server.listen(port, "127.0.0.1", () => {
	console.log(`KrenzSketch preview: http://127.0.0.1:${port}/`);
});
