/* WebUI v2 PWA: offline shell + visited reader chapters.
   Scope is /v2/ because the service worker itself is served from that path.

   - build assets      -> cache-first (Vite filenames are content-hashed)
   - reader/library    -> stale-while-revalidate
   - state/annotations/TTS/music writes -> network only
   - navigation        -> network first, cached shell while offline               */

const CACHE = "pytb-v2-v1";
const KEEP = 160;
const ASSET_RX = /\/v2\/(assets\/|icon-|favicon|cover-placeholder)/;
const CONTENT_RX = /\/api\/reader\/(meta|toc|doc|chapter|image)\?|\/api\/v2\/books\/preview\?|\/api\/v2\/books\/cover\?|\/books\/cover\?|\/api\/library\?/;

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => cache.addAll(["/v2/", "/v2/manifest.webmanifest"]))
      .then(() => self.skipWaiting())
      .catch(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

function put(cache, request, response) {
  if (!response || response.status !== 200 || response.type === "opaque") return response;
  const copy = response.clone();
  cache.put(request, copy)
    .then(() => cache.keys())
    .then((keys) => {
      if (keys.length > KEEP) cache.delete(keys[0]);
    })
    .catch(() => { /* quota: response is still valid */ });
  return response;
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/tts") || url.pathname.startsWith("/api/music") || url.pathname.startsWith("/api/v2/music")) return;

  if (ASSET_RX.test(url.pathname)) {
    event.respondWith(
      caches.open(CACHE).then((cache) =>
        cache.match(request).then((hit) => hit || fetch(request).then((response) => put(cache, request, response)))
      )
    );
    return;
  }

  if (CONTENT_RX.test(request.url)) {
    event.respondWith(
      caches.open(CACHE).then((cache) =>
        cache.match(request).then((hit) => {
          const network = fetch(request)
            .then((response) => put(cache, request, response))
            .catch(() => hit);
          return hit || network;
        })
      )
    );
    return;
  }

  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).catch(() => caches.match("/v2/"))
    );
    return;
  }

  event.respondWith(
    fetch(request).catch(() => caches.match(request))
  );
});
