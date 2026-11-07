import test from "node:test";
import assert from "node:assert/strict";

const source = await import("../src/lib/offlineUrls.mjs");

const BOOK = "F:\\learning\\py-translate-book\\86 - Volume 11 [Yen Press][Kobo].epub";

test("docUrl matches the reader page's iframe URL byte for byte", () => {
  const expected = `/v2/doc?${new URLSearchParams({ path: BOOK, n: "3" }).toString()}`;
  assert.equal(source.docUrl(BOOK, 3), expected);
});

test("readerApiUrl matches api.ts's readerQuery serialization", () => {
  assert.equal(
    source.readerApiUrl("/api/reader/meta", BOOK),
    `/api/reader/meta?${new URLSearchParams({ path: BOOK }).toString()}`,
  );
  assert.equal(
    source.readerApiUrl("/api/v2/reader/chapter", BOOK, { n: "7" }),
    `/api/v2/reader/chapter?${new URLSearchParams({ path: BOOK, n: "7" }).toString()}`,
  );
});

test("coverUrl matches LibraryPage's encodeURIComponent serialization", () => {
  assert.equal(
    source.coverUrl(BOOK),
    `/api/v2/books/cover?path=${encodeURIComponent(BOOK)}`,
  );
});

test("extractResUrls decodes &amp;, handles srcset and drops duplicates", () => {
  const enc = encodeURIComponent(BOOK);
  const html = [
    `<link href="/api/reader/res?path=${enc}&amp;ref=OEBPS/Styles/main.css"/>`,
    `<img src="/api/reader/res?path=${enc}&amp;ref=OEBPS/Images/p.png"/>`,
    `<img srcset="/api/reader/res?path=${enc}&amp;ref=OEBPS/Images/p.png 1x, /api/reader/res?path=${enc}&amp;ref=OEBPS/Images/big.png 2x"/>`,
    `<img src="/api/reader/res?path=${enc}&amp;ref=OEBPS/Images/p.png"/>`,
  ].join("\n");
  const urls = source.extractResUrls(html);
  assert.equal(urls.length, 3, "the picture appears twice but is fetched once");
  assert.ok(urls.every((url) => !url.includes("&amp;")), "entity escapes are decoded");
  assert.ok(urls.some((url) => url.endsWith("ref=OEBPS/Styles/main.css")));
  assert.ok(urls.some((url) => url.endsWith("ref=OEBPS/Images/big.png")));
});

test("extractCssResUrls finds url() and @import targets", () => {
  const css = `
    @font-face { src: url(/api/reader/res?ref=OEBPS/Fonts/x.ttf); }
    body { background: url('/api/reader/res?ref=OEBPS/Images/orn.jpg'); }
    @import "/api/reader/res?ref=OEBPS/Styles/extra.css";
  `;
  assert.deepEqual(source.extractCssResUrls(css).sort(), [
    "/api/reader/res?ref=OEBPS/Fonts/x.ttf",
    "/api/reader/res?ref=OEBPS/Images/orn.jpg",
    "/api/reader/res?ref=OEBPS/Styles/extra.css",
  ].sort());
});

test("extractCssResUrls ignores data: and fragment urls", () => {
  assert.deepEqual(source.extractCssResUrls("a{b:url(data:image/png;base64,xx)}c{d:url(#x)}"), []);
});

test("withPath appends path only when the server's rewrite dropped it", () => {
  const bare = "/api/reader/res?ref=OEBPS/Fonts/x.ttf";
  assert.equal(source.withPath(bare, BOOK), `${bare}&path=${encodeURIComponent(BOOK)}`);
  const complete = `/api/reader/res?path=${encodeURIComponent(BOOK)}&ref=OEBPS/Fonts/x.ttf`;
  assert.equal(source.withPath(complete, BOOK), complete);
});

test("urlBelongsToBook recognises every encoding the app uses", () => {
  // server (urllib.parse.quote): %20 for spaces
  const serverStyle = `/api/reader/res?path=${encodeURIComponent(BOOK)}&ref=x.png`;
  // api.ts / reader page (URLSearchParams): + for spaces
  const paramsStyle = `/api/reader/meta?${new URLSearchParams({ path: BOOK })}`;
  assert.equal(source.urlBelongsToBook(serverStyle, BOOK), true);
  assert.equal(source.urlBelongsToBook(paramsStyle, BOOK), true);
  // pathless stylesheet refs are matched through the recorded URL list instead
  assert.equal(source.urlBelongsToBook("/api/reader/res?ref=OEBPS/Fonts/x.ttf", BOOK), false);
  assert.equal(
    source.urlBelongsToBook(`/api/reader/meta?path=${encodeURIComponent("other.epub")}`, BOOK),
    false,
  );
});
