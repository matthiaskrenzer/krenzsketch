const CACHE = "krenzsketch-__BUILD_ID__";
const CACHE_PREFIX = "krenzsketch-";
const ASSETS = [
	"./",
	"./index.html",
	"./about.html",
	"./css/app.css",
	"./css/legal.css",
	"./js/app.js",
	"./js/brushes.js",
	"./js/persist.js",
	"./manifest.webmanifest",
	"./icons/icon-192.png",
	"./icons/icon-512.png",
	"./icons/icon-maskable-512.png",
	"./apple-touch-icon.png",
	"./favicon.svg",
];

self.addEventListener("install", (event) => {
	event.waitUntil(
		caches.open(CACHE).then((cache) => cache.addAll(ASSETS)),
	);
});

self.addEventListener("activate", (event) => {
	event.waitUntil(
		caches
			.keys()
			.then((keys) =>
				Promise.all(
					keys
						.filter((key) => key.startsWith(CACHE_PREFIX) && key !== CACHE)
						.map((key) => caches.delete(key)),
				),
			)
			.then(() => self.clients.claim()),
	);
});

self.addEventListener("message", (event) => {
	if (event.data === "SKIP_WAITING" || event.data === "skipWaiting") {
		self.skipWaiting();
	}
});

self.addEventListener("fetch", (event) => {
	if (event.request.method !== "GET") return;

	const url = new URL(event.request.url);
	if (url.origin !== self.location.origin) return;
	if (url.pathname === "/sw.js" || url.pathname.endsWith("/sw.js")) return;

	const isNavigation = event.request.mode === "navigate"
		|| event.request.destination === "document"
		|| (event.request.headers.get("accept") || "").includes("text/html");

	if (isNavigation) {
		event.respondWith(
			fetch(event.request)
				.then((response) => {
					if (!response || response.status !== 200 || response.type !== "basic") {
						return response;
					}
					const copy = response.clone();
					caches.open(CACHE).then((cache) => cache.put(event.request, copy));
					return response;
				})
				.catch(async () => {
					const cached = await caches.match(event.request);
					return cached || caches.match("./index.html");
				}),
		);
		return;
	}

	event.respondWith(
		caches.match(event.request).then((cached) => {
			if (cached) return cached;
			return fetch(event.request).then((response) => {
				if (!response || response.status !== 200 || response.type !== "basic") {
					return response;
				}
				const copy = response.clone();
				caches.open(CACHE).then((cache) => cache.put(event.request, copy));
				return response;
			});
		}),
	);
});
