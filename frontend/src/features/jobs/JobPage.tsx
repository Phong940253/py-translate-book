import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  ArrowLeft,
  ChevronRight,
  Download,
  Pencil,
  Loader2,
  Play,
  RotateCcw,
  Square,
  Terminal,
  Workflow,
} from "lucide-react";
import {
  getJob,
  getJobRules,
  getJobState,
  resumeJob,
  stopJob,
  updateJob,
  getTtsStatus,
  type JobParams,
  type Job,
  type JobProgress,
  type JobStatus,
} from "../../lib/api";
import { cn } from "../../lib/cn";

const terminal = new Set<JobStatus>(["done", "error", "stopped", "interrupted"]);

const statusLabel: Record<JobStatus, string> = {
  queued: "Đang chờ",
  running: "Đang dịch",
  done: "Hoàn tất",
  error: "Lỗi",
  stopped: "Đã dừng",
  interrupted: "Bị gián đoạn",
};

const statusStyle: Record<JobStatus, string> = {
  queued: "border-sky-500/30 bg-sky-500/10 text-sky-300",
  running: "border-amber-500/30 bg-amber-500/10 text-amber-300",
  done: "border-emerald-500/30 bg-emerald-500/10 text-emerald-300",
  error: "border-red-500/30 bg-red-500/10 text-red-300",
  stopped: "border-line bg-surface-2 text-muted",
  interrupted: "border-orange-500/30 bg-orange-500/10 text-orange-300",
};

function statusTone(status: JobStatus) {
  return statusStyle[status] || statusStyle.queued;
}

function asNumber(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function percent(progress: JobProgress | undefined) {
  if (!progress?.total_chapters) return 0;
  const done = asNumber(progress.last_completed);
  return Math.max(0, Math.min(100, Math.round((done / asNumber(progress.total_chapters)) * 100)));
}

function JsonBlock({ value }: { value: unknown }) {
  return (
    <pre className="max-h-[28rem] overflow-auto whitespace-pre-wrap break-words rounded-xl bg-surface-2 p-4 font-mono text-[11px] leading-5 text-muted">
      {JSON.stringify(value ?? null, null, 2)}
    </pre>
  );
}

type RuleCharacter = {
  names?: string[];
  calls?: Record<string, string>;
  pronouns?: Record<string, string>;
  notes?: string;
};

type RulePayload = {
  characters?: Record<string, RuleCharacter>;
  narration_defaults?: Record<string, string>;
  fallback?: string;
  analyses?: Record<string, string>;
  meta?: { chapter?: number | null; total_characters?: number; returned_characters?: number; has_analysis?: boolean };
};

function RulesView({ job }: { job: Job }) {
  const [chapter, setChapter] = useState("");
  const [scope, setScope] = useState<number | null>(null);
  const rules = useQuery({
    queryKey: ["job-rules", job.id, scope],
    queryFn: () => getJobRules(job.id, scope),
    staleTime: 30_000,
  });
  const data = (rules.data || {}) as RulePayload;
  const characters = data.characters || {};
  const analyses = data.analyses || {};

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-2">
        <label className="min-w-32 flex-1 space-y-1 sm:max-w-44">
          <span className="label">Lọc theo chương</span>
          <input className="input min-h-9 py-1.5 text-xs" type="number" min={1} value={chapter} onChange={(event) => setChapter(event.target.value)} placeholder="Tất cả" />
        </label>
        <button className="btn btn-secondary min-h-9 px-3 text-xs" onClick={() => setScope(chapter ? Number(chapter) : null)}>
          Áp dụng
        </button>
        <button className="btn btn-ghost min-h-9 px-3 text-xs" onClick={() => { setChapter(""); setScope(null); }}>
          Tất cả
        </button>
        {rules.isFetching ? <Loader2 className="mb-2 h-4 w-4 animate-spin text-accent" /> : null}
        <span className="mb-2 text-xs text-faint">
          {data.meta?.returned_characters ?? 0}/{data.meta?.total_characters ?? 0} nhân vật
          {scope ? ` · chương ${scope}` : ""}
        </span>
      </div>
      {rules.isError ? <p className="text-sm text-danger">{rules.error instanceof Error ? rules.error.message : "Không tải được rules."}</p> : null}
      {!rules.isLoading && !Object.keys(characters).length ? (
        <p className="rounded-xl border border-dashed border-line p-6 text-center text-sm text-muted">
          Chưa có rules. Rules được tạo dần khi các chương hoàn tất.
        </p>
      ) : null}
      {Object.keys(characters).length ? (
        <div className="grid gap-3 sm:grid-cols-2">
          {Object.entries(characters).map(([name, entry]) => (
            <article key={name} className="rounded-xl border border-line bg-surface-2/50 p-3">
              <h3 className="text-sm font-semibold text-ink">{name}</h3>
              {entry.names?.length ? <p className="mt-1 text-xs text-faint">Tên khác: {entry.names.join(", ")}</p> : null}
              {entry.calls && Object.keys(entry.calls).length ? (
                <ul className="mt-2 space-y-1 text-xs text-muted">
                  {Object.entries(entry.calls).map(([target, call]) => (
                    <li key={target}><span className="text-ink">{target}</span>: {call}</li>
                  ))}
                </ul>
              ) : null}
              {entry.notes ? <p className="mt-2 border-t border-line pt-2 text-xs leading-5 text-muted">{entry.notes}</p> : null}
            </article>
          ))}
        </div>
      ) : null}
      {data.narration_defaults && Object.keys(data.narration_defaults).length ? (
        <section>
          <h3 className="mb-2 text-sm font-semibold text-ink">Narration defaults</h3>
          <div className="space-y-1 rounded-xl border border-line p-3 text-xs text-muted">
            {Object.entries(data.narration_defaults).map(([key, value]) => <p key={key}><span className="text-ink">{key}</span> → {value}</p>)}
          </div>
        </section>
      ) : null}
      {data.fallback ? (
        <section>
          <h3 className="mb-2 text-sm font-semibold text-ink">Fallback</h3>
          <pre className="whitespace-pre-wrap rounded-xl border border-line p-3 text-xs text-muted">{data.fallback}</pre>
        </section>
      ) : null}
      {Object.keys(analyses).length ? (
        <section className="space-y-2">
          <h3 className="text-sm font-semibold text-ink">Phân tích chương ({Object.keys(analyses).length})</h3>
          {Object.entries(analyses).sort(([a], [b]) => Number(a) - Number(b)).map(([n, text]) => (
            <details key={n} className="rounded-xl border border-line bg-surface-2/40">
              <summary className="cursor-pointer px-3 py-2 text-sm text-ink">Chương {n}</summary>
              <pre className="max-h-72 overflow-auto whitespace-pre-wrap border-t border-line p-3 text-xs leading-5 text-muted">{text}</pre>
            </details>
          ))}
        </section>
      ) : null}
    </div>
  );
}

export function JobPage() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const jobQuery = useQuery({ queryKey: ["job", id], queryFn: () => getJob(id), enabled: Boolean(id) });
  const tts = useQuery({ queryKey: ["tts-status"], queryFn: getTtsStatus, staleTime: 60_000 });
  const [job, setJob] = useState<Job | null>(null);
  const [logs, setLogs] = useState<string[]>([]);
  const [connection, setConnection] = useState<"connecting" | "live" | "polling">("connecting");
  const [streamEpoch, setStreamEpoch] = useState(0);
  const [tab, setTab] = useState<"struct" | "diff" | "side" | "rules">("struct");
  const [editing, setEditing] = useState(false);
  const [editParams, setEditParams] = useState<Partial<JobParams>>({});
  const logRef = useRef<HTMLPreElement>(null);

  useEffect(() => setJob(jobQuery.data?.job ?? null), [jobQuery.data?.job]);

  useEffect(() => {
    if (!id) return;
    setLogs([]);
    setConnection("connecting");
    let alive = true;
    const source = new EventSource(`/jobs/${encodeURIComponent(id)}/stream`);

    source.onopen = () => {
      if (alive) setConnection("live");
    };
    source.onerror = () => {
      if (alive) setConnection("polling");
    };
    source.onmessage = (event) => {
      if (!alive) return;
      let payload: Record<string, unknown>;
      try {
        payload = JSON.parse(event.data);
      } catch {
        return;
      }
      const type = payload.type;
      if (type === "log_batch" && Array.isArray(payload.lines)) {
        const lines = payload.lines.map(String);
        setLogs((prev) => [...prev, ...lines].slice(-1000));
      } else if (type === "log" && typeof payload.line === "string") {
        setLogs((prev) => [...prev, payload.line as string].slice(-1000));
      } else if (type === "progress") {
        setJob((prev) => prev ? {
          ...prev,
          status: (payload.status as JobStatus) || prev.status,
          progress: (payload.progress as JobProgress) || prev.progress,
          result: (payload.result as Record<string, unknown> | null) ?? prev.result,
          error: (payload.error as string | null) ?? prev.error,
        } : prev);
      } else if (type === "end") {
        const status = payload.status as JobStatus;
        setJob((prev) => prev ? { ...prev, status } : prev);
        setConnection("polling");
        source.close();
        void queryClient.invalidateQueries({ queryKey: ["job", id] });
      }
    };

    return () => {
      alive = false;
      source.close();
    };
  }, [id, queryClient, streamEpoch]);

  useEffect(() => {
    if (!id) return;
    const timer = window.setInterval(async () => {
      try {
        const state = await getJobState(id);
        setJob((prev) => prev ? {
          ...prev,
          status: state.status,
          progress: state.progress,
          result: state.result,
          error: state.error,
        } : prev);
        if (terminal.has(state.status)) window.clearInterval(timer);
      } catch {
        setConnection("polling");
      }
    }, 4000);
    return () => window.clearInterval(timer);
  }, [id]);

  useEffect(() => {
    const el = logRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    if (nearBottom) el.scrollTop = el.scrollHeight;
  }, [logs]);

  const stop = useMutation({
    mutationFn: () => stopJob(id),
    onSuccess: ({ job: next }) => {
      setJob(next);
      void queryClient.invalidateQueries({ queryKey: ["job", id] });
    },
  });
  const resume = useMutation({
    mutationFn: () => resumeJob(id),
    onSuccess: ({ job: next }) => {
      setJob(next);
      setLogs((prev) => [...prev, "— Job được tiếp tục —"]);
      setConnection("connecting");
      setStreamEpoch((value) => value + 1);
      void queryClient.invalidateQueries({ queryKey: ["job", id] });
    },
  });
  const edit = useMutation({
    mutationFn: () => updateJob(id, editParams),
    onSuccess: ({ job: next }) => {
      setJob(next);
      setEditing(false);
      void queryClient.invalidateQueries({ queryKey: ["job", id] });
    },
  });

  const progress = job?.progress || {};
  const isTerminal = job ? terminal.has(job.status) : false;
  const pct = percent(progress);
  const currentChunk = progress.current_chunk;
  const diff = currentChunk?.diff;
  const structure = currentChunk?.structure || {};
  const canEdit = job ? ["stopped", "interrupted", "error"].includes(job.status) : false;

  const chapterText = useMemo(() => {
    if (!progress.total_chapters) return "Chưa bắt đầu";
    const current = progress.current_chapter || progress.last_completed || "?";
    return `Chương ${current} / ${progress.total_chapters}${progress.current_title ? ` — ${progress.current_title}` : ""}`;
  }, [progress.current_chapter, progress.current_title, progress.last_completed, progress.total_chapters]);

  if (jobQuery.isLoading && !job) {
    return <div className="flex items-center gap-3 p-8 text-sm text-muted"><Loader2 className="h-4 w-4 animate-spin text-accent" /> Đang tải job…</div>;
  }
  if (!job) {
    return (
      <div className="card p-8 text-center">
        <p className="text-sm text-danger">{jobQuery.error instanceof Error ? jobQuery.error.message : "Không tìm thấy job."}</p>
        <Link to="/" className="btn btn-primary mt-4">Về tổng quan</Link>
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <button className="btn btn-ghost -ml-2 mb-2 min-h-9 px-2 text-xs" onClick={() => navigate(-1)}>
            <ArrowLeft className="h-4 w-4" /> Quay lại
          </button>
          <div className="flex flex-wrap items-center gap-3">
            <h1 className="text-2xl font-bold tracking-tight text-ink">Job dịch</h1>
            <span className="font-mono text-sm text-muted">{job.id}</span>
            <span className={cn("rounded-full border px-2.5 py-1 text-xs font-medium", statusTone(job.status))}>
              {statusLabel[job.status] || job.status}
            </span>
            <span className="text-[11px] text-faint">
              {connection === "live" ? "SSE trực tiếp" : connection === "polling" ? "Đang đồng bộ định kỳ" : "Đang kết nối…"}
            </span>
          </div>
          <p className="mt-2 text-sm text-muted">{chapterText}</p>
        </div>
        <div className="flex flex-wrap gap-2">
          {canEdit ? (
            <button
              className="btn btn-secondary"
              onClick={() => {
                setEditParams({ ...job.params });
                setEditing((value) => !value);
              }}
            >
              <Pencil className="h-4 w-4" /> Chỉnh thuộc tính
            </button>
          ) : null}
          {job.status === "running" || job.status === "queued" ? (
            <button className="btn btn-danger" onClick={() => stop.mutate()} disabled={stop.isPending}>
              {stop.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Square className="h-4 w-4" />}
              Dừng job
            </button>
          ) : null}
          {job.status === "stopped" || job.status === "interrupted" || job.status === "error" ? (
            <button className="btn btn-primary" onClick={() => resume.mutate()} disabled={resume.isPending}>
              {resume.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <RotateCcw className="h-4 w-4" />}
              Tiếp tục
            </button>
          ) : null}
          {job.status === "done" && job.params.output ? (
            <a className="btn btn-primary" href={`/download?path=${encodeURIComponent(job.params.output)}`}>
              <Download className="h-4 w-4" /> Tải EPUB
            </a>
          ) : null}
        </div>
      </div>

      {(stop.error || resume.error) ? <p className="text-sm text-danger">{String((stop.error || resume.error) instanceof Error ? (stop.error || resume.error)?.message : "")}</p> : null}
      {editing ? (
        <section className="card space-y-4 p-5">
          <div>
            <h2 className="text-sm font-semibold text-ink">Chỉnh thuộc tính job</h2>
            <p className="mt-1 text-xs text-faint">Chỉ áp dụng khi bấm “Tiếp tục”; job đang chạy không thể chỉnh.</p>
          </div>
          <div className="grid gap-4 sm:grid-cols-2">
            {([
              ["input", "EPUB nguồn"],
              ["output", "EPUB đầu ra"],
              ["config_path", "Config path"],
              ["engine", "AI / Engine"],
              ["model", "Model"],
              ["description", "Mô tả"],
              ["from_lang", "Ngôn ngữ nguồn"],
              ["to_lang", "Ngôn ngữ đích"],
              ["from_chapter", "Chương bắt đầu"],
              ["to_chapter", "Chương kết thúc"],
            ] as const).map(([key, label]) => (
              <label key={key} className="space-y-1">
                <span className="label">{label}</span>
                <input
                  className="input"
                  type={key.includes("chapter") ? "number" : "text"}
                  value={editParams[key] ?? ""}
                  onChange={(event) => setEditParams((prev) => ({
                    ...prev,
                    [key]: key.includes("chapter")
                      ? (event.target.value ? Number(event.target.value) : null)
                      : event.target.value,
                  }))}
                />
              </label>
            ))}
          </div>
          <label className="flex items-start gap-3 text-sm">
            <input
              type="checkbox"
              className="mt-1 h-4 w-4 accent-[var(--color-accent)]"
              checked={Boolean(editParams.tts_disk_cache)}
              onChange={(event) => setEditParams((prev) => ({ ...prev, tts_disk_cache: event.target.checked }))}
            />
            <span><span className="font-medium text-ink">Tạo disk cache TTS theo từng chapter</span><span className="mt-0.5 block text-xs text-faint">Chapter nào dịch xong sẽ cache chapter đó.</span></span>
          </label>
          <label className="space-y-1">
            <span className="label">Giọng TTS</span>
            <select
              className="input"
              value={editParams.tts_voice ?? ""}
              onChange={(event) => setEditParams((prev) => ({
                ...prev,
                tts_voice: event.target.value || null,
              }))}
            >
              <option value="">Mặc định của TTS</option>
              {tts.data?.voices.map((voice) => (
                <option key={voice.id} value={voice.id}>{voice.label || voice.id}</option>
              ))}
            </select>
          </label>
          <div className="grid gap-2 sm:grid-cols-3">
            {([
              ["openai_batch", "OpenAI Batch API"],
              ["reset_checkpoint", "Xoá checkpoint khi chạy lại"],
              ["disable_resume", "Tắt resume tự động"],
            ] as const).map(([key, label]) => (
              <label key={key} className="flex items-start gap-2 text-xs text-muted">
                <input
                  type="checkbox"
                  className="mt-0.5 h-4 w-4 accent-[var(--color-accent)]"
                  checked={Boolean(editParams[key])}
                  onChange={(event) => setEditParams((prev) => ({ ...prev, [key]: event.target.checked }))}
                />
                <span>{label}</span>
              </label>
            ))}
          </div>
          {edit.error ? <p className="text-sm text-danger">{edit.error instanceof Error ? edit.error.message : "Không lưu được."}</p> : null}
          <div className="flex gap-2">
            <button className="btn btn-primary" onClick={() => edit.mutate()} disabled={edit.isPending}>
              {edit.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : null} Lưu thay đổi
            </button>
            <button className="btn btn-ghost" onClick={() => setEditing(false)}>Huỷ</button>
          </div>
        </section>
      ) : null}

      <section className="card p-5">
        <div className="flex items-center justify-between gap-3">
          <h2 className="text-sm font-semibold text-ink">Tiến độ</h2>
          <span className="text-xs tabular-nums text-muted">{pct}%</span>
        </div>
        <div className="mt-3 h-2 overflow-hidden rounded-full bg-surface-2" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct}>
          <div className="h-full rounded-full bg-accent transition-all" style={{ width: `${pct}%` }} />
        </div>
        <div className="mt-4 grid gap-3 text-xs text-muted sm:grid-cols-3">
          <div><span className="label block">API</span>{progress.api ? `${progress.api.calls || 0} call · ${((progress.api.total_ms || 0) / 1000).toFixed(1)}s` : "—"}</div>
          <div><span className="label block">Chunk</span>{progress.chunk_total ? `${progress.chunk_index || "?"} / ${progress.chunk_total}` : "—"}</div>
          <div><span className="label block">Chương đã xong</span>{progress.last_completed || 0} / {progress.total_chapters || "—"}</div>
        </div>
        {progress.error ? <p className="mt-3 text-xs text-danger">{String(progress.error)}</p> : null}
      </section>

      <section className="card p-5">
        <div className="flex flex-wrap items-center gap-2 border-b border-line pb-3">
          {[
            ["struct", "Cấu trúc"],
            ["diff", "Diff"],
            ["side", "Song song"],
            ["rules", "Rules"],
          ].map(([key, label]) => (
            <button
              key={key}
              className={cn("btn min-h-9 px-3 text-xs", tab === key ? "btn-primary" : "btn-ghost")}
              onClick={() => setTab(key as typeof tab)}
              aria-pressed={tab === key}
            >
              {label}
            </button>
          ))}
          {currentChunk?.status === "retry" ? <span className="text-xs text-warning">Thử lại lần {currentChunk.attempt}: {currentChunk.error}</span> : null}
          {currentChunk?.status === "failed" ? <span className="text-xs text-danger">Chunk lỗi: {currentChunk.error}</span> : null}
        </div>
        <div className="pt-4">
          {tab === "rules" ? <RulesView job={job} /> : null}
          {tab === "struct" ? (
            Object.keys(structure).length ? <JsonBlock value={structure} /> : <p className="text-sm text-muted">Chưa có dữ liệu cấu trúc của chunk hiện tại.</p>
          ) : null}
          {tab === "diff" ? (
            diff?.lines?.length ? (
              <div className="overflow-auto rounded-xl bg-surface-2 p-3 font-mono text-xs leading-6">
                <div className="mb-2 text-[11px] text-faint">+{diff.added_words || 0} từ · −{diff.removed_words || 0} từ</div>
                {diff.lines.map((line, index) => (
                  <div key={index} className={line.kind === "ins" ? "text-emerald-300" : line.kind === "del" ? "text-red-300" : "text-muted"}>
                    {line.parts?.map((part, partIndex) => <span key={partIndex} className={part.op === "ins" ? "bg-emerald-500/15" : part.op === "del" ? "bg-red-500/15" : ""}>{part.text}</span>)}
                  </div>
                ))}
              </div>
            ) : <p className="text-sm text-muted">Chưa có diff của chunk hiện tại.</p>
          ) : null}
          {tab === "side" ? (
            <div className="grid gap-3 lg:grid-cols-2">
              <div><p className="label mb-2">Nguồn</p><pre className="max-h-96 overflow-auto whitespace-pre-wrap rounded-xl bg-surface-2 p-3 text-xs leading-5 text-muted">{currentChunk?.source || "—"}</pre></div>
              <div><p className="label mb-2">Bản dịch</p><pre className="max-h-96 overflow-auto whitespace-pre-wrap rounded-xl bg-surface-2 p-3 text-xs leading-5 text-muted">{currentChunk?.translated || "—"}</pre></div>
            </div>
          ) : null}
        </div>
      </section>

      <section className="card p-5">
        <div className="mb-3 flex items-center gap-2 border-b border-line pb-3">
          <Terminal className="h-4 w-4 text-accent" />
          <h2 className="text-sm font-semibold text-ink">Log</h2>
          <span className="ml-auto text-[11px] text-faint">{logs.length} dòng gần nhất</span>
        </div>
        <pre ref={logRef} className="h-80 overflow-auto whitespace-pre-wrap break-words rounded-xl bg-black/30 p-4 font-mono text-[11px] leading-5 text-muted">
          {logs.length ? logs.join("\n") : "Đang chờ log…"}
        </pre>
      </section>

      <section className="card p-5">
        <h2 className="mb-3 text-sm font-semibold text-ink">Thông số job</h2>
        <dl className="grid gap-x-6 gap-y-3 text-xs sm:grid-cols-2">
          {[
            ["Engine", job.params.engine],
            ["Model", job.params.model || "mặc định"],
            ["Input", job.params.input],
            ["Output", job.params.output],
            ["Khoảng chương", `${job.params.from_chapter || 1} – ${job.params.to_chapter || "hết"}`],
            ["Ngôn ngữ", `${job.params.from_lang || "EN"} → ${job.params.to_lang || "VI"}`],
            ["Config", job.params.config_path],
            ["Tạo lúc", job.created_at ? new Date(job.created_at * 1000).toLocaleString("vi-VN") : "—"],
          ].map(([label, value]) => (
            <div key={label} className="min-w-0">
              <dt className="label">{label}</dt>
              <dd className="mt-1 break-all font-mono text-muted">{value || "—"}</dd>
            </div>
          ))}
        </dl>
        {job.result ? <div className="mt-4 border-t border-line pt-4"><p className="label mb-2">Kết quả</p><JsonBlock value={job.result} /></div> : null}
        {job.error ? <p className="mt-4 rounded-xl border border-red-500/30 bg-red-500/10 p-3 text-xs text-red-300">{job.error}</p> : null}
      </section>

      <div className="flex justify-end">
        <Link to="/" className="btn btn-ghost text-xs"><Workflow className="h-4 w-4" /> Tổng quan <ChevronRight className="h-4 w-4" /></Link>
      </div>
    </div>
  );
}
