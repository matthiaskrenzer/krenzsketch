const CACHE = "krenzsketch-v8";
const ASSETS = [
	"./",
	"./index.html",
	"./about.html",
	"./impressum.html",
	"./datenschutz.html",
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
		caches
			.open(CACHE)
			.then((cache) => cache.addAll(ASSETS))
			.then(() => self.skipWaiting()),
	);
});

self.addEventListener("activate", (event) => {
	event.waitUntil(
		caches
			.keys()
			.then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
			.then(() => self.clients.claim()),
	);
});

self.addEventListener("fetch", (event) => {
	if (event.request.method !== "GET") return;

	const url = new URL(event.request.url);
	if (url.origin !== self.location.origin) return;

	event.respondWith(
		caches.match(event.request).then((cached) => {
			if (cached) return cached;
			return fetch(event.request)
				.then((response) => {
					if (!response || response.status !== 200 || response.type !== "basic") {
						return response;
					}
					const copy = response.clone();
					caches.open(CACHE).then((cache) => cache.put(event.request, copy));
					return response;
				})
				.catch(() => caches.match("./index.html"));
		}),
	);
});
