/* Tessera service worker: caches the app shell so the PWA installs and reloads offline.
   API calls are never cached (always network); when offline the shell shows an offline notice. */
const SHELL_CACHE = "tessera-shell-v1";
const SHELL = ["/", "/index.html", "/manifest.webmanifest", "/icon.svg", "/offline.html"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== SHELL_CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.pathname.startsWith("/api/") || url.origin !== self.location.origin) return;
  if (event.request.mode === "navigate") {
    event.respondWith(fetch(event.request).catch(() => caches.match("/index.html").then((r) => r || caches.match("/offline.html"))));
    return;
  }
  event.respondWith(
    caches.match(event.request).then(
      (cached) =>
        cached ||
        fetch(event.request).then((response) => {
          if (response.ok && url.pathname.startsWith("/assets/")) {
            const copy = response.clone();
            caches.open(SHELL_CACHE).then((cache) => cache.put(event.request, copy));
          }
          return response;
        }),
    ),
  );
});
