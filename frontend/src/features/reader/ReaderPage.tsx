import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import * as Dialog from "@radix-ui/react-dialog";
import {
  AlignJustify,
  AlignLeft,
  ArrowLeft,
  Bot,
  ChevronLeft,
  ChevronRight,
  Headphones,
  Highlighter,
  ListTree,
  Loader2,
  Minus,
  Music2,
  Pause,
  Play,
  Plus,
  RotateCcw,
  Search,
  Settings2,
  Square,
  StickyNote,
  Trash2,
  X,
} from "lucide-react";
import {
  createAnnotation,
  deleteAnnotation,
  formatBytes,
  getAnnotations,
  getReaderChapter,
  getReaderMeta,
  getReaderState,
  getTtsCacheStatus,
  inspectTtsCache,
  lookupReaderText,
  saveReaderState,
  searchReaderBook,
  startTtsCache,
  stopTtsCache,
  clearTtsCache,
  updateAnnotation,
  type Annotation,
  type ReaderMeta,
  type ReaderSearchHit,
  type ReaderState,
} from "../../lib/api";
import { cn } from "../../lib/cn";
import { rememberBook } from "../../lib/legacy";
import { useTheme } from "../../lib/theme";
import { currentTrack, usePlayerStore } from "../music/playerStore";
import {
  READER_MARK_CSS,
  applyAnnotations,
  buildBlockMap,
  clearQueryMarks,
  findQuote,
  focusQueryHit,
  highlightQuery,
  selectionInfo,
  type BlockMap,
} from "./readerDom";
import { useReaderTts } from "./useReaderTts";

type ReaderSettings = {
  size: number;
  lh: number;
  width: number;
  margin: number;
  ls: number;
  align: "left" | "justify";
  family: "serif" | "sans";
  breakWords: boolean;
  overrideFont: boolean;
  pageTheme: "app" | "light" | "sepia" | "dark" | "black" | "custom" | "deep-yellow";
  customBg: string;
  customFg: string;
  mode: "scroll" | "page";
  columns: 1 | 2;
};

const SETTINGS_KEY = "v2.reader.settings";
const defaultSettings: ReaderSettings = {
  size: 19,
  lh: 1.78,
  width: 760,
  margin: 24,
  ls: 0,
  align: "left",
  family: "serif",
  breakWords: false,
  overrideFont: false,
  pageTheme: "app",
  customBg: "#f7f3e8",
  customFg: "#312b22",
  mode: "scroll",
  columns: 1,
};

function loadSettings(): ReaderSettings {
  try {
    const raw = localStorage.getItem(SETTINGS_KEY);
    if (!raw) return defaultSettings;
    const parsed = JSON.parse(raw) as Partial<ReaderSettings>;
    return { ...defaultSettings, ...parsed };
  } catch {
    return defaultSettings;
  }
}

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

function chapterTitle(meta: ReaderMeta | undefined, n: number) {
  return meta?.chapters.find((chapter) => chapter.n === n)?.title || `Chương ${n}`;
}

const pageThemes: Array<{ id: ReaderSettings["pageTheme"]; label: string; swatch: string; bg?: string; fg?: string }> = [
  { id: "app", label: "Theo WebUI", swatch: "bg-gradient-to-br from-[#151a22] to-[#f3f5f8]" },
  { id: "light", label: "Sáng", swatch: "bg-[#f5f6fa]", bg: "#f5f6fa", fg: "#202633" },
  { id: "sepia", label: "Sepia", swatch: "bg-[#f3ead9]", bg: "#f3ead9", fg: "#3d3225" },
  { id: "dark", label: "Tối", swatch: "bg-[#151a22]", bg: "#151a22", fg: "#e7e9ee" },
  { id: "black", label: "Đen", swatch: "bg-black", bg: "#000000", fg: "#d8d8d8" },
  { id: "deep-yellow", label: "Đậm vàng", swatch: "bg-[#FFB300]", bg: "#FFB300", fg: "#000000" },
  { id: "custom", label: "Tùy chỉnh", swatch: "bg-gradient-to-br from-amber-200 to-sky-300" },
];

const annotationColors: Array<{ id: Annotation["color"]; label: string; className: string }> = [
  { id: "yellow", label: "Vàng", className: "bg-yellow-400" },
  { id: "green", label: "Xanh lá", className: "bg-emerald-400" },
  { id: "blue", label: "Xanh dương", className: "bg-sky-400" },
  { id: "pink", label: "Hồng", className: "bg-pink-400" },
];

function formatEta(seconds?: number | null) {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  return `${minutes}m ${rest}s`;
}

type Selection = {
  item: number | null;
  quote: string;
  pre: string;
  post: string;
};

export function ReaderPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const path = searchParams.get("path") || "";
  const requestedChapter = Number(searchParams.get("n") || "");
  const { theme } = useTheme();
  const frameRef = useRef<HTMLIFrameElement>(null);
  const blocksRef = useRef<BlockMap>({ byItem: new Map(), byElement: new Map() });
  const bootedRef = useRef(false);
  const lastRequestedRef = useRef(requestedChapter);
  const [chapter, setChapter] = useState<number | null>(null);
  const [frameReady, setFrameReady] = useState(0);
  const [tocOpen, setTocOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [ttsOpen, setTtsOpen] = useState(false);
  const [annOpen, setAnnOpen] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const [lookupOpen, setLookupOpen] = useState(false);
  const [tocQuery, setTocQuery] = useState("");
  const [settings, setSettings] = useState<ReaderSettings>(loadSettings);
  const [autoScroll, setAutoScroll] = useState(() => {
    try { return localStorage.getItem("v2.reader.autoscroll") !== "0"; } catch { return true; }
  });
  const [selection, setSelection] = useState<Selection | null>(null);
  const [selectionNote, setSelectionNote] = useState("");
  const [footnote, setFootnote] = useState<{ text: string; id: string } | null>(null);
  const [annFocus, setAnnFocus] = useState<string | null>(null);
  const [searchScope, setSearchScope] = useState<"book" | "chapter">("book");
  const [searchQ, setSearchQ] = useState("");
  const [searchResult, setSearchResult] = useState<ReaderSearchHit | null>(null);
  const [searchBusy, setSearchBusy] = useState(false);
  const [chapterHits, setChapterHits] = useState<HTMLElement[]>([]);
  const [hitIndex, setHitIndex] = useState(0);
  const [pendingQuery, setPendingQuery] = useState<{ q: string; ch: number } | null>(null);
  const [cacheChapters, setCacheChapters] = useState(50);
  const [musicOpen, setMusicOpen] = useState(false);
  const track = usePlayerStore(currentTrack);
  const tracks = usePlayerStore((state) => state.tracks);
  const musicPlaying = usePlayerStore((state) => state.playing);
  const playTrack = usePlayerStore((state) => state.playTrack);
  const toggleMusic = usePlayerStore((state) => state.toggle);

  const metaQuery = useQuery({
    queryKey: ["reader-meta", path],
    queryFn: () => getReaderMeta(path),
    enabled: Boolean(path),
    staleTime: 60_000,
  });
  const stateQuery = useQuery({
    queryKey: ["reader-state", path],
    queryFn: () => getReaderState(path),
    enabled: Boolean(path),
    staleTime: Infinity,
  });
  const chapterQuery = useQuery({
    queryKey: ["reader-chapter", path, chapter],
    queryFn: () => getReaderChapter(path, chapter!),
    enabled: Boolean(path && chapter),
    staleTime: 10 * 60_000,
  });
  const annotationsQuery = useQuery({
    queryKey: ["reader-annotations", path],
    queryFn: () => getAnnotations(path),
    enabled: Boolean(path),
  });
  const anns = annotationsQuery.data?.anns ?? [];
  const meta = metaQuery.data;
  // savedParagraphRef: holds the item index we want to resume TTS from when the
  // chapter matching stateQuery.data?.pos?.ch first loads. Initialized lazily
  // so we pick it up even after stateQuery resolves.
  const savedParagraphRef = useRef<number | null>(null);
  // pendingRestoreRef: the chapter we're waiting to load before restoring TTS
  const pendingRestoreRef = useRef<number | null>(null);

  const total = meta?.total ?? 0;
  const items = chapterQuery.data?.items ?? [];

  // autoPlayRef: when set to true the reader will speakFrom(0) after the next chapter finishes loading
  const autoPlayRef = useRef(false);
  // Hold latest chapter & total in refs so the onEnd callback is always fresh
  const chapterRef = useRef(chapter);
  const totalRef = useRef(total);
  useEffect(() => { chapterRef.current = chapter; }, [chapter]);
  useEffect(() => { totalRef.current = total; }, [total]);

  const tts = useReaderTts({
    chapter,
    items,
    frameRef,
    blocksRef,
    autoScroll,
    onEnd: useCallback(() => {
      const ch = chapterRef.current;
      const tot = totalRef.current;
      if (!ch || !tot || ch >= tot) return; // no next chapter
      autoPlayRef.current = true;
      // goChapter is stable (useCallback), but we can't call it here directly
      // because it's declared below. We drive the chapter change via a small event.
      setChapter((prev) => (prev !== null ? clamp(prev + 1, 1, tot) : prev));
    }, []),
  });

  const cacheStatus = useQuery({
    queryKey: ["tts-cache-status"],
    queryFn: getTtsCacheStatus,
    enabled: ttsOpen,
    refetchInterval: (query) => (query.state.data?.job?.state === "running" ? 1500 : false),
  });
  const inspectCache = useQuery({
    queryKey: ["tts-cache-inspect", path, chapter, tts.voice],
    queryFn: () => inspectTtsCache(path, chapter!, tts.voice),
    enabled: ttsOpen && Boolean(path && chapter),
    staleTime: 10_000,
  });

  const createAnn = useMutation({
    mutationFn: (color: Annotation["color"]) => {
      if (!selection || !chapter) throw new Error("Chưa chọn đoạn văn.");
      return createAnnotation(path, {
        ch: chapter,
        item: selection.item,
        quote: selection.quote,
        pre: selection.pre,
        post: selection.post,
        color,
        note: selectionNote.trim(),
      });
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["reader-annotations", path] });
      clearSelection();
    },
  });
  const deleteAnn = useMutation({
    mutationFn: (id: string) => deleteAnnotation(path, id),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["reader-annotations", path] }),
  });
  const editAnn = useMutation({
    mutationFn: (ann: Annotation) => updateAnnotation(path, ann),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["reader-annotations", path] }),
  });
  const lookup = useMutation({
    mutationFn: (selected: Selection) =>
      lookupReaderText(selected.quote, `${selected.pre} ${selected.quote} ${selected.post}`.trim()),
    onSuccess: () => setLookupOpen(true),
    onError: () => setLookupOpen(true),
  });

  const startCache = useMutation({
    mutationFn: () => startTtsCache(path, chapter!, cacheChapters, tts.voice),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["tts-cache-status"] });
      void queryClient.invalidateQueries({ queryKey: ["tts-cache-inspect"] });
    },
  });
  const stopCache = useMutation({
    mutationFn: stopTtsCache,
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["tts-cache-status"] }),
  });
  const clearCache = useMutation({
    mutationFn: clearTtsCache,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["tts-cache-status"] });
      void queryClient.invalidateQueries({ queryKey: ["tts-cache-inspect"] });
    },
  });

  useEffect(() => {
    bootedRef.current = false;
    lastRequestedRef.current = Number.NaN;
    setChapter(null);
    setFrameReady(0);
    if (path) rememberBook(path);
  }, [path]);

  useEffect(() => {
    if (!meta) return;
    const fromUrl = Number.isFinite(requestedChapter) && requestedChapter > 0 ? requestedChapter : null;
    if (fromUrl) {
      if (!bootedRef.current) {
        bootedRef.current = true;
        lastRequestedRef.current = requestedChapter;
        setChapter(clamp(fromUrl, 1, meta.total));
      }
      return;
    }

    if (stateQuery.isLoading || bootedRef.current) return;
    bootedRef.current = true;
    const savedCh = Number(stateQuery.data?.pos?.ch) || null;
    const savedItem = stateQuery.data?.pos?.item ?? null;
    // Queue TTS restore only if there's a saved paragraph index
    if (savedItem != null && savedCh != null) {
      savedParagraphRef.current = savedItem;
      pendingRestoreRef.current = savedCh;
    }
    setChapter(clamp(savedCh ?? 1, 1, meta.total));
  }, [meta, requestedChapter, stateQuery.data?.pos?.ch, stateQuery.data?.pos?.item, stateQuery.isLoading]);

  useEffect(() => {
    if (!meta || !Number.isFinite(requestedChapter) || requestedChapter <= 0) return;
    if (requestedChapter === lastRequestedRef.current) return;
    lastRequestedRef.current = requestedChapter;
    setChapter(clamp(requestedChapter, 1, meta.total));
  }, [meta, requestedChapter]);

  useEffect(() => {
    if (!chapter || !path) return;
    if (searchParams.get("path") === path && searchParams.get("n") === String(chapter)) return;
    const timer = window.setTimeout(() => {
      const current = new URLSearchParams(searchParams);
      current.set("path", path);
      current.set("n", String(chapter));
      setSearchParams(current, { replace: true });
    }, 120);
    return () => window.clearTimeout(timer);
  }, [chapter, path, searchParams, setSearchParams]);

  useEffect(() => {
    if (!chapter || !path) return;
    const timer = window.setTimeout(() => {
      const previous = stateQuery.data ?? {};
      const next: ReaderState = {
        ...previous,
        pos: { ...(previous.pos ?? {}), v: 2, ch: chapter },
        settings: { ...(previous.settings ?? {}), ...settings },
      };
      void saveReaderState(path, next).catch(() => undefined);
    }, 900);
    return () => window.clearTimeout(timer);
  }, [chapter, path, settings, stateQuery.data]);

  useEffect(() => {
    try {
      localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
    } catch {
      // ignore private mode / quota
    }
  }, [settings]);

  useEffect(() => {
    try { localStorage.setItem("v2.reader.autoscroll", autoScroll ? "1" : "0"); } catch { /* ignore */ }
  }, [autoScroll]);

  // After the chapter iframe finishes loading, either:
  //  (a) auto-play from the beginning if we just auto-advanced from the previous chapter, or
  //  (b) restore TTS to a saved paragraph position
  useEffect(() => {
    if (!frameReady || !chapter) return;
    if (autoPlayRef.current) {
      // Wait for the new chapter's items to load before starting playback.
      // Without this guard, speakFrom(0) returns immediately (empty items)
      // and the auto-advance stalls on the new chapter.
      if (items.length === 0) return;
      autoPlayRef.current = false;
      tts.speakFrom(0);
      return;
    }
    if (pendingRestoreRef.current !== chapter) return;
    const item = savedParagraphRef.current;
    if (item == null) return;
    if (items.length === 0) return;
    pendingRestoreRef.current = null;
    savedParagraphRef.current = null;
    tts.speakFrom(item);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [frameReady, chapter, items]);

  useEffect(() => {
    tts.stop();
    setFrameReady(0);
    setSelection(null);
    setChapterHits([]);
    setHitIndex(0);
    setPendingQuery(null);
    setAnnFocus(null);
    setFootnote(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chapter]);

  const docUrl = useMemo(() => {
    if (!path || !chapter) return "";
    const params = new URLSearchParams({ path, n: String(chapter) });
    // Inside the /v2 scope on purpose: the PWA service worker must control this
    // navigation (and the res? subresources it loads) for offline reading.
    return `/v2/doc?${params.toString()}`;
  }, [path, chapter]);

  const clearSelection = useCallback(() => {
    frameRef.current?.contentWindow?.getSelection()?.removeAllRanges();
    setSelection(null);
    setSelectionNote("");
  }, []);

  const closeFootnote = useCallback(() => {
    const doc = frameRef.current?.contentDocument;
    for (const mark of Array.from(doc?.querySelectorAll(".rd-fnmark") || [])) mark.classList.remove("rd-fnmark");
    setFootnote(null);
  }, [frameRef]);

  const applyFrameTheme = useCallback(() => {
    const frame = frameRef.current;
    const doc = frame?.contentDocument;
    if (!doc) return;
    const root = doc.documentElement;
    const body = doc.body;
    const computed = getComputedStyle(document.documentElement);
    const appBg = computed.getPropertyValue("--color-bg").trim();
    const appFg = computed.getPropertyValue("--color-ink").trim();
    const accent = computed.getPropertyValue("--color-accent").trim();
    const preset = pageThemes.find((item) => item.id === settings.pageTheme);
    const bg = settings.pageTheme === "custom" ? settings.customBg : preset?.bg || appBg;
    const fg = settings.pageTheme === "custom" ? settings.customFg : preset?.fg || appFg;
    const family = computed
      .getPropertyValue(settings.family === "sans" ? "--font-sans" : "--font-reading")
      .trim() || (settings.family === "sans" ? "ui-sans-serif, system-ui, sans-serif" : "Georgia, Cambria, serif");
    root.style.setProperty("--rd-bg", bg);
    root.style.setProperty("--rd-fg", fg);
    root.style.setProperty("--rd-fam", family);
    root.style.setProperty("--rd-size", `${settings.size}px`);
    root.style.setProperty("--rd-lh", String(settings.lh));
    root.style.setProperty("--rd-w", `${settings.width}px`);
    root.style.setProperty("--rd-ls", `${settings.ls}px`);
    root.style.setProperty("--rd-padx", `${settings.margin}px`);
    root.style.setProperty("--rd-pad-y", `${Math.max(settings.margin, 16)}px`);
    root.style.setProperty("--rd-align", settings.align);
    root.style.setProperty("--rd-ow", settings.breakWords ? "break-word" : "normal");
    root.style.colorScheme = fg && bg && bg.toLowerCase() !== "#f5f6fa" && bg.toLowerCase() !== "#f3ead9" ? "dark" : "light";
    root.classList.toggle("rd-force", settings.overrideFont);
    body.classList.toggle("rd-has-player", Boolean(track));
    body.style.background = bg;
    body.style.color = fg;
    let style = doc.getElementById("rd-v2-accent") as HTMLStyleElement | null;
    if (!style) {
      style = doc.createElement("style");
      style.id = "rd-v2-accent";
      doc.head.append(style);
    }
    style.textContent = `:root{--rd-bg:${bg};--rd-fg:${fg}} a{color:${accent}} ::selection{background:${accent}55} body.rd-has-player .rd-content{padding-bottom:168px}`;
    let marks = doc.getElementById("rd-v2-marks") as HTMLStyleElement | null;
    if (!marks) {
      marks = doc.createElement("style");
      marks.id = "rd-v2-marks";
      doc.head.append(marks);
    }
    marks.textContent = READER_MARK_CSS;

    const content = doc.querySelector<HTMLElement>(".rd-content");
    if (content) {
      if (settings.mode === "page") {
        content.style.height = "calc(100dvh - 8px)";
        content.style.columnCount = String(settings.columns);
        content.style.columnGap = "48px";
        content.style.columnFill = "auto";
        content.style.overflow = "hidden";
        content.style.width = "100%";
        content.style.maxWidth = "none";
        content.style.scrollBehavior = "smooth";
        body.style.overflow = "hidden";
      } else {
        content.style.height = "";
        content.style.columnCount = "";
        content.style.columnGap = "";
        content.style.columnFill = "";
        content.style.overflow = "";
        content.style.width = "";
        content.style.maxWidth = "";
        content.style.scrollBehavior = "";
        body.style.overflow = "";
      }
    }

    if (!root.dataset.v2Bound) {
      root.dataset.v2Bound = "1";
      doc.addEventListener("click", (event) => {
        const target = event.target as Element | null;
        const link = target?.closest?.("a");
        const mark = target?.closest?.(".rd-ann-mark") as HTMLElement | null;
        if (mark) {
          setAnnOpen(true);
          setAnnFocus(mark.dataset.annId || null);
          return;
        }
        if (!link) return;
        const href = link.getAttribute("href") || "";
        if (href.startsWith("/api/reader/doc")) {
          event.preventDefault();
          const url = new URL(href, window.location.origin);
          const n = Number(url.searchParams.get("n"));
          if (Number.isFinite(n) && n > 0) setChapter(n);
          return;
        }
        if (href.startsWith("#")) {
          event.preventDefault();
          let id = href.slice(1);
          try { id = decodeURIComponent(id); } catch { /* keep encoded */ }
          const target = doc.getElementById(id);
          if (!target) return;
          const anchorType = (link.getAttribute("epub:type") || link.getAttribute("type") || "").toLowerCase();
          const targetType = (target.getAttribute("epub:type") || target.getAttribute("type") || "").toLowerCase();
          const isFootnote = anchorType.includes("noteref")
            || Boolean(target.closest('aside, footer, [role="doc-footnote"]'))
            || targetType.includes("footnote")
            || targetType.includes("endnote")
            || (/^page/i.test(id) ? false : /^(fn|fnote|foot|note|endnote|noteref)/i.test(id) || /^\d+(_\d+)?$/.test(id));
          if (isFootnote) {
            for (const mark of Array.from(doc.querySelectorAll(".rd-fnmark"))) mark.classList.remove("rd-fnmark");
            link.classList.add("rd-fnmark");
            const text = (target.innerText || target.textContent || "").trim().slice(0, 8000);
            setFootnote({ text, id });
          } else {
            try { target.scrollIntoView({ block: "center", behavior: "smooth" }); } catch { target.scrollIntoView(); }
          }
          return;
        }
        if (/^https?:\/\//i.test(href)) {
          event.preventDefault();
          window.open(href, "_blank", "noopener,noreferrer");
        }
      });
      doc.addEventListener("mouseup", () => {
        window.setTimeout(() => {
          setSelection(selectionInfo(frameRef.current, blocksRef.current));
        }, 10);
      });
    }
  }, [settings, theme, track?.id]);

  const rebuildBlocks = useCallback(() => {
    blocksRef.current = buildBlockMap(frameRef.current, items);
    if (chapter) applyAnnotations(frameRef.current, anns, blocksRef.current, chapter);
  }, [anns, chapter, items]);

  useEffect(() => {
    applyFrameTheme();
  }, [applyFrameTheme]);

  useEffect(() => {
    if (frameReady) rebuildBlocks();
  }, [frameReady, rebuildBlocks]);

  useEffect(() => {
    if (frameReady && chapter) {
      applyAnnotations(frameRef.current, anns, blocksRef.current, chapter);
    }
  }, [anns, chapter, frameReady]);

  useEffect(() => {
    if (!pendingQuery || !frameReady || chapter == null || chapter !== pendingQuery.ch) return;
    const hits = highlightQuery(frameRef.current, blocksRef.current, pendingQuery.q);
    setChapterHits(hits);
    setHitIndex(0);
    if (hits[0]) focusQueryHit(hits[0]);
    setPendingQuery(null);
  }, [pendingQuery, frameReady, chapter]);

  useEffect(() => {
    if (!annFocus || !frameReady || chapter == null) return;
    const ann = anns.find((item) => item.id === annFocus);
    if (!ann || ann.ch !== chapter) return;
    const found = findQuote(blocksRef.current, ann);
    if (found) {
      try { found.element.scrollIntoView({ block: "center", behavior: "smooth" }); } catch { found.element.scrollIntoView(); }
    }
    setAnnFocus(null);
  }, [annFocus, anns, chapter, frameReady]);

  const handleFrameLoad = useCallback(() => {
    applyFrameTheme();
    blocksRef.current = buildBlockMap(frameRef.current, items);
    if (chapter) applyAnnotations(frameRef.current, anns, blocksRef.current, chapter);
    setFrameReady((value) => value + 1);
  }, [anns, applyFrameTheme, chapter, items]);

  const goChapter = useCallback((n: number) => {
    if (!total) return;
    // Don't go past bounds
    const clamped = clamp(n, 1, total);
    if (clamped === chapter) return;
    setChapter(clamped);
    tts.stop();
    // Clear any pending restore so a manual chapter jump starts fresh
    pendingRestoreRef.current = null;
    savedParagraphRef.current = null;
  }, [chapter, total, tts]);

  const turnPage = useCallback((direction: number) => {
    const doc = frameRef.current?.contentDocument;
    const content = doc?.querySelector<HTMLElement>(".rd-content");
    if (!content) {
      // No paged content div; only advance chapter if not at bounds
      if (direction > 0 && (chapter || 1) >= total) { tts.stop(); return; }
      if (direction < 0 && (chapter || 1) <= 1) { tts.stop(); return; }
      goChapter((chapter || 1) + direction);
      return;
    }
    if (settings.mode === "page") {
      // At the last page of the chapter — stop TTS, do not advance chapter
      if (direction > 0 && content.scrollLeft + content.clientWidth >= content.scrollWidth - 2) {
        tts.stop();
        return;
      }
      // At the first page of the chapter going backward — go to previous chapter
      if (direction < 0 && content.scrollLeft <= 2) {
        goChapter((chapter || 1) - 1);
        return;
      }
      content.scrollBy({ left: direction * Math.max(240, content.clientWidth * 0.9), behavior: "smooth" });
    } else {
      // Scroll mode: just advance/retreat chapters
      goChapter((chapter || 1) + direction);
    }
  }, [chapter, goChapter, settings.mode, total, tts]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) return;
      if (!chapter) return;
      if (event.key === "ArrowLeft") {
        event.preventDefault();
        if (settings.mode === "page") turnPage(-1);
        else goChapter(chapter - 1);
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        if (settings.mode === "page") turnPage(1);
        else goChapter(chapter + 1);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [chapter, goChapter, settings.mode, turnPage]);

  const filteredChapters = useMemo(() => {
    if (!meta) return [];
    const needle = tocQuery.trim().toLowerCase();
    if (!needle) return meta.chapters;
    return meta.chapters.filter((item) => item.title.toLowerCase().includes(needle));
  }, [meta, tocQuery]);

  // Scroll the TOC list to the current chapter once per open. Doing this in a
  // ref callback re-runs on every render (read-aloud re-renders per paragraph),
  // which yanked the list back and made other chapters impossible to pick while
  // audio was playing. The list only mounts together with the dialog and the
  // chapter array can arrive later, so retry over a few frames — but only mark
  // the open as handled once the jump actually landed.
  const tocListRef = useRef<HTMLDivElement | null>(null);
  const tocScrolledRef = useRef(false);
  useEffect(() => {
    if (!tocOpen) {
      tocScrolledRef.current = false;
      return;
    }
    if (tocScrolledRef.current) return;
    let frame = 0;
    let tries = 0;
    const jump = () => {
      const el = tocListRef.current;
      const active = el?.querySelector<HTMLElement>(`[data-ch="${chapter}"]`);
      if (el && active) {
        const top = active.getBoundingClientRect().top - el.getBoundingClientRect().top + el.scrollTop;
        el.scrollTop = Math.max(0, Math.round(top - (el.clientHeight - active.offsetHeight) / 2));
        tocScrolledRef.current = true;
        return;
      }
      if (++tries < 90) frame = requestAnimationFrame(jump);
    };
    jump();
    return () => cancelAnimationFrame(frame);
  }, [tocOpen, chapter, filteredChapters]);

  const runSearch = async () => {
    const q = searchQ.trim();
    if (!q || !path) return;
    setSearchBusy(true);
    setSearchResult(null);
    try {
      if (searchScope === "chapter") {
        const hits = highlightQuery(frameRef.current, blocksRef.current, q);
        setChapterHits(hits);
        setHitIndex(0);
        if (hits[0]) focusQueryHit(hits[0]);
        setSearchResult({
          query: q,
          total: hits.length,
          truncated: false,
          chapters: [],
        });
      } else {
        const result = await searchReaderBook(path, q, 400);
        setSearchResult(result);
        clearQueryMarks(frameRef.current?.contentDocument);
        setChapterHits([]);
      }
    } catch (error) {
      setSearchResult({
        query: q,
        total: 0,
        truncated: false,
        chapters: [],
      });
      setSearchBusy(false);
      window.alert(error instanceof Error ? error.message : "Tìm kiếm thất bại.");
      return;
    }
    setSearchBusy(false);
  };

  const gotoSearchHit = (ch: number) => {
    const q = searchQ.trim();
    if (!q) return;
    setPendingQuery({ q, ch });
    setSearchOpen(false);
    if (ch !== chapter) goChapter(ch);
  };

  const jumpToAnnotation = (ann: Annotation) => {
    setAnnOpen(false);
    if (ann.id) setAnnFocus(ann.id);
    if (ann.ch !== chapter) goChapter(ann.ch);
  };

  const currentParagraph = useMemo(() => {
    if (tts.currentItem == null) return 0;
    return items.slice(0, tts.currentItem + 1).filter((item) => item.type === "p").length;
  }, [items, tts.currentItem]);
  const totalParagraphs = useMemo(() => items.filter((item) => item.type === "p").length, [items]);
  const cacheJob = cacheStatus.data?.job;
  const cachePercent = cacheJob && cacheJob.chars_total
    ? Math.min(100, Math.round(((cacheJob.chars_done || 0) / cacheJob.chars_total) * 100))
    : 0;

  if (!path) {
    return (
      <div className="flex min-h-dvh items-center justify-center bg-bg p-6">
        <div className="card max-w-md p-8 text-center">
          <h1 className="text-xl font-bold text-ink">Thiếu đường dẫn sách</h1>
          <p className="mt-2 text-sm text-muted">Mở một sách từ thư viện để bắt đầu đọc.</p>
          <Link to="/library" className="btn btn-primary mt-5">Mở thư viện</Link>
        </div>
      </div>
    );
  }

  if (metaQuery.isLoading) {
    return (
      <div className="flex min-h-dvh items-center justify-center bg-bg text-sm text-muted">
        <Loader2 className="mr-2 h-4 w-4 animate-spin text-accent" /> Đang mở sách…
      </div>
    );
  }

  if (metaQuery.isError || !meta) {
    return (
      <div className="flex min-h-dvh items-center justify-center bg-bg p-6">
        <div className="card max-w-md p-8">
          <h1 className="text-xl font-bold text-ink">Không mở được sách</h1>
          <p className="mt-2 text-sm text-danger">
            {metaQuery.error instanceof Error ? metaQuery.error.message : "Đường dẫn không hợp lệ."}
          </p>
          <Link to="/library" className="btn btn-primary mt-5">Về thư viện</Link>
        </div>
      </div>
    );
  }

  if (!meta.total) {
    return (
      <div className="flex min-h-dvh items-center justify-center bg-bg p-6">
        <div className="card max-w-md p-8 text-center">
          <h1 className="text-xl font-bold text-ink">Sách không có chương</h1>
          <p className="mt-2 text-sm text-muted">Không tìm thấy nội dung có thể đọc trong file EPUB này.</p>
          <Link to="/library" className="btn btn-primary mt-5">Về thư viện</Link>
        </div>
      </div>
    );
  }

  if (chapter === null) {
    return (
      <div className="flex min-h-dvh items-center justify-center bg-bg text-sm text-muted">
        <Loader2 className="mr-2 h-4 w-4 animate-spin text-accent" /> Đang mở chương…
      </div>
    );
  }

  return (
    <div className="flex h-dvh flex-col overflow-hidden bg-bg">
      <header className="flex h-14 shrink-0 items-center gap-1 border-b border-line bg-surface/95 px-2 backdrop-blur sm:gap-2 sm:px-4">
        <button
          className="focus-ring flex h-10 w-10 shrink-0 items-center justify-center rounded-xl text-muted hover:bg-surface-2 hover:text-ink"
          onClick={() => navigate("/library")}
          aria-label="Về thư viện"
        >
          <ArrowLeft className="h-5 w-5" />
        </button>
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-semibold text-ink">{meta.title}</div>
          <div className="hidden truncate text-[11px] text-muted sm:block">
            {chapterTitle(meta, chapter)} · {chapter}/{total}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-0.5">
          <button
            className="focus-ring hidden h-10 w-10 items-center justify-center rounded-xl text-muted hover:bg-surface-2 hover:text-ink disabled:opacity-35 sm:flex"
            onClick={() => goChapter(chapter - 1)}
            disabled={chapter <= 1}
            aria-label="Chương trước"
          >
            <ChevronLeft className="h-5 w-5" />
          </button>
          <button
            className="focus-ring hidden h-10 w-10 items-center justify-center rounded-xl text-muted hover:bg-surface-2 hover:text-ink disabled:opacity-35 sm:flex"
            onClick={() => goChapter(chapter + 1)}
            disabled={chapter >= total}
            aria-label="Chương sau"
          >
            <ChevronRight className="h-5 w-5" />
          </button>
          <button
            className="focus-ring hidden h-10 w-10 items-center justify-center rounded-xl text-muted hover:bg-surface-2 hover:text-ink sm:flex"
            onClick={() => setSearchOpen(true)}
            aria-label="Tìm trong sách"
            title="Tìm trong sách"
          >
            <Search className="h-5 w-5" />
          </button>
          <button
            className={cn(
              "focus-ring flex h-10 w-10 items-center justify-center rounded-xl hover:bg-surface-2 hover:text-ink",
              tts.state === "idle" ? "text-muted" : "bg-accent-soft text-accent",
            )}
            onClick={() => setTtsOpen(true)}
            aria-label="Đọc aloud / TTS"
            title="Đọc aloud / TTS"
          >
            <Headphones className="h-5 w-5" />
          </button>
          <button
            className="focus-ring hidden h-10 w-10 items-center justify-center rounded-xl text-muted hover:bg-surface-2 hover:text-ink sm:flex"
            onClick={() => setAnnOpen(true)}
            aria-label="Ghi chú & đánh dấu"
            title="Ghi chú & đánh dấu"
          >
            <StickyNote className="h-5 w-5" />
          </button>
          <button
            className="focus-ring flex h-10 w-10 items-center justify-center rounded-xl text-muted hover:bg-surface-2 hover:text-ink"
            onClick={() => setTocOpen(true)}
            aria-label="Mục lục"
            title="Mục lục"
          >
            <ListTree className="h-5 w-5" />
          </button>
          <button
            className="focus-ring flex h-10 w-10 items-center justify-center rounded-xl text-muted hover:bg-surface-2 hover:text-ink"
            onClick={() => setSettingsOpen(true)}
            aria-label="Cài đặt đọc"
            title="Cài đặt đọc"
          >
            <Settings2 className="h-5 w-5" />
          </button>
          <button
            className="focus-ring flex h-10 w-10 items-center justify-center rounded-xl text-muted hover:bg-surface-2 hover:text-ink"
            onClick={() => { setMusicOpen(true); setTtsOpen(false); }}
            aria-label="Nhạc nền"
            title="Nhạc nền"
          >
            <Music2 className="h-5 w-5" />
          </button>
        </div>
      </header>

      <div className="flex h-10 shrink-0 items-center gap-1 border-b border-line bg-surface/95 px-2 sm:hidden">
        <button
          className="focus-ring flex h-8 w-8 items-center justify-center rounded-lg text-muted hover:bg-surface-2 hover:text-ink disabled:opacity-35"
          onClick={() => goChapter(chapter - 1)}
          disabled={chapter <= 1}
          aria-label="Chương trước"
        >
          <ChevronLeft className="h-4 w-4" />
        </button>
        <div className="min-w-0 flex-1 truncate text-center text-[11px] text-muted">
          {chapterTitle(meta, chapter)} · {chapter}/{total}
        </div>
        <button className="focus-ring flex h-8 w-8 items-center justify-center rounded-lg text-muted hover:bg-surface-2 hover:text-ink" onClick={() => setSearchOpen(true)} aria-label="Tìm trong sách"><Search className="h-4 w-4" /></button>
        <button className="focus-ring flex h-8 w-8 items-center justify-center rounded-lg text-muted hover:bg-surface-2 hover:text-ink" onClick={() => setAnnOpen(true)} aria-label="Ghi chú"><StickyNote className="h-4 w-4" /></button>
        <button
          className="focus-ring flex h-8 w-8 items-center justify-center rounded-lg text-muted hover:bg-surface-2 hover:text-ink disabled:opacity-35"
          onClick={() => goChapter(chapter + 1)}
          disabled={chapter >= total}
          aria-label="Chương sau"
        >
          <ChevronRight className="h-4 w-4" />
        </button>
      </div>

      <div className="relative min-h-0 flex-1">
        <iframe
          ref={frameRef}
          key={docUrl}
          src={docUrl}
          title={`${meta.title} — ${chapterTitle(meta, chapter)}`}
          sandbox="allow-same-origin"
          referrerPolicy="no-referrer"
          onLoad={handleFrameLoad}
          className="h-full w-full border-0 bg-bg"
        />
        <div className="pointer-events-none absolute inset-x-0 bottom-0 h-8 bg-gradient-to-t from-bg to-transparent" />
        {settings.mode === "page" ? (
          <div className={cn(
            "pointer-events-none absolute inset-x-0 z-10 flex justify-between px-3",
            track || tts.state !== "idle" ? "bottom-24" : "bottom-4",
          )}>
            <button className="pointer-events-auto flex h-10 w-10 items-center justify-center rounded-full border border-line bg-surface/90 text-muted shadow-lg backdrop-blur hover:text-ink" onClick={() => turnPage(-1)} aria-label="Trang trước">
              <ChevronLeft className="h-5 w-5" />
            </button>
            <button className="pointer-events-auto flex h-10 w-10 items-center justify-center rounded-full border border-line bg-surface/90 text-muted shadow-lg backdrop-blur hover:text-ink" onClick={() => turnPage(1)} aria-label="Trang sau">
              <ChevronRight className="h-5 w-5" />
            </button>
          </div>
        ) : null}
      </div>

      {selection ? (
        <div className={cn(
          "fixed inset-x-2 z-40 mx-auto max-w-2xl rounded-2xl border border-line bg-surface/98 p-3 shadow-2xl shadow-black/40 backdrop-blur animate-slide-up",
          track ? "bottom-[calc(11.4rem+env(safe-area-inset-bottom))]" : tts.state !== "idle" ? "bottom-[calc(6.2rem+env(safe-area-inset-bottom))]" : "bottom-[calc(1rem+env(safe-area-inset-bottom))]",
        )}>
          <div className="flex items-start gap-2">
            <p className="line-clamp-2 min-w-0 flex-1 text-xs leading-5 text-muted">“{selection.quote}”</p>
            {/* Start TTS from selected paragraph */}
            {selection.item != null ? (
              <button
                className="focus-ring flex h-7 shrink-0 items-center justify-center gap-1 rounded-lg bg-accent px-2 text-[11px] font-semibold text-accent-ink transition-transform hover:scale-105 active:scale-95"
                onClick={() => {
                  tts.stop();
                  tts.speakFrom(selection.item!);
                  clearSelection();
                }}
                aria-label="Đọc từ đây"
              >
                <Play className="h-3.5 w-3.5" /> Đọc từ đây
              </button>
            ) : null}
            <button className="focus-ring flex h-7 w-7 shrink-0 items-center justify-center rounded-lg text-muted transition-colors hover:bg-surface-2 hover:text-ink" onClick={clearSelection} aria-label="Bỏ chọn"><X className="h-4 w-4" /></button>
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <span className="flex items-center gap-1 text-xs text-faint"><Highlighter className="h-3.5 w-3.5" /> Đánh dấu</span>
            {annotationColors.map((color) => (
              <button
                key={color.id}
                type="button"
                className={cn("focus-ring h-7 w-7 rounded-full border-2 border-white/20 shadow", color.className)}
                title={color.label}
                aria-label={`Đánh dấu ${color.label}`}
                onClick={() => createAnn.mutate(color.id)}
                disabled={createAnn.isPending}
              />
            ))}
            <button className="btn btn-ghost min-h-8 px-2 text-xs" type="button" onClick={() => { setLookupOpen(true); lookup.mutate(selection); }} disabled={lookup.isPending}>
              {lookup.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Bot className="h-3.5 w-3.5" />} Hỏi AI
            </button>
            <input
              className="input min-h-8 min-w-36 flex-1 py-1 text-xs"
              value={selectionNote}
              onChange={(event) => setSelectionNote(event.target.value)}
              placeholder="Ghi chú (không bắt buộc)…"
            />
          </div>
          {createAnn.isError ? <p className="mt-2 text-xs text-danger">{createAnn.error instanceof Error ? createAnn.error.message : "Không lưu được đánh dấu."}</p> : null}
        </div>
      ) : null}

      {tts.state !== "idle" ? (
        <div className={cn(
          // Geometry must match MusicPlayer's in-reader branch (inset-x-3 +
          // max-w-3xl) so both floating bars share the same width; when the
          // BGM strip is visible its top edge sits at ~5.75rem, hence 6rem.
          "fixed inset-x-3 z-30 mx-auto flex max-w-3xl items-center gap-2 rounded-2xl border border-line bg-surface/95 p-2 shadow-2xl shadow-black/30 backdrop-blur animate-slide-up",
          track ? "bottom-[calc(6rem+env(safe-area-inset-bottom))]" : "bottom-[calc(0.75rem+env(safe-area-inset-bottom))]",
        )}>
          <button className="focus-ring flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-accent text-accent-ink" onClick={tts.toggle} aria-label={tts.state === "playing" ? "Tạm dừng đọc" : "Tiếp tục đọc"}>
            {tts.state === "playing" ? <Pause className="h-4 w-4" /> : tts.state === "loading" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="ml-0.5 h-4 w-4" />}
          </button>
          <button className="focus-ring flex h-9 w-9 shrink-0 items-center justify-center rounded-lg text-muted hover:bg-surface-2 hover:text-ink" onClick={() => tts.seek(-1)} aria-label="Đoạn trước" title="Đoạn trước">
            <ChevronLeft className="h-4 w-4" />
          </button>
          <div className="min-w-0 flex-1">
            <p className="truncate text-xs font-medium text-ink">{tts.state === "paused" ? "Đã tạm dừng" : tts.state === "loading" ? "Đang chuẩn bị audio…" : "Đang đọc aloud"}</p>
            <p className="truncate text-[11px] text-muted">Đoạn {currentParagraph || "…"} / {totalParagraphs || "…"} · {chapterTitle(meta, chapter)}</p>
          </div>
          <button className="focus-ring flex h-9 w-9 shrink-0 items-center justify-center rounded-lg text-muted hover:bg-surface-2 hover:text-ink" onClick={() => tts.seek(1)} aria-label="Đoạn sau" title="Đoạn sau">
            <ChevronRight className="h-4 w-4" />
          </button>
          <button className="focus-ring flex h-9 w-9 items-center justify-center rounded-lg text-muted hover:bg-surface-2 hover:text-ink" onClick={tts.stop} aria-label="Dừng đọc"><Square className="h-4 w-4" /></button>
          <button className="focus-ring flex h-9 w-9 items-center justify-center rounded-lg text-muted hover:bg-surface-2 hover:text-ink" onClick={() => setTtsOpen(true)} aria-label="Mở điều khiển TTS"><Headphones className="h-4 w-4" /></button>
        </div>
      ) : null}

      {/* TOC */}
      <Dialog.Root open={tocOpen} onOpenChange={(open) => { setTocOpen(open); if (open) setTocQuery(""); }}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-50 bg-black/55 backdrop-blur-sm animate-fade-in" />
          <Dialog.Content className="fixed inset-y-0 right-0 z-50 flex w-[min(92vw,25rem)] flex-col border-l border-line bg-surface shadow-2xl outline-none animate-slide-in-right">
            <div className="flex items-center justify-between border-b border-line px-5 py-4">
              <div>
                <Dialog.Title className="text-base font-semibold text-ink">Mục lục</Dialog.Title>
                <Dialog.Description className="mt-0.5 text-xs text-muted">{total} chương · {meta.title}</Dialog.Description>
              </div>
              <Dialog.Close asChild>
                <button className="focus-ring flex h-10 w-10 items-center justify-center rounded-xl text-muted transition-colors hover:bg-surface-2 hover:text-ink" aria-label="Đóng mục lục"><X className="h-5 w-5" /></button>
              </Dialog.Close>
            </div>
            <div className="border-b border-line p-4">
              <div className="relative">
                <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-faint" />
                <input className="input pl-9" type="search" placeholder="Tìm chương…" value={tocQuery} onChange={(event) => setTocQuery(event.target.value)} autoFocus />
              </div>
              <button className="btn btn-ghost mt-2 w-full justify-start text-xs" onClick={() => { setTocOpen(false); setSearchOpen(true); }}>
                <Search className="h-3.5 w-3.5" /> Tìm trong toàn bộ sách
              </button>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto p-2" ref={tocListRef}>
              {filteredChapters.length ? (
                <ol className="space-y-0.5">
                  {filteredChapters.map((item) => (
                    <li key={item.n}>
                      <button
                        data-ch={item.n}
                        className={cn(
                          "flex w-full items-start gap-3 rounded-xl px-3 py-2.5 text-left text-sm transition-colors duration-150",
                          item.n === chapter
                            ? "bg-accent-soft font-semibold text-accent"
                            : "text-muted hover:bg-surface-2 hover:text-ink",
                        )}
                        onClick={() => { goChapter(item.n); setTocOpen(false); }}
                      >
                        <span className="mt-0.5 w-8 shrink-0 text-right text-[11px] tabular-nums text-faint">{item.n}</span>
                        <span className="min-w-0">{item.title || `Chương ${item.n}`}</span>
                      </button>
                    </li>
                  ))}
                </ol>
              ) : <p className="p-6 text-center text-sm text-muted">Không có chương phù hợp.</p>}
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>

      {/* Footnote popup (same affordance as the v1 reader) */}
      <Dialog.Root open={Boolean(footnote)} onOpenChange={(open) => { if (!open) closeFootnote(); }}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-[70] bg-black/45 backdrop-blur-sm" />
          <Dialog.Content className="fixed inset-x-0 bottom-0 z-[70] max-h-[70dvh] overflow-y-auto rounded-t-2xl border border-line bg-surface p-5 pb-[calc(1.25rem+env(safe-area-inset-bottom))] shadow-2xl outline-none sm:bottom-auto sm:left-1/2 sm:top-1/2 sm:w-[min(92vw,36rem)] sm:-translate-x-1/2 sm:-translate-y-1/2 sm:rounded-2xl sm:pb-5">
            <div className="flex items-start justify-between gap-3">
              <div>
                <Dialog.Title className="flex items-center gap-2 text-base font-semibold text-ink">
                  <StickyNote className="h-4 w-4 text-accent" /> Chú thích
                </Dialog.Title>
                <Dialog.Description className="mt-0.5 text-xs text-muted">Nội dung chú thích trong sách · #{footnote?.id}</Dialog.Description>
              </div>
              <Dialog.Close asChild>
                <button className="focus-ring flex h-9 w-9 shrink-0 items-center justify-center rounded-lg text-muted hover:bg-surface-2 hover:text-ink" aria-label="Đóng chú thích"><X className="h-4 w-4" /></button>
              </Dialog.Close>
            </div>
            <p className="mt-4 whitespace-pre-line text-sm leading-7 text-ink">{footnote?.text || "Không có nội dung chú thích."}</p>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>

      {/* TTS / music panel */}
      <Dialog.Root open={ttsOpen} onOpenChange={setTtsOpen}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-50 bg-black/55 backdrop-blur-sm animate-fade-in" />
          <Dialog.Content className="fixed inset-y-0 right-0 z-50 flex w-[min(94vw,26rem)] flex-col border-l border-line bg-surface shadow-2xl outline-none animate-slide-in-right">
            <div className="flex items-center justify-between border-b border-line px-5 py-4">
              <div>
                <Dialog.Title className="flex items-center gap-2 text-base font-semibold text-ink"><Headphones className="h-4 w-4 text-accent" /> Đọc aloud</Dialog.Title>
                <Dialog.Description className="mt-0.5 text-xs text-muted">TTS cục bộ · lưu cache theo chương</Dialog.Description>
              </div>
              <Dialog.Close asChild>
                <button className="focus-ring flex h-10 w-10 items-center justify-center rounded-xl text-muted hover:bg-surface-2 hover:text-ink" aria-label="Đóng TTS"><X className="h-5 w-5" /></button>
              </Dialog.Close>
            </div>
            <div className="min-h-0 flex-1 space-y-6 overflow-y-auto p-5">
              <section className="space-y-3">
                <div className="flex items-center justify-between gap-2">
                  <span className="label">Giọng đọc</span>
                  <span className={cn("text-[11px]", tts.status?.state === "ready" ? "text-emerald-300" : "text-warning")}>
                    {tts.status?.state === "ready" ? "Sẵn sàng" : tts.status?.state === "loading" ? "Đang nạp model…" : tts.status?.state === "error" ? "Lỗi model" : "Chưa nạp"}
                  </span>
                </div>
                <select className="input" value={tts.voice} onChange={(event) => tts.setVoice(event.target.value)} disabled={!tts.voices.length}>
                  {!tts.voices.length ? <option value="">Đang tải giọng…</option> : null}
                  {tts.voices.map((voice) => (
                    <option key={voice.id} value={voice.id}>
                      {voice.label || voice.id}{voice.gender ? ` · ${voice.gender}` : ""}{voice.region ? ` · ${voice.region}` : ""}
                    </option>
                  ))}
                </select>
                {tts.error ? <p className="text-xs text-danger">{tts.error}</p> : null}
                {tts.status?.error ? <p className="text-xs text-danger">{tts.status.error}</p> : null}
                <div className="flex flex-wrap gap-2">
                  <button className="btn btn-primary flex-1" onClick={tts.toggle}>
                    {tts.state === "playing" ? <Pause className="h-4 w-4" /> : tts.state === "loading" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
                    {tts.state === "playing" ? "Tạm dừng" : tts.state === "paused" ? "Tiếp tục" : "Đọc chương"}
                  </button>
                  <button className="btn btn-secondary" onClick={tts.stop} disabled={tts.state === "idle"} aria-label="Dừng"><Square className="h-4 w-4" /></button>
                </div>
                <div className="grid grid-cols-2 gap-2">
                  <button className="btn btn-secondary min-h-9 px-2 text-xs" onClick={() => tts.seek(-1)} disabled={tts.state === "idle"} aria-label="Đoạn trước">
                    <ChevronLeft className="h-4 w-4" /> Đoạn trước
                  </button>
                  <button className="btn btn-secondary min-h-9 px-2 text-xs" onClick={() => tts.seek(1)} disabled={tts.state === "idle"} aria-label="Đoạn sau">
                    Đoạn sau <ChevronRight className="h-4 w-4" />
                  </button>
                </div>
                <button className="btn btn-ghost min-h-9 w-full text-xs" onClick={() => void tts.test()} disabled={tts.state !== "idle" || tts.testing || tts.status?.state !== "ready"}>
                  {tts.testing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Headphones className="h-4 w-4" />}
                  {tts.testing ? "Đang nghe thử…" : "Nghe thử giọng đọc"}
                </button>
                <p className="text-xs text-faint">Đoạn {currentParagraph || "—"} / {totalParagraphs || "—"} · {chapterTitle(meta, chapter)}</p>
              </section>

              <section>
                <div className="flex items-center justify-between">
                  <label className="label" htmlFor="tts-rate">Tốc độ đọc</label>
                  <span className="text-xs tabular-nums text-muted">{tts.rate.toFixed(2)}×</span>
                </div>
                <input id="tts-rate" className="mt-3 h-1.5 w-full accent-[var(--color-accent)]" type="range" min="0.6" max="2" step="0.05" value={tts.rate} onChange={(event) => tts.setRate(Number(event.target.value))} />
                <label className="mt-4 flex items-center gap-2 text-sm text-muted">
                  <input type="checkbox" className="h-4 w-4 accent-[var(--color-accent)]" checked={autoScroll} onChange={(event) => setAutoScroll(event.target.checked)} />
                  Tự cuộn theo câu đang đọc
                </label>
              </section>

              <section className="rounded-xl border border-line bg-surface-2/50 p-3">
                <div className="flex items-center justify-between gap-2">
                  <h3 className="text-sm font-semibold text-ink">Cache TTS</h3>
                  <span className="text-[11px] text-muted">{cacheStatus.data?.disk ? `${formatBytes(cacheStatus.data.disk.bytes || 0)} · ${cacheStatus.data.disk.files || 0} file` : "—"}</span>
                </div>
                {inspectCache.data ? (
                  <p className="mt-2 text-xs text-muted">Chương {chapter}: {inspectCache.data.cached}/{inspectCache.data.pieces} đoạn đã có cache.</p>
                ) : null}
                {cacheJob ? (
                  <div className="mt-3 space-y-2">
                    <div className="flex items-center justify-between text-[11px] text-muted">
                      <span>{cacheJob.state === "running" ? `Đang render chương ${cacheJob.ch_now || "…"}` : cacheJob.state}</span>
                      <span>{cacheJob.ch_done || 0}/{cacheJob.chapters || 0} ch · ETA {formatEta(cacheJob.eta)}</span>
                    </div>
                    <div className="h-1.5 overflow-hidden rounded-full bg-surface-3"><div className="h-full bg-accent transition-all" style={{ width: `${cachePercent}%` }} /></div>
                  </div>
                ) : null}
                <div className="mt-3 flex flex-wrap items-end gap-2">
                  <label className="min-w-20 flex-1 space-y-1">
                    <span className="text-[10px] uppercase tracking-wide text-faint">Số chương</span>
                    <input className="input min-h-8 py-1 text-xs" type="number" min={1} max={999} value={cacheChapters} onChange={(event) => setCacheChapters(clamp(Number(event.target.value) || 1, 1, 999))} />
                  </label>
                  <button className="btn btn-secondary min-h-8 px-2 text-xs" onClick={() => startCache.mutate()} disabled={startCache.isPending || cacheJob?.state === "running"}>Pre-render</button>
                  <button className="btn btn-ghost min-h-8 px-2 text-xs" onClick={() => stopCache.mutate()} disabled={cacheJob?.state !== "running"}>Dừng</button>
                  <button className="btn btn-ghost min-h-8 px-2 text-xs text-red-300" onClick={() => { if (window.confirm("Xoá toàn bộ cache TTS?")) clearCache.mutate(); }}>Xoá</button>
                </div>
                {startCache.isError ? <p className="mt-2 text-xs text-danger">{startCache.error instanceof Error ? startCache.error.message : "Không bắt đầu được cache."}</p> : null}
              </section>

              <section>
                <div className="flex items-center justify-between gap-2">
                  <h3 className="flex items-center gap-2 text-sm font-semibold text-ink"><Music2 className="h-4 w-4 text-accent" /> Nhạc nền</h3>
                  <Link to="/music" className="text-xs text-accent hover:underline">Quản lý</Link>
                </div>
                {track ? (
                  <div className="mt-2 flex items-center gap-2 rounded-xl border border-line bg-surface-2/50 p-2">
                    <button className="flex h-9 w-9 items-center justify-center rounded-full bg-accent text-accent-ink" onClick={toggleMusic}>{musicPlaying ? <Pause className="h-4 w-4" /> : <Play className="ml-0.5 h-4 w-4" />}</button>
                    <div className="min-w-0 flex-1"><p className="truncate text-xs font-medium text-ink">{track.title}</p><p className="truncate text-[10px] text-faint">{track.artist || "Nhạc nền"}</p></div>
                  </div>
                ) : <p className="mt-2 text-xs text-muted">Chưa chọn bài. Mở thư viện nhạc để phát dưới giọng đọc.</p>}
                {tracks.length ? (
                  <div className="mt-2 max-h-36 space-y-1 overflow-y-auto">
                    {tracks.slice(0, 8).map((item) => (
                      <button key={item.id} className={cn("flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-xs", item.id === track?.id ? "bg-accent-soft text-accent" : "text-muted hover:bg-surface-2 hover:text-ink")} onClick={() => playTrack(item)}>
                        <Music2 className="h-3.5 w-3.5 shrink-0" /><span className="truncate">{item.title}</span>
                      </button>
                    ))}
                  </div>
                ) : null}
              </section>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>

      {/* Search */}
      <Dialog.Root open={searchOpen} onOpenChange={setSearchOpen}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-50 bg-black/55 backdrop-blur-sm animate-fade-in" />
          <Dialog.Content className="fixed left-1/2 top-[8vh] z-50 flex max-h-[84dvh] w-[calc(100vw-1.5rem)] max-w-2xl -translate-x-1/2 flex-col overflow-hidden rounded-2xl border border-line bg-surface shadow-2xl outline-none animate-scale-in">
            <div className="flex items-center justify-between border-b border-line px-5 py-4">
              <div><Dialog.Title className="text-base font-semibold text-ink">Tìm trong sách</Dialog.Title><Dialog.Description className="mt-0.5 text-xs text-muted">{meta.title}</Dialog.Description></div>
              <Dialog.Close asChild><button className="flex h-9 w-9 items-center justify-center rounded-lg text-muted hover:bg-surface-2 hover:text-ink" aria-label="Đóng"><X className="h-4 w-4" /></button></Dialog.Close>
            </div>
            <form className="space-y-3 border-b border-line p-4" onSubmit={(event) => { event.preventDefault(); void runSearch(); }}>
              <div className="relative">
                <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-faint" />
                <input className="input pl-9" autoFocus type="search" placeholder="Nhập từ hoặc câu…" value={searchQ} onChange={(event) => setSearchQ(event.target.value)} />
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <button type="button" className={cn("btn min-h-8 px-3 text-xs", searchScope === "book" ? "btn-primary" : "btn-ghost")} onClick={() => setSearchScope("book")}>Toàn sách</button>
                <button type="button" className={cn("btn min-h-8 px-3 text-xs", searchScope === "chapter" ? "btn-primary" : "btn-ghost")} onClick={() => setSearchScope("chapter")}>Chương này</button>
                <button className="btn btn-secondary ml-auto min-h-8 px-3 text-xs" type="submit" disabled={searchBusy || !searchQ.trim()}>{searchBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null} Tìm</button>
                {chapterHits.length ? <button type="button" className="btn btn-ghost min-h-8 px-2 text-xs" onClick={() => { clearQueryMarks(frameRef.current?.contentDocument); setChapterHits([]); setSearchResult(null); }}>Xoá dấu</button> : null}
              </div>
            </form>
            <div className="min-h-0 flex-1 overflow-y-auto p-3">
              {searchScope === "chapter" && chapterHits.length ? (
                <div className="mb-3 flex items-center gap-2 text-xs text-muted">
                  <span>{hitIndex + 1}/{chapterHits.length} kết quả trong chương</span>
                  <button className="btn btn-ghost min-h-7 px-2 text-xs" onClick={() => { const next = (hitIndex - 1 + chapterHits.length) % chapterHits.length; setHitIndex(next); focusQueryHit(chapterHits[next]); }}>Trước</button>
                  <button className="btn btn-ghost min-h-7 px-2 text-xs" onClick={() => { const next = (hitIndex + 1) % chapterHits.length; setHitIndex(next); focusQueryHit(chapterHits[next]); }}>Sau</button>
                </div>
              ) : null}
              {searchResult && searchScope === "chapter" ? <p className="p-3 text-sm text-muted">{searchResult.total ? `${searchResult.total} kết quả trong chương.` : "Không tìm thấy."}</p> : null}
              {searchResult && searchScope === "book" ? (
                searchResult.chapters.length ? (
                  <div className="space-y-3">
                    <p className="px-2 text-xs text-faint">{searchResult.total} kết quả{searchResult.truncated ? " (đã giới hạn)" : ""}</p>
                    {searchResult.chapters.map((item) => (
                      <section key={item.n}>
                        <button className="flex w-full items-center gap-2 rounded-lg bg-surface-2 px-3 py-2 text-left text-sm font-medium text-ink hover:bg-surface-3" onClick={() => gotoSearchHit(item.n)}>
                          <span className="text-[11px] text-faint">{item.n}</span><span className="min-w-0 flex-1 truncate">{item.title}</span><span className="text-[11px] text-accent">{item.count}</span>
                        </button>
                        <div className="mt-1 space-y-0.5">
                          {item.hits.map((hit) => (
                            <button key={`${item.n}-${hit.off}`} className="line-clamp-2 w-full rounded px-3 py-1.5 text-left text-xs leading-5 text-muted hover:bg-surface-2" onClick={() => gotoSearchHit(item.n)}>{hit.snip}</button>
                          ))}
                        </div>
                      </section>
                    ))}
                  </div>
                ) : <p className="p-3 text-sm text-muted">Không tìm thấy kết quả.</p>
              ) : null}
              {!searchResult ? <p className="p-4 text-center text-sm text-muted">Tìm theo từ hoặc câu trong toàn bộ sách hoặc chương hiện tại.</p> : null}
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>

      {/* Annotations */}
      <Dialog.Root open={annOpen} onOpenChange={setAnnOpen}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-50 bg-black/55 backdrop-blur-sm animate-fade-in" />
          <Dialog.Content className="fixed inset-y-0 right-0 z-50 flex w-[min(94vw,25rem)] flex-col border-l border-line bg-surface shadow-2xl outline-none animate-slide-in-right">
            <div className="flex items-center justify-between border-b border-line px-5 py-4">
              <div><Dialog.Title className="flex items-center gap-2 text-base font-semibold text-ink"><StickyNote className="h-4 w-4 text-accent" /> Ghi chú & đánh dấu</Dialog.Title><Dialog.Description className="mt-0.5 text-xs text-muted">{anns.length} mục · bôi đen chữ để thêm mới</Dialog.Description></div>
              <Dialog.Close asChild><button className="flex h-10 w-10 items-center justify-center rounded-xl text-muted hover:bg-surface-2 hover:text-ink" aria-label="Đóng"><X className="h-5 w-5" /></button></Dialog.Close>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto p-3">
              {!anns.length ? <p className="p-6 text-center text-sm text-muted">Chưa có ghi chú. Bôi đen một đoạn trong chương để đánh dấu.</p> : (
                <div className="space-y-2">
                  {anns.slice().sort((a, b) => (a.ch - b.ch) || String(a.id).localeCompare(String(b.id))).map((ann) => (
                    <article key={ann.id || `${ann.ch}-${ann.quote}`} className="rounded-xl border border-line bg-surface-2/50 p-3">
                      <div className="flex items-start gap-2">
                        <span className={cn("mt-1 h-3 w-3 shrink-0 rounded-full", annotationColors.find((color) => color.id === ann.color)?.className || "bg-yellow-400")} />
                        <button className="min-w-0 flex-1 text-left" onClick={() => jumpToAnnotation(ann)}>
                          <p className="line-clamp-3 text-xs leading-5 text-ink">{ann.quote}</p>
                          <p className="mt-1 text-[10px] text-faint">Chương {ann.ch}{ann.note ? " · có ghi chú" : ""}</p>
                        </button>
                        <button className="text-faint hover:text-ink" onClick={() => { if (ann.id) { const note = window.prompt("Ghi chú", ann.note || ""); if (note !== null) editAnn.mutate({ ...ann, note }); } }} aria-label="Sửa ghi chú"><Highlighter className="h-3.5 w-3.5" /></button>
                        <button className="text-faint hover:text-red-300" onClick={() => { if (ann.id && window.confirm("Xoá đánh dấu này?")) deleteAnn.mutate(ann.id); }} aria-label="Xoá"><Trash2 className="h-3.5 w-3.5" /></button>
                      </div>
                      {ann.note ? <p className="mt-2 whitespace-pre-wrap border-t border-line pt-2 text-xs leading-5 text-muted">{ann.note}</p> : null}
                    </article>
                  ))}
                </div>
              )}
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>

      {/* Lookup */}
      <Dialog.Root open={lookupOpen} onOpenChange={setLookupOpen}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm" />
          <Dialog.Content className="fixed left-1/2 top-1/2 z-50 w-[calc(100vw-2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 rounded-2xl border border-line bg-surface p-5 shadow-2xl outline-none">
            <div className="flex items-start justify-between gap-3">
              <div><Dialog.Title className="flex items-center gap-2 text-base font-semibold text-ink"><Bot className="h-4 w-4 text-accent" /> Giải thích</Dialog.Title><Dialog.Description className="mt-1 line-clamp-2 text-xs text-muted">{selection?.quote}</Dialog.Description></div>
              <Dialog.Close asChild><button className="flex h-8 w-8 items-center justify-center rounded-lg text-muted hover:bg-surface-2 hover:text-ink" aria-label="Đóng"><X className="h-4 w-4" /></button></Dialog.Close>
            </div>
            <div className="mt-4">
              {lookup.isPending ? <p className="flex items-center gap-2 text-sm text-muted"><Loader2 className="h-4 w-4 animate-spin text-accent" /> Đang hỏi AI…</p> : lookup.isError ? <p className="text-sm text-danger">{lookup.error instanceof Error ? lookup.error.message : "Không tra được."}</p> : <p className="whitespace-pre-wrap text-sm leading-6 text-ink">{lookup.data?.result || "Không có kết quả."}</p>}
              {lookup.data?.engine ? <p className="mt-3 text-[11px] text-faint">Provider: {lookup.data.engine}</p> : null}
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>

      {/* Settings */}
      <Dialog.Root open={settingsOpen} onOpenChange={setSettingsOpen}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-50 bg-black/55 backdrop-blur-sm animate-fade-in" />
          <Dialog.Content className="fixed inset-y-0 right-0 z-50 flex w-[min(94vw,26rem)] flex-col border-l border-line bg-surface shadow-2xl outline-none animate-slide-in-right">
            <div className="flex items-center justify-between border-b border-line px-5 py-4">
              <div><Dialog.Title className="text-base font-semibold text-ink">Cài đặt đọc</Dialog.Title><Dialog.Description className="mt-0.5 text-xs text-muted">Chỉ áp dụng cho reader v2.</Dialog.Description></div>
              <Dialog.Close asChild><button className="focus-ring flex h-10 w-10 items-center justify-center rounded-xl text-muted hover:bg-surface-2 hover:text-ink" aria-label="Đóng cài đặt"><X className="h-5 w-5" /></button></Dialog.Close>
            </div>
            <div className="min-h-0 flex-1 space-y-6 overflow-y-auto p-5">
              <section>
                <div className="flex items-center justify-between"><span className="label">Chế độ đọc</span><span className="text-xs text-muted">{settings.mode === "page" ? `${settings.columns} cột` : "Cuộn"}</span></div>
                <div className="mt-3 flex gap-2">
                  <button className={cn("btn flex-1", settings.mode === "scroll" ? "btn-primary" : "btn-secondary")} onClick={() => setSettings((prev) => ({ ...prev, mode: "scroll" }))}>Cuộn</button>
                  <button className={cn("btn flex-1", settings.mode === "page" ? "btn-primary" : "btn-secondary")} onClick={() => setSettings((prev) => ({ ...prev, mode: "page" }))}>Trang</button>
                </div>
                {settings.mode === "page" ? (
                  <div className="mt-2 flex gap-2">
                    {[1, 2].map((columns) => <button key={columns} className={cn("btn flex-1 min-h-8 text-xs", settings.columns === columns ? "btn-primary" : "btn-secondary")} onClick={() => setSettings((prev) => ({ ...prev, columns: columns as 1 | 2 }))}>{columns} cột</button>)}
                  </div>
                ) : null}
              </section>

              <section>
                <div className="flex items-center justify-between"><label className="label" htmlFor="reader-size">Cỡ chữ</label><span className="text-xs tabular-nums text-muted">{settings.size}px</span></div>
                <input id="reader-size" className="mt-3 h-1.5 w-full accent-[var(--color-accent)]" type="range" min="15" max="32" step="1" value={settings.size} onChange={(event) => setSettings((prev) => ({ ...prev, size: Number(event.target.value) }))} />
              </section>
              <section>
                <div className="flex items-center justify-between"><label className="label" htmlFor="reader-lh">Giãn dòng</label><span className="text-xs tabular-nums text-muted">{settings.lh.toFixed(2)}</span></div>
                <input id="reader-lh" className="mt-3 h-1.5 w-full accent-[var(--color-accent)]" type="range" min="1.35" max="2.2" step="0.05" value={settings.lh} onChange={(event) => setSettings((prev) => ({ ...prev, lh: Number(event.target.value) }))} />
              </section>
              <section>
                <div className="flex items-center justify-between"><label className="label" htmlFor="reader-width">Độ rộng cột chữ</label><span className="text-xs tabular-nums text-muted">{settings.width}px</span></div>
                <input id="reader-width" className="mt-3 h-1.5 w-full accent-[var(--color-accent)]" type="range" min="520" max="1120" step="20" value={settings.width} onChange={(event) => setSettings((prev) => ({ ...prev, width: Number(event.target.value) }))} />
              </section>
              <section>
                <div className="flex items-center justify-between"><label className="label" htmlFor="reader-ls">Giãn chữ</label><span className="text-xs tabular-nums text-muted">{settings.ls}px</span></div>
                <input id="reader-ls" className="mt-3 h-1.5 w-full accent-[var(--color-accent)]" type="range" min="0" max="4" step="0.5" value={settings.ls} onChange={(event) => setSettings((prev) => ({ ...prev, ls: Number(event.target.value) }))} />
              </section>
              <section>
                <div className="flex items-center justify-between"><label className="label" htmlFor="reader-margin">Lề trang</label><span className="text-xs tabular-nums text-muted">{settings.margin}px</span></div>
                <input id="reader-margin" className="mt-3 h-1.5 w-full accent-[var(--color-accent)]" type="range" min="8" max="64" step="4" value={settings.margin} onChange={(event) => setSettings((prev) => ({ ...prev, margin: Number(event.target.value) }))} />
              </section>

              <section>
                <span className="label">Kiểu chữ</span>
                <div className="mt-3 grid grid-cols-2 gap-2">
                  {[{ id: "serif" as const, label: "Serif", sample: "Aa" }, { id: "sans" as const, label: "Sans", sample: "Aa" }].map((item) => (
                    <button key={item.id} className={cn("focus-ring rounded-xl border px-3 py-3 text-left", settings.family === item.id ? "border-accent bg-accent-soft" : "border-line bg-surface-2")} onClick={() => setSettings((prev) => ({ ...prev, family: item.id }))} aria-pressed={settings.family === item.id}>
                      <span className={cn("text-lg", item.id === "sans" && "font-sans")} style={item.id === "serif" ? { fontFamily: "var(--font-reading)" } : undefined}>{item.sample}</span><span className="ml-2 text-xs text-muted">{item.label}</span>
                    </button>
                  ))}
                </div>
                <label className="mt-3 flex items-center gap-2 text-sm text-muted"><input type="checkbox" className="h-4 w-4 accent-[var(--color-accent)]" checked={settings.overrideFont} onChange={(event) => setSettings((prev) => ({ ...prev, overrideFont: event.target.checked }))} /> Luôn dùng font đã chọn</label>
              </section>

              <section>
                <span className="label">Căn chữ</span>
                <div className="mt-3 flex gap-2">
                  <button className={cn("btn flex-1", settings.align === "left" ? "btn-primary" : "btn-secondary")} onClick={() => setSettings((prev) => ({ ...prev, align: "left" }))}><AlignLeft className="h-4 w-4" /> Trái</button>
                  <button className={cn("btn flex-1", settings.align === "justify" ? "btn-primary" : "btn-secondary")} onClick={() => setSettings((prev) => ({ ...prev, align: "justify" }))}><AlignJustify className="h-4 w-4" /> Đều</button>
                </div>
                <label className="mt-3 flex items-center gap-2 text-sm text-muted"><input type="checkbox" className="h-4 w-4 accent-[var(--color-accent)]" checked={settings.breakWords} onChange={(event) => setSettings((prev) => ({ ...prev, breakWords: event.target.checked }))} /> Ngắt từ dài</label>
              </section>

              <section>
                <span className="label">Nền trang đọc</span>
                <div className="mt-3 grid grid-cols-2 gap-2">
                  {pageThemes.map((item) => (
                    <button key={item.id} className={cn("flex items-center gap-2 rounded-xl border p-2 text-left text-xs transition-colors duration-150", settings.pageTheme === item.id ? "border-accent bg-accent-soft text-accent" : "border-line bg-surface-2 text-muted hover:border-accent/40")} onClick={() => setSettings((prev) => ({ ...prev, pageTheme: item.id }))}>
                      <span className={cn("h-5 w-5 shrink-0 rounded border border-line-strong", item.swatch)} />{item.label}
                    </button>
                  ))}
                </div>
                {settings.pageTheme === "custom" ? (
                  <div className="mt-3 grid grid-cols-2 gap-2">
                    <label className="space-y-1 text-xs text-muted">Nền<input className="input h-9 p-1" type="color" value={settings.customBg} onChange={(event) => setSettings((prev) => ({ ...prev, customBg: event.target.value }))} /></label>
                    <label className="space-y-1 text-muted">Chữ<input className="input h-9 p-1" type="color" value={settings.customFg} onChange={(event) => setSettings((prev) => ({ ...prev, customFg: event.target.value }))} /></label>
                  </div>
                ) : null}
              </section>

              <div className="flex items-center gap-2">
                <button className="btn btn-secondary flex-1" onClick={() => setSettings((prev) => ({ ...prev, size: clamp(prev.size - 1, 15, 32) }))}><Minus className="h-4 w-4" /> Nhỏ</button>
                <button className="btn btn-secondary flex-1" onClick={() => setSettings((prev) => ({ ...prev, size: clamp(prev.size + 1, 15, 32) }))}><Plus className="h-4 w-4" /> Lớn</button>
              </div>
              <button className="btn btn-ghost w-full text-xs" onClick={() => setSettings(defaultSettings)}><RotateCcw className="h-3.5 w-3.5" /> Khôi phục mặc định</button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>

      {/* Music drawer */}
      <Dialog.Root open={musicOpen} onOpenChange={setMusicOpen}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-50 bg-black/55 backdrop-blur-sm" />
          <Dialog.Content className="fixed inset-y-0 right-0 z-50 flex w-[min(94vw,24rem)] flex-col border-l border-line bg-surface shadow-2xl outline-none">
            <div className="flex items-center justify-between border-b border-line px-5 py-4">
              <div><Dialog.Title className="flex items-center gap-2 text-base font-semibold text-ink"><Music2 className="h-4 w-4 text-accent" /> Nhạc nền</Dialog.Title><Dialog.Description className="mt-0.5 text-xs text-muted">Phát dưới giọng đọc, không đổi tốc độ.</Dialog.Description></div>
              <Dialog.Close asChild><button className="flex h-10 w-10 items-center justify-center rounded-xl text-muted hover:bg-surface-2 hover:text-ink" aria-label="Đóng"><X className="h-5 w-5" /></button></Dialog.Close>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto p-3">
              {track ? <div className="mb-3 flex items-center gap-2 rounded-xl border border-line bg-surface-2/50 p-3"><button className="btn btn-primary h-10 w-10 rounded-full p-0" onClick={toggleMusic}>{musicPlaying ? <Pause className="h-4 w-4" /> : <Play className="ml-0.5 h-4 w-4" />}</button><div className="min-w-0 flex-1"><p className="truncate text-sm font-medium text-ink">{track.title}</p><p className="truncate text-xs text-muted">{track.artist || "Nhạc nền"}</p></div></div> : <p className="mb-3 text-sm text-muted">Chưa chọn bài.</p>}
              {tracks.length ? tracks.map((item) => (
                <button key={item.id} className={cn("flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-sm", item.id === track?.id ? "bg-accent-soft text-accent" : "text-muted hover:bg-surface-2 hover:text-ink")} onClick={() => playTrack(item)}>
                  <Music2 className="h-4 w-4 shrink-0" /><span className="min-w-0 flex-1 truncate">{item.title}</span>{item.id === track?.id && musicPlaying ? <span className="text-[10px]">ĐANG PHÁT</span> : null}
                </button>
              )) : <p className="p-4 text-center text-sm text-muted">Thư viện nhạc trống.</p>}
              <Link to="/music" className="btn btn-secondary mt-4 w-full justify-center text-xs">Quản lý thư viện nhạc</Link>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </div>
  );
}
