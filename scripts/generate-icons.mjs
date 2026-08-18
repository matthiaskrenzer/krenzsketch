import fs from "node:fs";
import path from "node:path";
import zlib from "node:zlib";
import { fileURLToPath } from "node:url";

const targetDir = process.argv[2]
	? path.resolve(process.argv[2])
	: path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "src");

fs.mkdirSync(path.join(targetDir, "icons"), { recursive: true });

function crc32(buf) {
	let crc = ~0;
	for (let i = 0; i < buf.length; i++) {
		crc ^= buf[i];
		for (let j = 0; j < 8; j++) {
			crc = crc & 1 ? (crc >>> 1) ^ 0xedb88320 : crc >>> 1;
		}
	}
	return ~crc >>> 0;
}

function chunk(type, data) {
	const typeBuf = Buffer.from(type);
	const len = Buffer.alloc(4);
	len.writeUInt32BE(data.length);
	const crcBuf = Buffer.alloc(4);
	crcBuf.writeUInt32BE(crc32(Buffer.concat([typeBuf, data])));
	return Buffer.concat([len, typeBuf, data, crcBuf]);
}

function writePng(filePath, size, paint) {
	const raw = Buffer.alloc((size * 4 + 1) * size);
	for (let y = 0; y < size; y++) {
		const row = y * (size * 4 + 1);
		raw[row] = 0;
		for (let x = 0; x < size; x++) {
			const [r, g, b, a] = paint(x, y, size);
			const i = row + 1 + x * 4;
			raw[i] = r;
			raw[i + 1] = g;
			raw[i + 2] = b;
			raw[i + 3] = a;
		}
	}

	const ihdr = Buffer.alloc(13);
	ihdr.writeUInt32BE(size, 0);
	ihdr.writeUInt32BE(size, 4);
	ihdr[8] = 8;
	ihdr[9] = 6;

	const png = Buffer.concat([
		Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]),
		chunk("IHDR", ihdr),
		chunk("IDAT", zlib.deflateSync(raw)),
		chunk("IEND", Buffer.alloc(0)),
	]);

	fs.writeFileSync(filePath, png);
}

function distToSegment(px, py, ax, ay, bx, by) {
	const abx = bx - ax;
	const aby = by - ay;
	const apx = px - ax;
	const apy = py - ay;
	const ab2 = abx * abx + aby * aby || 1;
	let t = (apx * abx + apy * aby) / ab2;
	t = Math.max(0, Math.min(1, t));
	const dx = px - (ax + abx * t);
	const dy = py - (ay + aby * t);
	return Math.hypot(dx, dy);
}

function makePainter(maskable) {
	const bg = [18, 18, 18, 255];
	const ink = [232, 226, 214, 255];
	const mute = [176, 168, 152, 180];

	const pathA = [
		[0.28, 0.7],
		[0.34, 0.42],
		[0.46, 0.3],
		[0.62, 0.34],
		[0.7, 0.5],
		[0.58, 0.64],
		[0.42, 0.58],
		[0.4, 0.44],
	];
	const pathB = [
		[0.32, 0.62],
		[0.5, 0.38],
		[0.66, 0.46],
	];

	return (x, y, size) => {
		const nx = (x + 0.5) / size;
		const ny = (y + 0.5) / size;
		const pad = maskable ? 0.18 : 0.08;
		const inset = nx < pad || ny < pad || nx > 1 - pad || ny > 1 - pad;

		if (maskable && inset) return bg;

		let d = Infinity;
		let d2 = Infinity;
		const pts = pathA.map(([px, py]) => [px * size, py * size]);
		const pts2 = pathB.map(([px, py]) => [px * size, py * size]);
		for (let i = 1; i < pts.length; i++) {
			d = Math.min(d, distToSegment(x, y, pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1]));
		}
		for (let i = 1; i < pts2.length; i++) {
			d2 = Math.min(d2, distToSegment(x, y, pts2[i - 1][0], pts2[i - 1][1], pts2[i][0], pts2[i][1]));
		}

		const w = size * 0.028;
		if (d < w) return ink;
		if (d2 < w * 0.65) return mute;
		if (d < w * 2.4 && (x + y) % 7 === 0) return [210, 204, 190, 70];
		return bg;
	};
}

writePng(path.join(targetDir, "icons", "icon-192.png"), 192, makePainter(false));
writePng(path.join(targetDir, "icons", "icon-512.png"), 512, makePainter(false));
writePng(path.join(targetDir, "icons", "icon-maskable-512.png"), 512, makePainter(true));
writePng(path.join(targetDir, "apple-touch-icon.png"), 180, makePainter(false));

const svg = `<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
  <rect width="32" height="32" fill="#121212"/>
  <path d="M9 22 C11 13 15 9 20 11 C24 13 24 18 20 21 C16 23 13 20 13 16"
        fill="none" stroke="#e8e2d6" stroke-width="1.6" stroke-linecap="round"/>
</svg>
`;
fs.writeFileSync(path.join(targetDir, "favicon.svg"), svg);
console.log("Icons written to", targetDir);
