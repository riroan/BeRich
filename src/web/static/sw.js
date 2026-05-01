/* BeRich Service Worker
 * Strategy:
 *   - Static assets (/static/*, manifest, icons)  → cache-first
 *   - HTML navigations                            → network-first, offline fallback
 *   - /api/*, /ws, websockets                     → bypass (always network)
 *   - /login, /logout                             → bypass (auth-sensitive)
 */
const CACHE_VERSION = "berich-v2";
const STATIC_CACHE = `${CACHE_VERSION}-static`;
const HTML_CACHE = `${CACHE_VERSION}-html`;

const PRECACHE_URLS = [
    "/static/style.css",
    "/static/script.js?v=2",
    "/static/lightweight-charts.js",
    "/static/icons/icon-192.png",
    "/static/icons/icon-512.png",
    "/static/icons/icon-maskable-512.png",
    "/static/icons/apple-touch-icon-180.png",
    "/static/manifest.webmanifest",
    "/static/offline.html",
];

self.addEventListener("install", (event) => {
    event.waitUntil(
        caches.open(STATIC_CACHE).then((cache) =>
            // Use addAll with allSettled-style fallback so one 404 doesn't kill install
            Promise.all(
                PRECACHE_URLS.map((url) =>
                    cache.add(url).catch((err) =>
                        console.warn("[sw] precache skip", url, err.message)
                    )
                )
            )
        )
    );
    self.skipWaiting();
});

self.addEventListener("activate", (event) => {
    event.waitUntil(
        caches.keys().then((keys) =>
            Promise.all(
                keys
                    .filter((k) => !k.startsWith(CACHE_VERSION))
                    .map((k) => caches.delete(k))
            )
        )
    );
    self.clients.claim();
});

function isBypassed(url) {
    return (
        url.pathname.startsWith("/api/") ||
        url.pathname.startsWith("/ws") ||
        url.pathname === "/login" ||
        url.pathname === "/logout"
    );
}

self.addEventListener("fetch", (event) => {
    const req = event.request;
    if (req.method !== "GET") return;

    const url = new URL(req.url);
    if (url.origin !== self.location.origin) return;
    if (isBypassed(url)) return; // let the network handle it

    // Static assets: cache-first
    if (url.pathname.startsWith("/static/")) {
        event.respondWith(
            caches.match(req).then((hit) => {
                if (hit) return hit;
                return fetch(req).then((resp) => {
                    if (resp.ok) {
                        const clone = resp.clone();
                        caches.open(STATIC_CACHE).then((c) => c.put(req, clone));
                    }
                    return resp;
                });
            })
        );
        return;
    }

    // HTML navigations: network-first, fall back to last good HTML, then offline page
    if (req.mode === "navigate" || req.headers.get("accept")?.includes("text/html")) {
        event.respondWith(
            fetch(req)
                .then((resp) => {
                    if (resp.ok) {
                        const clone = resp.clone();
                        caches.open(HTML_CACHE).then((c) => c.put(req, clone));
                    }
                    return resp;
                })
                .catch(() =>
                    caches.match(req).then((hit) => hit || caches.match("/static/offline.html"))
                )
        );
    }
});
