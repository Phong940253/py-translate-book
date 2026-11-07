/* Download whole books into the browser for offline reading (YouTube-style).

   The bytes live in the dedicated Cache API store `pytb-v2-offline` — never
   touched by the service worker's LRU budget — and an index in localStorage
   records which URLs belong to which book so removal is exact. The service
   worker (public/sw.js) falls back to that cache whenever the network is
   unreachable, which is what makes a downloaded chapter render offline.

   Pure URL parsing lives in offlineUrls.mjs so node --test can cover it. */

import { useEffect, useState, useSyncExternalStore } from "react";
import {
  coverUrl,
  docUrl,
  extractCssResUrls,
  extractResUrls,
  readerApiUrl,
  urlBelongsToBook,
  withPath,
} from "./offlineUrls.mjs";

export type OfflineBook = {
  path: string;
  name: string;
  bytes: number;
  files: number;
  urls: string[];
  complete: boolean;
  updatedAt: number;
};

export type OfflineProgress = {
  path: string;
  bytes: number;
  files: number;
  chaptersDone: number;
  chaptersTotal: number;
  pct: number;
};

export type DownloadResult = {
  ok: boolean;
  cancelled?: boolean;
  error?: string;
};

const INDEX_KEY = "v2.offline.books";
const CACHE_NAME = "pytb-v2-offline";
const CONCURRENCY = 5;

const listeners = new Set<() => void>();
const progress = new Map<string, OfflineProgress>();
const controllers = new Map<string, AbortController>();
let indexSnapshot: OfflineBook[] | null = null;
let progressSnapshot: Record<string, OfflineProgress> | null = null;

function notify(): void {
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function loadIndex(): OfflineBook[] {
  try {
    const raw = localStorage.getItem(INDEX_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function getOfflineSnapshot(): OfflineBook[] {
  if (!indexSnapshot) indexSnapshot = loadIndex();
  return indexSnapshot;
}

function getProgressSnapshot(): Record<string, OfflineProgress> {
  if (!progressSnapshot) progressSnapshot = Object.fromEntries(progress);
  return progressSnapshot;
}

function writeIndex(entries: OfflineBook[]): void {
  indexSnapshot = entries;
  try {
    localStorage.setItem(INDEX_KEY, JSON.stringify(entries));
  } catch {
    // quota / private mode: this session still sees the in-memory snapshot
  }
  notify();
}

function upsertBook(entry: OfflineBook): void {
  writeIndex(getOfflineSnapshot().filter((book) => book.path !== entry.path).concat(entry));
}

export function readOfflineIndex(): OfflineBook[] {
  return getOfflineSnapshot();
}

export function useOfflineBooks(): OfflineBook[] {
  return useSyncExternalStore(subscribe, getOfflineSnapshot, getOfflineSnapshot);
}

export function useOfflineProgress(): Record<string, OfflineProgress> {
  return useSyncExternalStore(subscribe, getProgressSnapshot, getProgressSnapshot);
}

export function useOnline(): boolean {
  const [online, setOnline] = useState(() =>
    typeof navigator === "undefined" ? true : navigator.onLine,
  );
  useEffect(() => {
    const goOnline = () => setOnline(true);
    const goOffline = () => setOnline(false);
    window.addEventListener("online", goOnline);
    window.addEventListener("offline", goOffline);
    return () => {
      window.removeEventListener("online", goOnline);
      window.removeEventListener("offline", goOffline);
    };
  }, []);
  return online;
}

export function cancelOfflineDownload(path: string): void {
  controllers.get(path)?.abort();
}

function abortError(): DOMException {
  return new DOMException("cancelled", "AbortError");
}

function toResult(error: unknown): DownloadResult {
  if (error && typeof error === "object" && (error as { name?: string }).name === "AbortError") {
    return { ok: false, cancelled: true };
  }
  return { ok: false, error: error instanceof Error ? error.message : String(error) };
}

/* Start (or resume) a download. Cached URLs are skipped, so an interrupted
   run continues where it stopped. Resolves — never rejects — with the outcome. */
export async function downloadBook(path: string, name: string): Promise<DownloadResult> {
  if (controllers.has(path)) return { ok: false, error: "already-downloading" };
  if (typeof caches === "undefined") return { ok: false, error: "cache-api-unavailable" };
  const controller = new AbortController();
  controllers.set(path, controller);
  try {
    return await runDownload(path, name, controller.signal);
  } catch (error) {
    const result = toResult(error);
    progress.delete(path);
    progressSnapshot = null;
    if (!getOfflineSnapshot().some((book) => book.path === path)) {
      upsertBook({
        path,
        name,
        bytes: 0,
        files: 0,
        urls: [],
        complete: false,
        updatedAt: Date.now(),
      });
    }
    return result;
  } finally {
    controllers.delete(path);
    if (progress.delete(path)) {
      progressSnapshot = null;
      notify();
    }
  }
}

async function runDownload(path: string, name: string, signal: AbortSignal): Promise<DownloadResult> {
  const cache = await caches.open(CACHE_NAME);
  const previous = getOfflineSnapshot().find((book) => book.path === path);
  const bookName = previous?.name || name;
  const urls = new Set<string>(previous?.urls ?? []);
  let bytes = previous?.bytes ?? 0;
  let files = previous?.files ?? 0;
  let chaptersDone = 0;
  let chaptersTotal = 0;
  let done = 0;
  let total = 1; // the meta request, counted up front
  let failure: unknown = null;
  let settled = false;

  const report = (): void => {
    progress.set(path, {
      path,
      bytes,
      files,
      chaptersDone,
      chaptersTotal,
      pct: Math.min(99, Math.round((done / Math.max(total, 1)) * 100)),
    });
    progressSnapshot = null;
    notify();
  };

  const finalize = (result: DownloadResult): void => {
    progress.delete(path);
    progressSnapshot = null;
    upsertBook({
      path,
      name: bookName,
      bytes,
      files,
      urls: [...urls],
      complete: result.ok,
      updatedAt: Date.now(),
    });
  };

  // Reserve the book in the index right away: a crash then leaves a record the
  // library page can offer to resume.
  upsertBook({
    path,
    name: bookName,
    bytes,
    files,
    urls: [...urls],
    complete: false,
    updatedAt: previous?.updatedAt ?? Date.now(),
  });

  const queue: Array<() => Promise<void>> = [];
  let active = 0;
  let resolveFinished!: (result: DownloadResult) => void;
  const finished = new Promise<DownloadResult>((resolve) => {
    resolveFinished = resolve;
  });

  const settle = (result: DownloadResult): void => {
    if (settled) return;
    settled = true;
    queue.length = 0;
    resolveFinished(result);
  };

  const pump = (): void => {
    if (settled) return;
    if (failure !== null) {
      if (active === 0) settle(toResult(failure));
      return;
    }
    while (active < CONCURRENCY && queue.length) {
      const job = queue.shift()!;
      active += 1;
      job()
        .catch((error: unknown) => {
          if (failure === null) failure = error;
        })
        .finally(() => {
          active -= 1;
          pump();
        });
    }
    if (active === 0 && queue.length === 0) settle({ ok: true });
  };

  const enqueue = (job: () => Promise<void>): void => {
    if (settled) return;
    queue.push(job);
    total += 1;
    pump();
  };

  const store = async (url: string, fetchUrl: string = url): Promise<void> => {
    if (signal.aborted) throw abortError();
    if (await cache.match(url)) {
      done += 1;
      report();
      return;
    }
    const response = await fetch(fetchUrl, { signal });
    if (!response.ok) {
      // dead reference inside the book: skip it, the rest still works
      done += 1;
      report();
      return;
    }
    const buffer = await response.arrayBuffer();
    await cache.put(url, new Response(buffer, { status: response.status, headers: response.headers }));
    bytes += buffer.byteLength;
    files += 1;
    urls.add(url);
    done += 1;
    report();
    const type = response.headers.get("content-type") || "";
    if (type.includes("text/css") || /\.css(?:$|\?)/i.test(url)) {
      // The server rewrites url(...) without ?path=, which answers 400 — fetch
      // it with the path added, but cache it under the URL the browser asks for.
      for (const inner of extractCssResUrls(new TextDecoder().decode(buffer))) {
        enqueue(() => store(inner, withPath(inner, path)));
      }
    }
  };

  const readText = async (url: string): Promise<string | null> => {
    if (signal.aborted) throw abortError();
    const stored = await cache.match(url);
    if (stored) {
      done += 1;
      report();
      return stored.text();
    }
    const response = await fetch(url, { signal });
    if (!response.ok) {
      done += 1;
      report();
      return null;
    }
    const buffer = await response.arrayBuffer();
    await cache.put(url, new Response(buffer, { status: response.status, headers: response.headers }));
    bytes += buffer.byteLength;
    files += 1;
    urls.add(url);
    done += 1;
    report();
    return new TextDecoder().decode(buffer);
  };

  const ensureChapter = async (n: number): Promise<void> => {
    const html = await readText(docUrl(path, n));
    chaptersDone += 1;
    await store(readerApiUrl("/api/v2/reader/chapter", path, { n: String(n) }));
    if (html) {
      for (const res of extractResUrls(html)) {
        enqueue(() => store(res));
      }
    }
    report();
  };

  try {
    // Fresh meta every run: the chapter count is the download's blueprint.
    const metaUrl = readerApiUrl("/api/reader/meta", path);
    const response = await fetch(metaUrl, { signal });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const buffer = await response.arrayBuffer();
    const alreadyHad = await cache.match(metaUrl);
    await cache.put(metaUrl, new Response(buffer, { status: response.status, headers: response.headers }));
    urls.add(metaUrl);
    if (!alreadyHad) {
      bytes += buffer.byteLength;
      files += 1;
    }
    done += 1;
    const meta = JSON.parse(new TextDecoder().decode(buffer)) as { total?: number };
    chaptersTotal = Number(meta.total) || 0;
    report();

    enqueue(() => store("/api/library"));
    enqueue(() => store(readerApiUrl("/api/reader/toc", path)));
    enqueue(() => store(readerApiUrl("/api/reader/state", path)));
    enqueue(() => store(readerApiUrl("/api/reader/annotations", path)));
    enqueue(() => store(coverUrl(path)));
    for (let n = 1; n <= chaptersTotal; n += 1) {
      enqueue(() => ensureChapter(n));
    }

    const result = await finished;
    finalize(result);
    return result;
  } catch (error) {
    const result = toResult(error);
    finalize(result);
    return result;
  }
}

/* Drop every cache entry that belongs to a book — both the URLs recorded in
   the index (stylesheet refs carry no ?path=) and anything the service worker
   stored under the book's path while it was being read. */
export async function removeOfflineBook(path: string): Promise<void> {
  const entry = getOfflineSnapshot().find((book) => book.path === path);
  try {
    const cache = await caches.open(CACHE_NAME);
    const keys = await cache.keys();
    const targets = new Set<string>();
    for (const request of keys) {
      if (urlBelongsToBook(request.url, path)) targets.add(request.url);
    }
    for (const url of entry?.urls ?? []) {
      targets.add(new URL(url, window.location.origin).href);
    }
    await Promise.all([...targets].map((url) => cache.delete(url).catch(() => false)));
  } catch {
    // cache unavailable — the index entry is still removed below
  }
  writeIndex(getOfflineSnapshot().filter((book) => book.path !== path));
}
