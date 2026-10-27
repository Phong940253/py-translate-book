import { useCallback, useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  Clock3,
  Download,
  ListMusic,
  Loader2,
  Pause,
  Play,
  Plus,
  Search,
  SkipBack,
  SkipForward,
  Trash2,
  Volume2,
} from "lucide-react";
import {
  cancelImport,
  deleteTrack,
  formatBytes,
  formatDuration,
  getMusic,
  type ImportJob,
  type Track,
} from "../../lib/api";
import { copy, formatEta } from "../../lib/copy";
import { cn } from "../../lib/cn";
import { ImportDialog } from "./ImportDialog";
import { currentTrack, usePlayerStore } from "./playerStore";

const ACTIVE_STATES = new Set(["queued", "fetching", "downloading", "processing", "cancelling"]);

function activeJob(job: ImportJob | null | undefined) {
  return Boolean(job && ACTIVE_STATES.has(job.state));
}

export function MusicPage() {
  const queryClient = useQueryClient();
  const [query, setQuery] = useState("");
  const [importOpen, setImportOpen] = useState(false);
  const [notice, setNotice] = useState("");
  const tracks = usePlayerStore((state) => state.tracks);
  const setTracks = usePlayerStore((state) => state.setTracks);
  const playing = usePlayerStore((state) => state.playing);
  const volume = usePlayerStore((state) => state.volume);
  const loop = usePlayerStore((state) => state.loop);
  const current = usePlayerStore(currentTrack);
  const playTrack = usePlayerStore((state) => state.playTrack);
  const toggle = usePlayerStore((state) => state.toggle);
  const next = usePlayerStore((state) => state.next);
  const prev = usePlayerStore((state) => state.prev);
  const setVolume = usePlayerStore((state) => state.setVolume);
  const setLoop = usePlayerStore((state) => state.setLoop);
  const removeFromPlayer = usePlayerStore((state) => state.removeTrack);

  const musicQuery = useQuery({
    queryKey: ["music"],
    queryFn: getMusic,
    refetchInterval: (queryState) => (activeJob(queryState.state.data?.import) ? 800 : false),
  });

  useEffect(() => {
    if (musicQuery.data?.tracks) setTracks(musicQuery.data.tracks);
  }, [musicQuery.data?.tracks, setTracks]);

  const cancelMutation = useMutation({
    mutationFn: (jobId: string) => cancelImport(jobId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["music"] });
      setNotice("Đã yêu cầu huỷ.");
    },
    onError: (error) => setNotice(error instanceof Error ? error.message : String(error)),
  });

  const deleteMutation = useMutation({
    mutationFn: (track: Track) => deleteTrack(track.id),
    onSuccess: (_data, track) => {
      removeFromPlayer(track.id);
      void queryClient.invalidateQueries({ queryKey: ["music"] });
      setNotice("Đã xoá khỏi thư viện nhạc.");
    },
    onError: (error) => setNotice(error instanceof Error ? error.message : String(error)),
  });

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return tracks;
    return tracks.filter((track) =>
      `${track.title} ${track.artist}`.toLowerCase().includes(needle),
    );
  }, [tracks, query]);

  const totalMinutes = Math.round(
    tracks.reduce((sum, track) => sum + (track.duration || 0), 0) / 60,
  );
  const job = musicQuery.data?.import;
  const importing = activeJob(job);
  const refreshMusic = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ["music"] });
  }, [queryClient]);

  return (
    <div className="space-y-6 pb-10">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
        <div className="min-w-0">
          <p className="label">Background audio</p>
          <h1 className="mt-2 text-3xl font-bold tracking-tight text-ink sm:text-4xl">{copy.music.title}</h1>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-muted">{copy.music.subtitle}</p>
        </div>
        <button className="btn btn-primary shrink-0" onClick={() => setImportOpen(true)} disabled={importing}>
          <Plus className="h-4 w-4" />
          {importing ? "Đang tải…" : copy.music.import}
        </button>
      </div>

      {notice ? (
        <div className="flex items-start gap-2 rounded-xl border border-line bg-surface-2 px-4 py-3 text-sm text-muted">
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-accent" />
          <span className="min-w-0 break-words">{notice}</span>
          <button className="ml-auto shrink-0 text-xs font-semibold text-faint hover:text-ink" onClick={() => setNotice("")}>
            Đóng
          </button>
        </div>
      ) : null}

      {activeJob(job) ? <ImportProgress job={job!} onCancel={() => cancelMutation.mutate(job!.id)} canceling={cancelMutation.isPending} /> : null}

      <section className="grid gap-5 lg:grid-cols-[minmax(0,1.15fr)_minmax(20rem,0.85fr)]">
        <div className="card min-w-0 overflow-hidden">
          <div className="flex flex-col gap-3 border-b border-line p-5 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex items-center gap-2">
              <ListMusic className="h-5 w-5 text-accent" />
              <h2 className="text-base font-semibold text-ink">{copy.music.library}</h2>
              <span className="rounded-full bg-surface-2 px-2 py-0.5 text-xs text-muted">{tracks.length}</span>
            </div>
            <div className="flex items-center gap-2 text-xs text-faint">
              <Clock3 className="h-3.5 w-3.5" />
              {totalMinutes} phút
            </div>
          </div>

          <div className="border-b border-line p-4">
            <div className="relative">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-faint" />
              <input
                className="input pl-9"
                type="search"
                placeholder={copy.music.search}
                value={query}
                onChange={(event) => setQuery(event.target.value)}
              />
            </div>
          </div>

          {musicQuery.isLoading ? (
            <div className="flex items-center gap-3 p-8 text-sm text-muted">
              <Loader2 className="h-4 w-4 animate-spin text-accent" />
              Đang tải thư viện nhạc…
            </div>
          ) : musicQuery.isError ? (
            <div className="p-8 text-sm text-danger">
              {musicQuery.error instanceof Error ? musicQuery.error.message : "Không tải được thư viện nhạc."}
            </div>
          ) : !tracks.length ? (
            <div className="p-8 text-center">
              <ListMusic className="mx-auto h-9 w-9 text-faint" />
              <p className="mt-3 text-sm text-muted">{copy.music.empty}</p>
              <button className="btn btn-secondary mt-4" onClick={() => setImportOpen(true)}>
                <Download className="h-4 w-4" /> {copy.music.import}
              </button>
            </div>
          ) : !filtered.length ? (
            <div className="p-8 text-center text-sm text-muted">{copy.music.emptySearch}</div>
          ) : (
            <ul className="divide-y divide-line">
              {filtered.map((track, index) => {
                const selected = track.id === current?.id;
                return (
                  <li key={track.id} className={cn("group flex min-w-0 items-center gap-3 px-4 py-3 transition-colors sm:px-5", selected ? "bg-accent-soft/40" : "hover:bg-surface-2/70")}>
                    <span className="hidden w-5 shrink-0 text-center text-xs tabular-nums text-faint sm:block">{index + 1}</span>
                    <button
                      className={cn(
                        "focus-ring flex h-10 w-10 shrink-0 items-center justify-center rounded-full",
                        selected ? "bg-accent text-accent-ink" : "bg-surface-2 text-muted group-hover:text-ink",
                      )}
                      onClick={() => (selected ? toggle() : playTrack(track))}
                      aria-label={selected && playing ? copy.music.pause : copy.music.play}
                    >
                      {selected && playing ? <Pause className="h-4 w-4" /> : <Play className="ml-0.5 h-4 w-4" />}
                    </button>
                    <button className="min-w-0 flex-1 text-left" onClick={() => playTrack(track)}>
                      <div className={cn("truncate text-sm font-semibold", selected ? "text-accent" : "text-ink")}>{track.title}</div>
                      <div className="mt-0.5 truncate text-xs text-muted">{track.artist || copy.music.local}</div>
                    </button>
                    <span className="shrink-0 text-xs tabular-nums text-faint">{formatDuration(track.duration)}</span>
                    <button
                      className="focus-ring flex h-9 w-9 shrink-0 items-center justify-center rounded-lg text-faint opacity-100 hover:bg-red-500/10 hover:text-danger sm:opacity-0 sm:group-hover:opacity-100"
                      onClick={() => {
                        if (window.confirm(`${copy.music.removeConfirm}\n\n${track.title}`)) deleteMutation.mutate(track);
                      }}
                      aria-label={copy.music.remove}
                    >
                      <Trash2 className="h-4 w-4" />
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>

        <div className="space-y-5">
          <section className="card min-w-0 overflow-hidden">
            <div className="border-b border-line p-5">
              <p className="label">{copy.music.nowPlaying}</p>
              <h2 className="mt-2 line-clamp-2 text-xl font-bold leading-tight text-ink">
                {current?.title || copy.music.nothingPlaying}
              </h2>
              <p className="mt-1 truncate text-sm text-muted">{current ? (current.artist || copy.music.local) : copy.music.chooseTrack}</p>
            </div>
            <div className="p-5">
              <div className="flex items-center justify-center gap-5">
                <button className="focus-ring rounded-full p-2 text-muted hover:bg-surface-2 hover:text-ink" onClick={prev} disabled={!tracks.length} aria-label={copy.music.prev}>
                  <SkipBack className="h-5 w-5" />
                </button>
                <button
                  className="focus-ring flex h-16 w-16 items-center justify-center rounded-full bg-accent text-accent-ink shadow-lg shadow-accent/20 transition hover:brightness-110 disabled:opacity-40"
                  onClick={toggle}
                  disabled={!tracks.length}
                  aria-label={playing ? copy.music.pause : copy.music.play}
                >
                  {playing ? <Pause className="h-7 w-7" /> : <Play className="ml-1 h-7 w-7" />}
                </button>
                <button className="focus-ring rounded-full p-2 text-muted hover:bg-surface-2 hover:text-ink" onClick={next} disabled={!tracks.length} aria-label={copy.music.next}>
                  <SkipForward className="h-5 w-5" />
                </button>
              </div>

              <div className="mt-6 space-y-4">
                <label className="block">
                  <span className="flex items-center justify-between text-xs text-muted">
                    <span className="flex items-center gap-1.5"><Volume2 className="h-3.5 w-3.5" /> {copy.music.volume}</span>
                    <span className="tabular-nums">{Math.round(volume * 100)}%</span>
                  </span>
                  <input
                    className="mt-2 h-1.5 w-full accent-[var(--color-accent)]"
                    type="range"
                    min="0"
                    max="100"
                    value={Math.round(volume * 100)}
                    onChange={(event) => setVolume(Number(event.target.value) / 100)}
                  />
                </label>
                <button
                  className={cn(
                    "focus-ring flex w-full items-center justify-between rounded-xl border px-3 py-2 text-sm font-medium",
                    loop ? "border-accent/30 bg-accent-soft text-accent" : "border-line bg-surface-2 text-muted",
                  )}
                  onClick={() => setLoop(!loop)}
                  aria-pressed={loop}
                >
                  <span>{copy.music.loop}</span>
                  <span className={cn("h-2.5 w-2.5 rounded-full", loop ? "bg-accent" : "bg-faint")} />
                </button>
              </div>
            </div>
          </section>

          {current?.source_url ? (
            <section className="card p-5">
              <p className="label">{copy.music.source}</p>
              <a className="mt-2 block truncate text-sm text-accent hover:underline" href={current.source_url} target="_blank" rel="noreferrer">
                {current.source_url}
              </a>
              {current.size ? <p className="mt-2 text-xs text-faint">{formatBytes(current.size)}</p> : null}
            </section>
          ) : null}
        </div>
      </section>

      <ImportDialog
        open={importOpen}
        onOpenChange={setImportOpen}
        onImported={refreshMusic}
        onStarted={refreshMusic}
      />
    </div>
  );
}

function ImportProgress({
  job,
  onCancel,
  canceling,
}: {
  job: ImportJob;
  onCancel: () => void;
  canceling: boolean;
}) {
  return (
    <div className="card border-accent/25 bg-accent-soft/30 p-4">
      <div className="flex flex-wrap items-center gap-3">
        <Loader2 className="h-5 w-5 shrink-0 animate-spin text-accent" />
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-semibold text-ink">
            {job.title || job.url}
          </p>
          <p className="mt-0.5 text-xs text-muted">
            {job.state === "processing" ? "Đang chuyển sang MP3…" : "Đang tải từ YouTube…"}
            {job.eta ? ` · còn ${formatEta(job.eta)}` : ""}
          </p>
        </div>
        <span className="text-xs tabular-nums text-accent">{Math.round(job.progress)}%</span>
        <button className="btn btn-danger min-h-9 px-3 text-xs" onClick={onCancel} disabled={canceling || job.state === "cancelling"}>
          {canceling ? "Đang huỷ…" : copy.music.cancel}
        </button>
      </div>
      <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-surface-3">
        <div className="h-full rounded-full bg-accent transition-[width] duration-300" style={{ width: `${job.progress}%` }} />
      </div>
    </div>
  );
}
