export type Track = {
  id: string;
  title: string;
  artist: string;
  duration: number | null;
  source_url: string | null;
  added_at: number | null;
  size: number | null;
  url: string;
};

export type ImportState =
  | "queued"
  | "fetching"
  | "downloading"
  | "processing"
  | "cancelling"
  | "done"
  | "error"
  | "cancelled";

export type ImportJob = {
  id: string;
  url: string;
  state: ImportState;
  progress: number;
  downloaded_bytes: number;
  total_bytes: number;
  speed: number | null;
  eta: number | null;
  title: string;
  error: string | null;
  track: Track | null;
  created_at: number;
  updated_at: number;
};

export type MusicPayload = {
  tracks: Track[];
  import: ImportJob | null;
};

export type LibraryEntry = {
  name: string;
  path: string;
  size_mb: number | null;
  kind: string;
  status: "done" | "partial" | "assumed" | "untranslated";
  label: string;
  progress?: number | null;
  chapters?: number | null;
  meta?: { title?: string; author?: string; cover?: string };
};

export type LibraryGroup = {
  base_name: string;
  title: string;
  rank: string;
  rank_label: string;
  source: LibraryEntry | null;
  translations: LibraryEntry[];
  entries: LibraryEntry[];
};

export type LibraryPayload = {
  groups: LibraryGroup[];
  stats: Record<string, number>;
  locale: string;
};

export type ReaderChapterMeta = {
  n: number;
  title: string;
};

export type ReaderMeta = {
  path: string;
  title: string;
  author: string | null;
  language: string | null;
  total: number;
  chars: number;
  chapters: ReaderChapterMeta[];
};

export type ReaderState = {
  pos?: {
    v?: number;
    ch?: number;
    path?: string | null;
    off?: number;
    item?: number;
    sc?: number;
    pg?: number;
  };
  settings?: Record<string, unknown>;
  [key: string]: unknown;
};

export type ReaderTocNode = {
  title: string;
  n: number | null;
  frag?: string | null;
  children?: ReaderTocNode[];
};

export function readerQuery(path: string, extra: Record<string, string> = {}) {
  const params = new URLSearchParams({ path, ...extra });
  return params.toString();
}

export function getReaderMeta(path: string) {
  return request<ReaderMeta>(`/api/reader/meta?${readerQuery(path)}`);
}

export function getReaderToc(path: string) {
  return request<{ toc: ReaderTocNode[]; landmarks: unknown[]; pages: unknown[] }>(
    `/api/reader/toc?${readerQuery(path)}`,
  );
}

export function getReaderState(path: string) {
  return request<ReaderState>(`/api/reader/state?${readerQuery(path)}`);
}

export function saveReaderState(path: string, state: ReaderState) {
  return request<{ ok?: boolean }>("/api/reader/state", {
    method: "PUT",
    body: JSON.stringify({ path, state }),
  });
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
    ...init,
  });
  const text = await response.text();
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = text;
  }
  if (!response.ok) {
    const message =
      body && typeof body === "object" && "error" in body
        ? String((body as { error: unknown }).error)
        : `HTTP ${response.status}`;
    throw new Error(message);
  }
  return body as T;
}

export function getMusic() {
  return request<MusicPayload>("/api/v2/music");
}

export function startImport(url: string) {
  return request<{ job: ImportJob }>("/api/v2/music/import", {
    method: "POST",
    body: JSON.stringify({ url }),
  });
}

export function getImport(jobId: string) {
  return request<{ job: ImportJob }>(`/api/v2/music/import/${jobId}`);
}

export function cancelImport(jobId: string) {
  return request<{ job: ImportJob }>(`/api/v2/music/import/${jobId}`, {
    method: "DELETE",
  });
}

export function deleteTrack(id: string) {
  return request<{ ok: true }>(`/api/v2/music/${id}`, { method: "DELETE" });
}

export function getLibrary(refresh = false) {
  return request<LibraryPayload>(`/api/library${refresh ? "?refresh=1" : ""}`);
}

export type JobStatus = "queued" | "running" | "done" | "error" | "stopped" | "interrupted";

export type JobParams = {
  engine?: string;
  input?: string;
  output?: string;
  config_path?: string;
  from_chapter?: number | null;
  to_chapter?: number | null;
  from_lang?: string;
  to_lang?: string;
  description?: string | null;
  model?: string | null;
  openai_batch?: boolean;
  reset_checkpoint?: boolean;
  disable_resume?: boolean;
  tts_disk_cache?: boolean;
  tts_voice?: string | null;
  global_rules_path?: string | null;
};

export type JobProgress = {
  total_chapters?: number;
  current_chapter?: number | null;
  current_title?: string;
  last_completed?: number;
  index?: number;
  total?: number;
  chunk_index?: number;
  chunk_total?: number;
  done?: boolean;
  api?: { calls?: number; total_ms?: number; last_ms?: number; avg_ms?: number };
  current_chunk?: {
    chapter?: number;
    index?: number;
    source?: string;
    translated?: string;
    status?: string;
    attempt?: number;
    error?: string;
    diff?: { lines?: Array<{ kind: string; parts?: Array<{ op: string; text: string }> }>; added_words?: number; removed_words?: number };
    structure?: Record<string, unknown>;
  };
  [key: string]: unknown;
};

export type Job = {
  id: string;
  created_at: number;
  status: JobStatus;
  params: JobParams;
  progress: JobProgress;
  result: Record<string, unknown> | null;
  error: string | null;
  stop_requested: boolean;
};

export type ProviderInfo = {
  name: string;
  type: string;
  has_key: boolean;
  needs_key: boolean;
  ready: boolean;
  base_url: string;
  model: string;
  show_base: boolean;
  models_text: string;
  has_models: boolean;
  removable: boolean;
};

export type KeysPayload = {
  providers: ProviderInfo[];
  model_suggestions: Record<string, string[]>;
  engine_missing: string[];
  config_path: string;
};

export type ConfigField = {
  path: string;
  kind: "int" | "float" | "bool" | "str" | "text";
  value: unknown;
  extra: { min?: number; max?: number; step?: number; rows?: number };
  label: string;
  label_text: string;
};

export type ConfigGroup = {
  label: string;
  label_text: string;
  fields: ConfigField[];
};

export type SettingsPayload = {
  groups: ConfigGroup[];
  locale: string;
};

export type Illustration = { name: string; url: string };

export type ReaderChapter = {
  n: number;
  title: string;
  items: Array<{ type: "p" | "img"; text?: string; src?: string }>;
  chars: number;
  prev: number | null;
  next: number | null;
  total: number;
};

export type ReaderSearchHit = {
  query: string;
  total: number;
  truncated: boolean;
  chapters: Array<{
    n: number;
    title: string;
    count: number;
    hits: Array<{ off: number; snip: string }>;
  }>;
};

export type Annotation = {
  id?: string;
  ch: number;
  item?: number | null;
  quote: string;
  pre?: string;
  post?: string;
  color: "yellow" | "green" | "blue" | "pink";
  note?: string;
  created?: number;
  updated?: number;
};

export type TtsVoice = { id: string; label?: string; gender?: string; region?: string };

export type TtsStatus = {
  state: string;
  ready?: boolean;
  load_seconds?: number;
  error?: string | null;
  voices: TtsVoice[];
  default: string | null;
};

export type TtsCacheJob = {
  state: "running" | "done" | "stopped" | "error";
  book?: string;
  ch?: number;
  ch_from?: number;
  chapters?: number;
  ch_now?: number;
  ch_done?: number;
  pieces_done?: number;
  pieces_total?: number;
  chars_done?: number;
  chars_total?: number;
  eta?: number | null;
  elapsed?: number;
  error?: string | null;
};

export type TtsCacheStatus = {
  disk?: { bytes?: number; files?: number };
  job?: TtsCacheJob | null;
};

export function getJobs() {
  return request<{ jobs: Job[] }>("/api/v2/jobs");
}

export function createJob(values: Partial<JobParams> & { engine: string; input: string }) {
  return request<{ job: Job }>("/api/v2/jobs", {
    method: "POST",
    body: JSON.stringify(values),
  });
}

export function getJob(id: string) {
  return request<{ job: Job }>(`/api/v2/jobs/${encodeURIComponent(id)}`);
}

export function getJobState(id: string) {
  return request<{ progress: JobProgress; status: JobStatus; result: Record<string, unknown> | null; error: string | null }>(
    `/api/v2/jobs/${encodeURIComponent(id)}/state`,
  );
}

export function stopJob(id: string) {
  return request<{ job: Job }>(`/api/v2/jobs/${encodeURIComponent(id)}/stop`, { method: "POST" });
}

export function resumeJob(id: string) {
  return request<{ job: Job }>(`/api/v2/jobs/${encodeURIComponent(id)}/resume`, { method: "POST" });
}

export function updateJob(id: string, values: Partial<JobParams>) {
  return request<{ job: Job }>(`/api/v2/jobs/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify(values),
  });
}

export function getJobRules(id: string, chapter?: number | null) {
  const query = chapter ? `?chapter=${chapter}` : "";
  return request<Record<string, unknown>>(`/api/jobs/${encodeURIComponent(id)}/rules${query}`);
}

export function getKeys() {
  return request<KeysPayload>("/api/v2/config/keys");
}

export function saveKeys(providers: Array<Partial<ProviderInfo> & { name: string }>) {
  return request<KeysPayload>("/api/v2/config/keys", {
    method: "PUT",
    body: JSON.stringify({ providers }),
  });
}

export function addProvider(values: { name: string; base_url: string; api_key?: string; model?: string }) {
  return request<KeysPayload>("/api/v2/config/keys/providers", {
    method: "POST",
    body: JSON.stringify(values),
  });
}

export function removeProvider(id: string) {
  return request<KeysPayload>(`/api/v2/config/keys/providers/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

export function fetchModels(engine: string) {
  return request<{ models: string[]; source?: string; error?: string }>(
    `/api/v2/config/models?engine=${encodeURIComponent(engine)}`,
  );
}

export function getSettingsConfig() {
  return request<SettingsPayload>("/api/v2/config/settings");
}

export function saveSettingsConfig(values: Record<string, unknown>) {
  return request<SettingsPayload & { ok: boolean }>("/api/v2/config/settings", {
    method: "PUT",
    body: JSON.stringify({ values }),
  });
}

export function getAdvancedConfig() {
  return request<{ text: string; config_path: string }>("/api/v2/config");
}

export function saveAdvancedConfig(text: string) {
  return request<{ ok: boolean; text: string }>("/api/v2/config", {
    method: "PUT",
    body: JSON.stringify({ text }),
  });
}

export function testDiscord() {
  return request<{ success: boolean }>("/api/v2/config/test-discord", { method: "POST" });
}

export function getIllustrations() {
  return request<{ images: Illustration[]; dir: string }>("/api/v2/illustrations");
}

export function previewBook(path: string, chapter: number, maxChars = 4000) {
  const query = new URLSearchParams({ path, chapter: String(chapter), max_chars: String(maxChars) });
  return request<{ path: string; chapter: number; total: number; shown: number; text: string }>(
    `/api/v2/books/preview?${query.toString()}`,
  );
}

export function getReaderChapter(path: string, n: number) {
  return request<ReaderChapter>(`/api/v2/reader/chapter?${readerQuery(path, { n: String(n) })}`);
}

export function searchReaderBook(path: string, q: string, max = 300) {
  return request<ReaderSearchHit>(`/api/reader/search?${readerQuery(path, { q, max: String(max) })}`);
}

export function getAnnotations(path: string) {
  return request<{ anns: Annotation[] }>(`/api/reader/annotations?${readerQuery(path)}`);
}

export function createAnnotation(path: string, ann: Annotation) {
  return request<void>("/api/reader/annotations", {
    method: "POST",
    body: JSON.stringify({ path, ann }),
  });
}

export function updateAnnotation(path: string, ann: Annotation) {
  return request<void>("/api/reader/annotations", {
    method: "PUT",
    body: JSON.stringify({ path, ann }),
  });
}

export function deleteAnnotation(path: string, id: string) {
  return request<void>(`/api/reader/annotations/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

export function lookupReaderText(text: string, ctx = "") {
  return request<{ engine?: string; result?: string; error?: string }>(
    "/api/reader/lookup",
    { method: "POST", body: JSON.stringify({ text, ctx }) },
  );
}

export function getTtsStatus() {
  return request<TtsStatus>("/api/tts/voices");
}

export function getTtsCacheStatus() {
  return request<TtsCacheStatus>("/api/tts/cache/status");
}

export function inspectTtsCache(path: string, ch: number, voice?: string | null) {
  const query = new URLSearchParams({ path, ch: String(ch) });
  if (voice) query.set("voice", voice);
  return request<TtsCacheStatus & { ch: number; voice: string; pieces: number; cached: number }>(
    `/api/tts/cache/inspect?${query.toString()}`,
  );
}

export function startTtsCache(path: string, ch: number, chapters: number, voice?: string | null) {
  return request<{ job: TtsCacheJob }>("/api/tts/cache/start", {
    method: "POST",
    body: JSON.stringify({ path, ch, chapters, voice }),
  });
}

export function stopTtsCache() {
  return request<TtsCacheStatus>("/api/tts/cache/stop", { method: "POST" });
}

export function clearTtsCache() {
  return request<TtsCacheStatus>("/api/tts/cache/clear", { method: "POST" });
}

export async function synthesizeTts(text: string, voice?: string | null): Promise<{ blob: Blob; segs: number[][] }> {
  const response = await fetch("/api/tts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, voice }),
  });
  if (!response.ok) {
    const raw = await response.text();
    let message = `HTTP ${response.status}`;
    try {
      const data = JSON.parse(raw);
      if (data?.error) message = String(data.error);
    } catch {
      // keep HTTP message
    }
    throw new Error(message);
  }
  let segs: number[][] = [];
  try {
    segs = JSON.parse(response.headers.get("X-TTS-Segs") || "[]");
  } catch {
    segs = [];
  }
  return { blob: await response.blob(), segs };
}

export async function* synthesizeTtsStream(
  text: string,
  voice?: string | null,
  signal?: AbortSignal,
): AsyncGenerator<{ header: any; wav: Blob }> {
  const response = await fetch("/api/tts/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, voice }),
    signal,
  });
  if (!response.ok) {
    const raw = await response.text();
    let message = `HTTP ${response.status}`;
    try {
      const data = JSON.parse(raw);
      if (data?.error) message = String(data.error);
    } catch {
      // keep HTTP message
    }
    throw new Error(message);
  }

  const reader = response.body?.getReader();
  if (!reader) throw new Error("No response body");

  const decoder = new TextDecoder();
  let buffer = new Uint8Array(0);

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    const newBuffer = new Uint8Array(buffer.length + value.length);
    newBuffer.set(buffer);
    newBuffer.set(value, buffer.length);
    buffer = newBuffer;

    while (buffer.length >= 4) {
      const view = new DataView(buffer.buffer, buffer.byteOffset, buffer.byteLength);
      const headerLen = view.getUint32(0, false);

      if (buffer.length < 4 + headerLen) break;

      const headerJson = decoder.decode(buffer.subarray(4, 4 + headerLen));
      const header = JSON.parse(headerJson);

      const wavOffset = 4 + headerLen;
      if (buffer.length < wavOffset + 4) break;

      const wavLenView = new DataView(buffer.buffer, buffer.byteOffset + wavOffset, buffer.byteLength - wavOffset);
      const wavLen = wavLenView.getUint32(0, false);

      if (buffer.length < wavOffset + 4 + wavLen) break;

      const wavBytes = buffer.subarray(wavOffset + 4, wavOffset + 4 + wavLen);
      yield { header, wav: new Blob([wavBytes], { type: "audio/wav" }) };

      buffer = buffer.subarray(wavOffset + 4 + wavLen);
    }
  }
}

export function formatDuration(seconds?: number | null) {
  if (!seconds || !Number.isFinite(seconds)) return "—";
  const total = Math.max(0, Math.round(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h > 0
    ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
    : `${m}:${String(s).padStart(2, "0")}`;
}

export function formatBytes(bytes?: number | null) {
  if (!bytes || !Number.isFinite(bytes)) return "";
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}