import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { getTtsStatus, synthesizeTts, synthesizeTtsStream } from "../../lib/api";
import {
  clearMarks,
  highlightParagraph,
  highlightSentence,
  type BlockMap,
  type ReaderItem,
} from "./readerDom";

type TtsState = "idle" | "loading" | "playing" | "paused";

/** One synthesized sentence as delivered by /api/tts/stream. */
type TtsChunk = { header: { s: number; e: number }; wav: Blob };

const VOICE_KEY = "v2.tts.voice";
const RATE_KEY = "v2.tts.rate";

/** Paragraphs buffered around the playing one so playback never waits on the
 *  network mid-chapter (the whole point of preloading the stream). */
const PREFETCH_AHEAD = 3;

function storedVoice() {
  try {
    return localStorage.getItem(VOICE_KEY) || "";
  } catch {
    return "";
  }
}

function storedRate() {
  try {
    const value = Number(localStorage.getItem(RATE_KEY));
    if (Number.isFinite(value) && value >= 0.6 && value <= 2) return value;
  } catch {
    // ignore
  }
  return 1;
}

export function useReaderTts({
  chapter,
  items,
  frameRef,
  blocksRef,
  autoScroll = true,
  onEnd,
}: {
  chapter: number | null;
  items: ReaderItem[];
  frameRef: React.RefObject<HTMLIFrameElement | null>;
  blocksRef: React.RefObject<BlockMap>;
  autoScroll?: boolean;
  onEnd?: () => void;
}) {
  const [state, setState] = useState<TtsState>("idle");
  const [error, setError] = useState("");
  const [currentItem, setCurrentItemState] = useState<number | null>(null);
  const [voice, setVoiceState] = useState(storedVoice);
  const [rate, setRateState] = useState(storedRate);
  const [testing, setTesting] = useState(false);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const testAudioRef = useRef<HTMLAudioElement | null>(null);
  const tokenRef = useRef(0);
  const itemsRef = useRef(items);
  const voiceRef = useRef(voice);
  const rateRef = useRef(rate);
  const pausedRef = useRef(false);
  const autoScrollRef = useRef(autoScroll);
  const currentItemRef = useRef<number | null>(null);
  const playResolveRef = useRef<(() => void) | null>(null);
  const onEndRef = useRef<(() => void) | undefined>(onEnd);
  // Frontend audio buffer: WAV chunks per paragraph so /api/tts/stream is
  // fetched a few paragraphs ahead instead of "read → then load".
  const chunkCacheRef = useRef(new Map<number, { voice: string; chunks: TtsChunk[] }>());
  const inflightRef = useRef(new Map<number, Promise<TtsChunk[]>>());
  const fetchAbortRef = useRef<AbortController | null>(null);
  // Bumped on chapter change so an in-flight fetch from the previous chapter
  // never writes its chunks into the fresh buffer (indices collide).
  const cacheEpochRef = useRef(0);

  const setCurrentItem = useCallback((index: number | null) => {
    currentItemRef.current = index;
    setCurrentItemState(index);
  }, []);

  useEffect(() => { itemsRef.current = items; }, [items]);
  useEffect(() => { voiceRef.current = voice; }, [voice]);
  useEffect(() => { rateRef.current = rate; }, [rate]);
  useEffect(() => { autoScrollRef.current = autoScroll; }, [autoScroll]);
  useEffect(() => { onEndRef.current = onEnd; }, [onEnd]);
  useEffect(() => {
    cacheEpochRef.current += 1;
    fetchAbortRef.current?.abort();
    fetchAbortRef.current = null;
    chunkCacheRef.current.clear();
    inflightRef.current.clear();
  }, [chapter]);

  const statusQuery = useQuery({
    queryKey: ["tts-status"],
    queryFn: getTtsStatus,
    refetchInterval: (query) => (query.state.data?.state === "ready" ? false : 2200),
    retry: 1,
    staleTime: 5_000,
  });
  const status = statusQuery.data;
  const voices = status?.voices || [];

  useEffect(() => {
    if (!voices.length) return;
    if (voice && voices.some((item) => item.id === voice)) return;
    const next = status?.default && voices.some((item) => item.id === status.default) ? status.default : voices[0].id;
    setVoiceState(next);
  }, [status?.default, voice, voices]);

  useEffect(() => {
    const audio = new Audio();
    audio.preload = "auto";
    audio.preservesPitch = true;
    audioRef.current = audio;
    return () => {
      tokenRef.current += 1;
      playResolveRef.current?.();
      audio.pause();
      audio.removeAttribute("src");
      audio.load();
      audioRef.current = null;
      testAudioRef.current?.pause();
      testAudioRef.current = null;
    };
  }, []);

  const clearLook = useCallback(() => {
    const doc = frameRef.current?.contentDocument;
    clearMarks(doc, "rd-tts-sent");
    for (const active of Array.from(doc?.querySelectorAll<HTMLElement>(".rd-tts-active") || [])) {
      active.classList.remove("rd-tts-active");
    }
  }, [frameRef]);

  const stop = useCallback(() => {
    tokenRef.current += 1;
    fetchAbortRef.current?.abort();
    fetchAbortRef.current = null;
    inflightRef.current.clear();
    pausedRef.current = false;
    playResolveRef.current?.();
    playResolveRef.current = null;
    const audio = audioRef.current;
    if (audio) {
      audio.pause();
      audio.removeAttribute("src");
      audio.load();
    }
    setState("idle");
    setCurrentItem(null);
    clearLook();
  }, [clearLook, setCurrentItem]);

  const pause = useCallback(() => {
    const audio = audioRef.current;
    if (!audio || state !== "playing") return;
    pausedRef.current = true;
    audio.pause();
    setState("paused");
  }, [state]);

  const resume = useCallback(() => {
    const audio = audioRef.current;
    if (!audio) return;
    pausedRef.current = false;
    setState("playing");
    void audio.play().catch((err: unknown) => {
      setError(err instanceof Error ? err.message : "Không phát được audio.");
      setState("paused");
    });
  }, []);

  const paragraphAt = useCallback((from: number) => {
    const itemsNow = itemsRef.current;
    for (let index = Math.max(0, from); index < itemsNow.length; index += 1) {
      if (itemsNow[index].type === "p" && itemsNow[index].text?.trim()) return index;
    }
    return null;
  }, []);

  // Buffered audio per paragraph index: [voiceKey, chunks]. /api/tts/stream
  // only emits once the backend synthesizes a sentence, so without a frontend
  // buffer playback stalled at paragraph starts ("read then load"). The cache
  // is primed a few paragraphs ahead and never re-requests a warm entry.

  /** Fetch one paragraph into the buffer (deduped; aborted by stop()). */
  const fetchParagraph = useCallback((index: number): Promise<TtsChunk[]> => {
    const text = itemsRef.current[index]?.text?.trim();
    if (!text) return Promise.resolve([]);
    const voiceNow = voiceRef.current;
    const cached = chunkCacheRef.current.get(index);
    if (cached && cached.voice === voiceNow) return Promise.resolve(cached.chunks);
    const existing = inflightRef.current.get(index);
    if (existing) return existing;
    const controller = fetchAbortRef.current || new AbortController();
    fetchAbortRef.current = controller;
    const epoch = cacheEpochRef.current;
    const task = (async () => {
      const chunks: TtsChunk[] = [];
      const stream = synthesizeTtsStream(text, voiceNow || null, controller.signal);
      for await (const part of stream) {
        chunks.push({ header: { s: part.header.s, e: part.header.e }, wav: part.wav });
      }
      if (cacheEpochRef.current === epoch) {
        chunkCacheRef.current.set(index, { voice: voiceNow, chunks });
      }
      return chunks;
    })();
    inflightRef.current.set(index, task);
    task.catch(() => undefined).finally(() => {
      if (inflightRef.current.get(index) === task) inflightRef.current.delete(index);
    });
    return task;
  }, []);

  /** Warm the buffer for the next few paragraphs so playback never waits on the
   *  network, and drop audio that fell too far behind to bound memory. */
  const primeCache = useCallback((center: number) => {
    let cursor = paragraphAt(center);
    for (let count = 0; cursor != null && count <= PREFETCH_AHEAD; count += 1) {
      void fetchParagraph(cursor).catch(() => undefined);
      cursor = paragraphAt(cursor + 1);
    }
    for (const key of Array.from(chunkCacheRef.current.keys())) {
      if (key < center - 3) chunkCacheRef.current.delete(key);
    }
  }, [fetchParagraph, paragraphAt]);

  /** Audio of one paragraph: replayed from the buffer when warm, otherwise
   *  streamed sentence-by-sentence (fast first sound) while filling the buffer
   *  so the next pass is gapless. */
  const paragraphAudio = useCallback((index: number): AsyncGenerator<TtsChunk> => {
    const voiceNow = voiceRef.current;
    const text = itemsRef.current[index]?.text?.trim();
    const cached = chunkCacheRef.current.get(index);
    const inflight = inflightRef.current.get(index);
    const controller = fetchAbortRef.current || new AbortController();
    fetchAbortRef.current = controller;
    const epoch = cacheEpochRef.current;
    return (async function* () {
      if (!text) return;
      if (cached && cached.voice === voiceNow) {
        for (const chunk of cached.chunks) yield chunk;
        return;
      }
      if (inflight) {
        for (const chunk of await inflight) yield chunk;
        return;
      }
      const chunks: TtsChunk[] = [];
      const stream = synthesizeTtsStream(text, voiceNow || null, controller.signal);
      for await (const part of stream) {
        const chunk = { header: { s: part.header.s, e: part.header.e }, wav: part.wav };
        chunks.push(chunk);
        yield chunk;
      }
      if (cacheEpochRef.current === epoch) {
        chunkCacheRef.current.set(index, { voice: voiceNow, chunks });
      }
    })();
  }, []);

  const playBlob = useCallback(
    (url: string, segs: number[][], element: HTMLElement | null, itemIndex: number) =>
      new Promise<void>((resolve, reject) => {
        const audio = audioRef.current;
        if (!audio) {
          reject(new Error("audio element unavailable"));
          return;
        }
        const frameDoc = frameRef.current?.contentDocument || null;
        let lastSeg = -1;
        const onTime = () => {
          if (!segs.length) return;
          const now = audio.currentTime;
          let active = 0;
          for (let index = 0; index < segs.length; index += 1) {
            if (segs[index][2] <= now + 0.03) active = index;
            else break;
          }
          if (active === lastSeg) return;
          lastSeg = active;
          const seg = segs[active];
          if (seg) highlightSentence(frameDoc, element, seg[0], seg[1]);
        };
        const finish = () => {
          audio.removeEventListener("timeupdate", onTime);
          audio.removeEventListener("ended", finish);
          audio.removeEventListener("error", fail);
          if (playResolveRef.current === finish) playResolveRef.current = null;
          resolve();
        };
        const fail = () => {
          audio.removeEventListener("timeupdate", onTime);
          audio.removeEventListener("ended", finish);
          audio.removeEventListener("error", fail);
          if (playResolveRef.current === fail) playResolveRef.current = null;
          reject(new Error("Không phát được đoạn TTS."));
        };
        audio.src = url;
        audio.playbackRate = rateRef.current;
        audio.preservesPitch = true;
        audio.addEventListener("timeupdate", onTime);
        audio.addEventListener("ended", finish);
        audio.addEventListener("error", fail);
        playResolveRef.current = finish;
        setCurrentItem(itemIndex);
        if (!pausedRef.current) {
          void audio.play().catch(fail);
        }
      }),
    [frameRef, setCurrentItem],
  );

  const playFrom = useCallback(async (start: number) => {
    const itemsNow = itemsRef.current;
    const first = Math.max(0, start);
    if (first >= itemsNow.length) return;
    const token = tokenRef.current + 1;
    tokenRef.current = token;
    pausedRef.current = false;
    setError("");
    setState("loading");
    try {
      let index = paragraphAt(first);

      while (index != null) {
        if (token !== tokenRef.current) return;
        const element = blocksRef.current.byItem.get(index) || null;
        setCurrentItem(index);
        highlightParagraph(frameRef.current?.contentDocument, element, autoScrollRef.current);
        setState("playing");
        // Buffer the paragraphs after this one while this one plays, so the
        // next paragraph starts instantly instead of "read → then load".
        primeCache(index + 1);

        for await (const chunk of paragraphAudio(index)) {
          if (token !== tokenRef.current) return;
          const url = URL.createObjectURL(chunk.wav);
          try {
            await playBlob(url, [[chunk.header.s, chunk.header.e, 0]], element, index);
          } finally {
            URL.revokeObjectURL(url);
          }
        }

        index = paragraphAt(index + 1);
      }

      setState("idle");
      setCurrentItem(null);
      clearLook();
      onEndRef.current?.();
    } catch (err) {
      if (token !== tokenRef.current) return;
      setError(err instanceof Error ? err.message : "Không phát được audio.");
      setState("idle");
    }
  }, [blocksRef, clearLook, frameRef, paragraphAt, paragraphAudio, playBlob, primeCache, setCurrentItem]);

  const play = useCallback(() => {
    if (state === "paused") {
      resume();
      return;
    }
    const current = currentItemRef.current;
    void playFrom(current == null ? 0 : current);
  }, [playFrom, resume, state]);

  const toggle = useCallback(() => {
    if (state === "playing") pause();
    else if (state === "paused") resume();
    else play();
  }, [pause, play, resume, state]);

  const seek = useCallback((direction: -1 | 1) => {
    const itemsNow = itemsRef.current;
    let target = currentItemRef.current;
    if (target == null) target = direction > 0 ? -1 : itemsNow.length;
    for (target += direction; target >= 0 && target < itemsNow.length; target += direction) {
      const item = itemsNow[target];
      if (item.type === "p" && item.text?.trim()) {
        stop();
        void playFrom(target);
        return;
      }
    }
  }, [playFrom, stop]);

  const test = useCallback(async () => {
    if (testing) return;
    setTesting(true);
    setError("");
    let audio: HTMLAudioElement | null = null;
    let url = "";
    try {
      const selected = status?.voices?.find((item) => item.id === voiceRef.current);
      const name = selected?.label || selected?.id || "tiếng Việt";
      const result = await synthesizeTts(`Xin chào, đây là giọng đọc ${name}.`, voiceRef.current || null);
      url = URL.createObjectURL(result.blob);
      audio = new Audio(url);
      testAudioRef.current = audio;
      audio.playbackRate = rateRef.current;
      audio.preservesPitch = true;
      await new Promise<void>((resolve, reject) => {
        audio!.onended = () => resolve();
        audio!.onerror = () => reject(new Error("Không phát được audio nghe thử."));
        void audio!.play().catch(reject);
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Nghe thử thất bại.");
    } finally {
      audio?.pause();
      if (url) URL.revokeObjectURL(url);
      if (testAudioRef.current === audio) testAudioRef.current = null;
      setTesting(false);
    }
  }, [status?.voices, testing]);

  const setVoice = useCallback((next: string) => {
    setVoiceState(next);
    try { localStorage.setItem(VOICE_KEY, next); } catch { /* ignore */ }
    stop();
  }, [stop]);

  const setRate = useCallback((next: number) => {
    const clamped = Math.min(2, Math.max(0.6, next));
    setRateState(clamped);
    try { localStorage.setItem(RATE_KEY, String(clamped)); } catch { /* ignore */ }
    if (audioRef.current) audioRef.current.playbackRate = clamped;
  }, []);

  useEffect(() => () => { stop(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  return {
    state,
    error,
    currentItem,
    voice,
    setVoice,
    rate,
    setRate,
    voices,
    status,
    statusQuery,
    play,
    pause,
    resume,
    toggle,
    stop,
    seek,
    test,
    testing,
    speakFrom: (index: number) => void playFrom(index),
  };
}
