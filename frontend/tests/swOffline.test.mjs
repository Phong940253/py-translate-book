import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const sw = await readFile(new URL("../public/sw.js", import.meta.url), "utf8");

test("the downloaded-book cache survives activation and the LRU budget", () => {
  assert.match(sw, /const OFFLINE = "pytb-v2-offline"/);
  assert.match(sw, /key !== CACHE && key !== OFFLINE/, "activate() must keep book downloads");
  // LRU trimming only runs through put(), which the book cache never uses.
  assert.ok(!/OFFLINE\)\.then[\s\S]{0,120}KEEP/.test(sw));
});

test("the shell cache version changed so installed workers update", () => {
  assert.match(sw, /const CACHE = "pytb-v2-v2"/);
});

test("content matching covers every read endpoint the v2 reader uses", () => {
  assert.ok(sw.includes(String.raw`\/api\/reader\/(meta|toc|doc|chapter|image|res)\?`), "reader reads");
  assert.ok(sw.includes(String.raw`\/api\/v2\/(books\/preview|books\/cover|reader\/chapter)\?`), "v2 reads");
  assert.ok(sw.includes(String.raw`\/api\/library(\?|$)`), "library list without a query string");
});

test("chapter documents are network-first inside the /v2 scope", () => {
  assert.ok(sw.includes('url.pathname === "/v2/doc"'), "doc navigation must hit this worker");
  assert.match(sw, /async function fresh\(request, shell\)/);
});

test("reading position falls back to the book cache and stashes failed saves", () => {
  assert.ok(sw.includes('"/api/reader/state"'), "state endpoint branch");
  assert.ok(sw.includes("stashStateWrite"), "PUT while offline is kept locally");
  assert.match(sw, /request\.method === "GET"/);
});

test("uncached APIs answer a polite 503 instead of a dead fetch", () => {
  assert.match(sw, /status: 503/);
  assert.match(sw, /Mất kết nối máy chủ/);
});
