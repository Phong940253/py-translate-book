import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Activity, ArrowRight, BookOpen, LibraryBig, Loader2, Music2, PlusCircle, Sparkles } from "lucide-react";
import { getJobs, getLibrary, type JobStatus, type LibraryGroup } from "../../lib/api";
import { copy } from "../../lib/copy";
import { cn } from "../../lib/cn";

function statusCount(groups: LibraryGroup[], status: string) {
  return groups.filter((group) => (group.translations[0] ?? group.source)?.status === status).length;
}

const jobLabel: Record<JobStatus, string> = {
  queued: "Đang chờ",
  running: "Đang dịch",
  done: "Hoàn tất",
  error: "Lỗi",
  stopped: "Đã dừng",
  interrupted: "Gián đoạn",
};

const jobTone: Record<JobStatus, string> = {
  queued: "text-sky-300",
  running: "text-amber-300",
  done: "text-emerald-300",
  error: "text-red-300",
  stopped: "text-muted",
  interrupted: "text-orange-300",
};

function jobPercent(progress: Record<string, unknown>) {
  const total = Number(progress.total_chapters || 0);
  const done = Number(progress.last_completed || 0);
  return total ? Math.min(100, Math.round((done / total) * 100)) : 0;
}

export function DashboardPage() {
  const library = useQuery({ queryKey: ["library"], queryFn: () => getLibrary() });
  const jobsQuery = useQuery({ queryKey: ["jobs"], queryFn: getJobs, refetchInterval: 5000 });
  const groups = library.data?.groups ?? [];
  const jobs = jobsQuery.data?.jobs ?? [];
  const done = statusCount(groups, "done");
  const partial = statusCount(groups, "partial");
  const active = jobs.filter((job) => job.status === "running" || job.status === "queued").length;

  return (
    <div className="space-y-6">
      <section className="relative overflow-hidden rounded-3xl border border-line bg-surface p-6 sm:p-8">
        <div className="pointer-events-none absolute right-0 -top-20 h-56 w-56 rounded-full bg-accent/10 blur-3xl" />
        <div className="relative max-w-2xl">
          <div className="inline-flex items-center gap-2 rounded-full border border-accent/25 bg-accent-soft px-3 py-1 text-xs font-medium text-accent">
            <Sparkles className="h-3.5 w-3.5" /> WebUI v2
          </div>
          <h1 className="mt-4 text-3xl font-bold tracking-tight text-ink sm:text-5xl">{copy.dashboard.title}</h1>
          <p className="mt-3 text-sm leading-6 text-muted sm:text-base">{copy.dashboard.subtitle}</p>
          <div className="mt-6 flex flex-wrap gap-3">
            <Link to="/jobs/new" className="btn btn-primary">
              <PlusCircle className="h-4 w-4" /> Dịch sách mới
            </Link>
            <Link to="/library" className="btn btn-secondary">
              <LibraryBig className="h-4 w-4" /> {copy.dashboard.openLibrary}
            </Link>
            <Link to="/music" className="btn btn-ghost">
              <Music2 className="h-4 w-4" /> {copy.dashboard.openMusic}
            </Link>
          </div>
        </div>
      </section>

      <section className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        {[
          { label: copy.dashboard.books, value: groups.length, icon: LibraryBig },
          { label: copy.dashboard.done, value: done, icon: BookOpen },
          { label: copy.dashboard.partial, value: partial, icon: Loader2 },
          { label: "Job đang chạy", value: active, icon: Activity },
        ].map(({ label, value, icon: Icon }) => (
          <div key={label} className="card flex items-center justify-between gap-3 p-4 sm:p-5">
            <div className="min-w-0">
              <p className="text-[11px] font-medium uppercase tracking-[0.1em] text-faint sm:text-xs">{label}</p>
              <p className="mt-2 text-2xl font-bold tabular-nums text-ink sm:text-3xl">{library.isLoading ? "—" : value}</p>
            </div>
            <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-surface-2 text-accent sm:h-11 sm:w-11">
              <Icon className="h-5 w-5" />
            </span>
          </div>
        ))}
      </section>

      <section className="card overflow-hidden">
        <div className="flex items-center justify-between border-b border-line p-5">
          <h2 className="flex items-center gap-2 text-base font-semibold text-ink"><Activity className="h-4 w-4 text-accent" /> Job gần đây</h2>
          <Link to="/jobs/new" className="inline-flex items-center gap-1 text-sm font-medium text-accent hover:underline">
            Tạo job <PlusCircle className="h-4 w-4" />
          </Link>
        </div>
        {jobsQuery.isLoading ? (
          <div className="flex items-center gap-3 p-6 text-sm text-muted"><Loader2 className="h-4 w-4 animate-spin text-accent" /> Đang tải job…</div>
        ) : jobs.length ? (
          <ul className="divide-y divide-line">
            {jobs.slice(0, 6).map((job) => {
              const pct = jobPercent(job.progress as Record<string, unknown>);
              return (
                <li key={job.id}>
                  <Link to={`/jobs/${job.id}`} className="flex min-w-0 items-center gap-3 px-5 py-3 transition hover:bg-surface-2/60">
                    <span className={cn("h-2 w-2 shrink-0 rounded-full", job.status === "running" ? "bg-amber-400" : job.status === "done" ? "bg-emerald-400" : job.status === "error" ? "bg-red-400" : "bg-line-strong")} />
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <span className="truncate text-sm font-medium text-ink">{job.params.description || job.params.input?.split(/[\\/]/).pop() || job.id}</span>
                        <span className={cn("shrink-0 text-[11px]", jobTone[job.status])}>{jobLabel[job.status] || job.status}</span>
                      </div>
                      <div className="mt-1 flex items-center gap-2">
                        <div className="h-1.5 min-w-0 flex-1 overflow-hidden rounded-full bg-surface-2">
                          <div className="h-full rounded-full bg-accent" style={{ width: `${pct}%` }} />
                        </div>
                        <span className="shrink-0 text-[10px] tabular-nums text-faint">{pct}%</span>
                      </div>
                    </div>
                    <ArrowRight className="h-4 w-4 shrink-0 text-faint" />
                  </Link>
                </li>
              );
            })}
          </ul>
        ) : (
          <div className="p-6 text-sm text-muted">Chưa có job nào. <Link className="text-accent hover:underline" to="/jobs/new">Tạo job đầu tiên</Link>.</div>
        )}
      </section>

      <section className="card overflow-hidden">
        <div className="flex items-center justify-between border-b border-line p-5">
          <h2 className="text-base font-semibold text-ink">{copy.dashboard.recent}</h2>
          <Link to="/library" className="inline-flex items-center gap-1 text-sm font-medium text-accent hover:underline">
            {copy.dashboard.openLibrary} <ArrowRight className="h-4 w-4" />
          </Link>
        </div>
        {library.isLoading ? (
          <div className="flex items-center gap-3 p-6 text-sm text-muted">
            <Loader2 className="h-4 w-4 animate-spin text-accent" /> {copy.library.loading}
          </div>
        ) : groups.length ? (
          <ul className="divide-y divide-line">
            {groups.slice(0, 5).map((group) => {
              const entry = group.translations[0] ?? group.source;
              return (
                <li key={group.base_name} className="flex min-w-0 items-center gap-3 px-5 py-3">
                  <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-surface-2 text-faint">
                    <BookOpen className="h-4 w-4" />
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-sm font-medium text-ink">{group.title || group.base_name}</div>
                    <div className="truncate text-xs text-muted">{entry?.name || group.base_name}</div>
                  </div>
                  {entry ? (
                    <Link className="btn btn-ghost min-h-9 shrink-0 px-3 text-xs" to={`/read?path=${encodeURIComponent(entry.path)}`}>
                      {copy.library.read}
                    </Link>
                  ) : null}
                </li>
              );
            })}
          </ul>
        ) : (
          <div className="p-6 text-sm text-muted">{copy.library.empty}</div>
        )}
      </section>
    </div>
  );
}
