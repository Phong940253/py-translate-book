/* WebUI v2 PWA: offline shell, visited chapters and downloaded books.
   Scope is /v2/ because the service worker itself is served from that path.

   - build assets      -> cache-first (Vite filenames are content-hashed)
   - reader/library    -> stale-while-revalidate, offline copy from the book cache
   - doc/state/ann     -> network first: they must be current while online and
                          fall back to the downloaded book when offline
   - state writes      -> network; a failed save is stashed in the book cache so
                          the next GET resumes the reading position offline
   - other APIs        -> network; polite JSON 503 while offline
   - navigation        -> network first, cached shell while offline

   pytb-v2-offline holds whole books downloaded from the library page. It is
   never evicted by activate() nor by the KEEP budget — books leave only
   through the library's offline manager. */

const CACHE = "pytb-v2-v2";
const OFFLINE = "pytb-v2-offline";
const KEEP = 160;
const ASSET_RX = /\/v2\/(assets\/|icon-|favicon|cover-placeholder)/;
const CONTENT_RX = /\/api\/reader\/(meta|toc|doc|chapter|image|res)\?|\/api\/v2\/(books\/preview|books\/cover|reader\/chapter)\?|\/books\/cover\?|\/api\/library(\?|$)/;

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
      .then((keys) => Promise.all(
        keys.filter((key) => key !== CACHE && key !== OFFLINE).map((key) => caches.delete(key)),
      ))
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

async function matchOffline(request) {
  try {
    return await (await caches.open(OFFLINE)).match(request);
  } catch {
    return undefined;
  }
}

function offlineJson() {
  return new Response(
    JSON.stringify({ error: "Mất kết nối máy chủ (đang ngoại tuyến)." }),
    { status: 503, headers: { "Content-Type": "application/json; charset=utf-8" } },
  );
}

function offlineHtml() {
  return new Response(
    "<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>Offline</title></head>" +
    "<body style=\"font:15px/1.6 system-ui,sans-serif;margin:3rem auto;max-width:28rem;text-align:center\">" +
    "<p>Đang ngoại tuyến — nội dung này chưa được tải về.</p>" +
    "<p style=\"color:#888\">Offline — this content has not been downloaded.</p></body></html>",
    { status: 503, headers: { "Content-Type": "text/html; charset=utf-8" } },
  );
}

/* Network first, downloaded book as the fallback. Chapter documents, reading
   position and annotations must never be served stale while the server is
   reachable, yet have to survive an offline reading session. */
async function fresh(request, shell) {
  try {
    const response = await fetch(request);
    if (response.ok) {
      const copy = response.clone();
      const job = caches.open(shell ? CACHE : OFFLINE).then((cache) =>
        shell ? put(cache, request, copy) : cache.put(request, copy));
      job.catch(() => { /* quota: the response is still valid */ });
    }
    return response;
  } catch {
    const offline = await matchOffline(request);
    if (offline) return offline;
    if (shell) {
      const hit = await (await caches.open(CACHE)).match(request);
      if (hit) return hit;
    }
    return request.mode === "navigate" ? offlineHtml() : offlineJson();
  }
}

/* A position saved while offline is kept in the book cache under the GET URL
   so the next read resumes where the reader left off. */
async function stashStateWrite(request) {
  let stash = null;
  try {
    stash = request.clone();
  } catch { /* body unusable: fall through to plain network */ }
  try {
    return await fetch(request);
  } catch {
    try {
      const payload = await stash.json();
      const key = `/api/reader/state?${new URLSearchParams({ path: String(payload?.path ?? "") })}`;
      const cache = await caches.open(OFFLINE);
      await cache.put(key, new Response(JSON.stringify(payload?.state ?? {}), {
        headers: { "Content-Type": "application/json; charset=utf-8" },
      }));
      return new Response(JSON.stringify({ ok: true, offline: true }), {
        status: 200,
        headers: { "Content-Type": "application/json; charset=utf-8" },
      });
    } catch {
      return offlineJson();
    }
  }
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (url.pathname === "/api/reader/state" || url.pathname === "/api/reader/annotations") {
    if (request.method === "GET") {
      event.respondWith(fresh(request, false));
      return;
    }
    if (request.method === "PUT" && url.pathname === "/api/reader/state") {
      event.respondWith(stashStateWrite(request));
      return;
    }
    return; // annotation writes go straight to the network
  }

  if (request.method !== "GET") return;
  if (url.pathname.startsWith("/api/tts") || url.pathname.startsWith("/api/music") || url.pathname.startsWith("/api/v2/music")) return;

  if (ASSET_RX.test(url.pathname)) {
    event.respondWith(
      caches.open(CACHE).then((cache) =>
        cache.match(request).then((hit) => hit || fetch(request).then((response) => put(cache, request, response)))
      )
    );
    return;
  }

  // Chapter documents: the reader iframe navigates here (inside the /v2 scope,
  // so this worker controls the document and every res? subresource it loads).
  if (url.pathname === "/v2/doc") {
    event.respondWith(fresh(request, true));
    return;
  }

  if (CONTENT_RX.test(request.url)) {
    event.respondWith(
      caches.open(CACHE).then((cache) =>
        cache.match(request).then((hit) => {
          const network = fetch(request)
            .then((response) => put(cache, request, response))
            .catch(async () => (await matchOffline(request)) || hit || offlineJson());
          return hit || network;
        })
      )
    );
    return;
  }

  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).catch(async () => (await caches.match("/v2/")) || offlineHtml())
    );
    return;
  }

  // Search, lookup, TTS… need the server: answer politely so the UI can show
  // its own "needs a connection" message instead of a dead fetch.
  event.respondWith(
    fetch(request).catch(async () => (await caches.match(request)) || offlineJson())
  );
});
