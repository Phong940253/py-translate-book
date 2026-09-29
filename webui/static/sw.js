/* PWA service worker: offline shell + visited-chapter cache.
   Served from /sw.js with Service-Worker-Allowed: / so the scope is the
   whole app. Strategy:
   - static assets      → cache-first (immutable build files)
   - reader content     → stale-while-revalidate (instant on revisit,
                          refreshed in the background)
   - state/annotations/search, POST/PUT, TTS audio → network only. */

var CACHE = 'pytb-v8';
var KEEP = 240;                       // runtime entries (chapter docs, images)
var ASSET_RX = /\/static\/|\/manifest\.webmanifest$|\/favicon/;
var CONTENT_RX = /\/api\/reader\/(meta|toc|doc|chapter|image)\?|\/books\/cover\?|\/api\/library\?/;

self.addEventListener('install', function (e) {
  e.waitUntil(
    caches.open(CACHE).then(function (c) {
      return c.addAll(['/', '/static/style.css', '/static/app.js',
        '/static/reader.css', '/static/reader.js']);
    }).then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener('activate', function (e) {
  e.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.map(function (k) {
        return k === CACHE ? null : caches.delete(k);
      }));
    }).then(function () { return self.clients.claim(); })
  );
});

function cachePut(cache, req, res) {
  if (!res || res.status !== 200 || res.type === 'opaque') return res;
  var copy = res.clone();
  cache.put(req, copy).then(function () {
    cache.keys().then(function (keys) {
      if (keys.length > KEEP) cache.delete(keys[0]);   // oldest first
    });
  }).catch(function () { /* quota — the response is still fine */ });
  return res;
}

self.addEventListener('fetch', function (e) {
  var req = e.request;
  if (req.method !== 'GET') return;                    // state/ann writes → net
  if (req.url.indexOf('/api/tts') >= 0) return;        // WAV blobs: never cache
  if (new URL(req.url).origin !== self.location.origin) return;

  if (ASSET_RX.test(new URL(req.url).pathname)) {
    e.respondWith(
      caches.open(CACHE).then(function (c) {
        return c.match(req).then(function (hit) {
          if (hit) return hit;
          return fetch(req).then(function (res) { return cachePut(c, req, res); });
        });
      })
    );
    return;
  }

  if (CONTENT_RX.test(req.url)) {
    e.respondWith(
      caches.open(CACHE).then(function (c) {
        return c.match(req).then(function (hit) {
          var net = fetch(req).then(function (res) { return cachePut(c, req, res); })
            .catch(function () { return hit; });        // offline → stale copy
          return hit || net;                            // cached? instant answer
        });
      })
    );
    return;
  }

  // pages & APIs: network first, cached copy only when offline
  e.respondWith(
    fetch(req).catch(function () {
      return caches.open(CACHE).then(function (c) {
        return c.match(req).then(function (hit) { return hit || c.match('/'); });
      });
    })
  );
});
