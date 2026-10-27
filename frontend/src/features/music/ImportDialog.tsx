import { useEffect, useMemo, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { AlertCircle, CheckCircle2, Download, Loader2, X } from "lucide-react";
import { cancelImport, getImport, startImport, type ImportJob } from "../../lib/api";
import { copy, formatEta } from "../../lib/copy";
import { cn } from "../../lib/cn";

type Props = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onImported: () => void;
  onStarted?: () => void;
};

const ACTIVE_STATES = new Set(["queued", "fetching", "downloading", "processing", "cancelling"]);

function isActive(job: ImportJob | null) {
  return Boolean(job && ACTIVE_STATES.has(job.state));
}

function validUrl(value: string) {
  try {
    const url = new URL(value);
    if (!["http:", "https:"].includes(url.protocol)) return false;
    return [
      "youtube.com",
      "www.youtube.com",
      "m.youtube.com",
      "music.youtube.com",
      "youtu.be",
      "www.youtube-nocookie.com",
    ].includes(url.hostname.toLowerCase());
  } catch {
    return false;
  }
}

export function ImportDialog({ open, onOpenChange, onImported, onStarted }: Props) {
  const [url, setUrl] = useState("");
  const [job, setJob] = useState<ImportJob | null>(null);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [imported, setImported] = useState(false);

  const active = isActive(job);
  const valid = useMemo(() => validUrl(url.trim()), [url]);

  useEffect(() => {
    if (!open) return;
    setError("");
  }, [open]);

  useEffect(() => {
    if (!job || !isActive(job)) return;
    let alive = true;
    const jobId = job.id;
    const timer = window.setInterval(async () => {
      try {
        const next = (await getImport(jobId)).job;
        if (!alive) return;
        setJob(next);
        if (next.state === "done") {
          setImported(true);
          onImported();
        }
      } catch (err) {
        if (alive) setError(err instanceof Error ? err.message : String(err));
      }
    }, 700);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [job?.id, job?.state, onImported]);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!valid || submitting) {
      setError("Link chưa đúng. Chỉ nhận link YouTube / YouTube Music / youtu.be.");
      return;
    }
    setSubmitting(true);
    setError("");
    setImported(false);
    try {
      const result = await startImport(url.trim());
      setJob(result.job);
      onStarted?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  }

  async function cancel() {
    if (!job) return;
    try {
      const result = await cancelImport(job.id);
      setJob(result.job);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  function close(next: boolean) {
    if (!next) {
      setJob(null);
      setUrl("");
      setError("");
      setImported(false);
    }
    onOpenChange(next);
  }

  const progress = Math.max(0, Math.min(100, job?.progress ?? 0));
  const stateLabel: Record<string, string> = {
    queued: "Đang chờ…",
    fetching: "Đang lấy thông tin video…",
    downloading: "Đang tải audio…",
    processing: "Đang chuyển sang MP3…",
    cancelling: "Đang huỷ…",
    done: "Đã tải xong",
    error: "Tải thất bại",
    cancelled: "Đã huỷ",
  };

  return (
    <Dialog.Root open={open} onOpenChange={close}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm data-[state=open]:animate-in" />
        <Dialog.Content
          className="fixed inset-x-0 bottom-0 z-50 max-h-[92dvh] overflow-y-auto rounded-t-3xl border border-line bg-surface p-5 shadow-2xl outline-none data-[state=open]:animate-in sm:inset-x-auto sm:bottom-auto sm:left-1/2 sm:top-1/2 sm:w-[min(34rem,calc(100vw-2rem))] sm:-translate-x-1/2 sm:-translate-y-1/2 sm:rounded-3xl"
          aria-describedby="import-description"
        >
          <div className="flex items-start justify-between gap-4">
            <div>
              <Dialog.Title className="text-lg font-semibold text-ink">{copy.music.importTitle}</Dialog.Title>
              <Dialog.Description id="import-description" className="mt-1 text-sm leading-6 text-muted">
                {copy.music.importHint}
              </Dialog.Description>
            </div>
            <Dialog.Close asChild>
              <button className="focus-ring flex h-10 w-10 shrink-0 items-center justify-center rounded-xl text-muted hover:bg-surface-2 hover:text-ink" aria-label={copy.music.close}>
                <X className="h-5 w-5" />
              </button>
            </Dialog.Close>
          </div>

          <form className="mt-5 space-y-4" onSubmit={submit}>
            <label className="block space-y-2">
              <span className="label">{copy.music.url}</span>
              <input
                className="input"
                type="url"
                inputMode="url"
                autoComplete="off"
                spellCheck={false}
                placeholder={copy.music.urlPlaceholder}
                value={url}
                onChange={(event) => {
                  setUrl(event.target.value);
                  setError("");
                }}
                disabled={active || imported}
              />
            </label>

            {!job ? (
              <button
                type="submit"
                className="btn btn-primary w-full"
                disabled={!valid || submitting}
              >
                {submitting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Download className="h-4 w-4" />}
                {submitting ? copy.music.downloading : copy.music.download}
              </button>
            ) : null}
          </form>

          {job ? (
            <div className="mt-5 rounded-2xl border border-line bg-surface-2 p-4">
              <div className="flex items-center gap-3">
                {job.state === "done" ? (
                  <CheckCircle2 className="h-5 w-5 shrink-0 text-accent" />
                ) : job.state === "error" || job.state === "cancelled" ? (
                  <AlertCircle className="h-5 w-5 shrink-0 text-danger" />
                ) : (
                  <Loader2 className="h-5 w-5 shrink-0 animate-spin text-accent" />
                )}
                <div className="min-w-0 flex-1">
                  <div className="flex items-center justify-between gap-3">
                    <span className="truncate text-sm font-semibold text-ink">
                      {stateLabel[job.state] || job.state}
                    </span>
                    {active ? <span className="shrink-0 text-xs tabular-nums text-muted">{Math.round(progress)}%</span> : null}
                  </div>
                  <div className="mt-1 truncate text-xs text-muted">
                    {job.track?.title || job.title || job.url}
                  </div>
                </div>
              </div>

              {active ? (
                <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-surface-3">
                  <div className="h-full rounded-full bg-accent transition-[width] duration-300" style={{ width: `${progress}%` }} />
                </div>
              ) : null}

              {active && (job.speed || job.eta) ? (
                <div className="mt-2 flex gap-4 text-xs text-faint">
                  {job.speed ? <span>{formatBytesPerSecond(job.speed)}</span> : null}
                  {job.eta ? <span>còn {formatEta(job.eta)}</span> : null}
                </div>
              ) : null}

              {job.error ? <p className="mt-3 break-words text-sm text-danger">{job.error}</p> : null}

              <div className="mt-4 flex flex-wrap gap-2">
                {active ? (
                  <button type="button" className="btn btn-danger" onClick={cancel} disabled={job.state === "cancelling"}>
                    {copy.music.cancel}
                  </button>
                ) : null}
                {job.state === "error" || job.state === "cancelled" ? (
                  <button
                    type="button"
                    className="btn btn-secondary"
                    onClick={() => {
                      setJob(null);
                      setError("");
                    }}
                  >
                    {copy.music.retry}
                  </button>
                ) : null}
                <button type="button" className="btn btn-secondary" onClick={() => close(false)}>
                  {copy.music.close}
                </button>
              </div>
            </div>
          ) : null}

          {error ? (
            <div className="mt-4 flex items-start gap-2 rounded-xl border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-300">
              <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
              <span className="break-words">{error}</span>
            </div>
          ) : null}

          {imported ? (
            <div className="mt-4 rounded-xl border border-accent/30 bg-accent-soft p-3 text-sm text-accent">
              {copy.music.importDone}
            </div>
          ) : null}

          <p className={cn("mt-4 text-xs leading-5 text-faint", !valid && url ? "text-warning" : "")}>
            Chỉ tải video/audio bạn có quyền sử dụng. File được lưu trong thư mục <code>music/</code>.
          </p>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function formatBytesPerSecond(value: number) {
  if (!Number.isFinite(value)) return "";
  if (value < 1024 * 1024) return `${Math.round(value / 1024)} KB/s`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB/s`;
}
