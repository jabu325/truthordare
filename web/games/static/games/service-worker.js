const CACHE_NAME = "truth-dare-arcade-v16";
const OFFLINE_URL = "/static/games/offline.html";
const APP_ASSETS = [
    OFFLINE_URL,
    "/static/games/style.css",
    "/static/games/manifest.webmanifest",
    "/static/games/icons/truth-or-dare-logo.jpg",
    "/static/games/icons/icon-192.png",
    "/static/games/icons/icon-512.png",
    "/static/games/pwa.js",
    "/static/games/invite.js",
    "/static/games/conversation.js",
    "/static/games/turn-alert.js",
    "/static/games/presence.js",
    "/static/games/proof.js",
    "/static/games/room-navigation.js",
];

self.addEventListener("install", (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME)
            .then((cache) => cache.addAll(APP_ASSETS))
            .then(() => self.skipWaiting())
    );
});

self.addEventListener("activate", (event) => {
    event.waitUntil(
        caches.keys()
            .then((keys) => Promise.all(
                keys
                    .filter((key) => key.startsWith("truth-dare-") && key !== CACHE_NAME)
                    .map((key) => caches.delete(key))
            ))
            .then(() => self.clients.claim())
    );
});

self.addEventListener("fetch", (event) => {
    const requestUrl = new URL(event.request.url);
    if (event.request.method !== "GET" || requestUrl.origin !== self.location.origin) {
        return;
    }

    if (requestUrl.pathname.startsWith("/static/")) {
        event.respondWith(
            caches.match(event.request).then((cached) => cached || fetch(event.request))
        );
        return;
    }

    if (event.request.mode === "navigate") {
        event.respondWith(
            fetch(event.request).catch(() => caches.match(OFFLINE_URL))
        );
    }
});
