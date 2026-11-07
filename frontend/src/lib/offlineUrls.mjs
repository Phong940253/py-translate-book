/* Pure URL helpers for the offline book downloader: extracting resource
   references out of served chapter HTML / stylesheets and building URLs that
   byte-match what the reader page requests (cache keys are exact strings).
   Dependency-free so node --test can exercise it directly. */

const RES_RX = /\/api\/reader\/res\?[^"'<>\s)]+/g;

/** Chapter document URL — must match ReaderPage's docUrl byte for byte. */
export function docUrl(path, n) {
  return `/v2/doc?${new URLSearchParams({ path, n: String(n) }).toString()}`;
}

/** JSON endpoint built like api.ts's readerQuery(path, extra). */
export function readerApiUrl(pathname, path, extra = {}) {
  return `${pathname}?${new URLSearchParams({ path, ...extra }).toString()}`;
}

/** Cover URL built like LibraryPage's coverUrl (encodeURIComponent, not URLSearchParams). */
export function coverUrl(path) {
  return `/api/v2/books/cover?path=${encodeURIComponent(path)}`;
}

/** Attribute/JS escapes inside served HTML: `&amp;` is a plain `&` once parsed. */
export function decodeHtmlUrl(url) {
  return url.replace(/&amp;/g, "&");
}

/** Every /api/reader/res?… reference in a chapter document. */
export function extractResUrls(html) {
  const out = new Set();
  for (const raw of String(html).match(RES_RX) || []) {
    out.add(decodeHtmlUrl(raw));
  }
  return [...out];
}

/** url(...) and @import "…" targets inside a stylesheet. The server rewrites
    these without ?path=, so the browser's request URL differs from the only
    URL the server can answer — both are handled by the downloader. */
export function extractCssResUrls(css) {
  const rx = /url\(\s*['"]?(\/api\/reader\/res\?[^'")\s]+)['"]?\s*\)|@import\s+['"](\/api\/reader\/res\?[^'"]+)['"]/g;
  const out = new Set();
  let match;
  while ((match = rx.exec(css))) {
    out.add(decodeHtmlUrl(match[1] || match[2]));
  }
  return [...out];
}

/** Append ?path= when the server's CSS rewrite dropped it (res?ref= 400s
    without it). Percent-encoding differences do not matter here: this value is
    only sent to the server, never used as a cache key. */
export function withPath(url, path) {
  if (/[?&]path=/.test(url)) return url;
  return `${url}${url.includes("?") ? "&" : "?"}path=${encodeURIComponent(path)}`;
}

/** Does a cache URL carry this book's path? (any of the three encodings used
    by the server, api.ts and LibraryPage) */
export function urlBelongsToBook(url, path) {
  const match = /[?&]path=([^&]*)/.exec(url);
  if (!match) return false;
  let value = match[1].replace(/\+/g, " ");
  try {
    value = decodeURIComponent(value);
  } catch {
    // keep the raw value
  }
  return value === path;
}
