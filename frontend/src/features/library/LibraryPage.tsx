import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import * as Dialog from "@radix-ui/react-dialog";
import {
  BookOpen,
  ChevronLeft,
  ChevronRight,
  Eye,
  Languages,
  LibraryBig,
  Loader2,
  Plus,
  RefreshCw,
  Search,
  X,
} from "lucide-react";
import {
  getLibrary,
  previewBook,
  type LibraryEntry,
  type LibraryGroup,
} from "../../lib/api";
import { copy } from "../../lib/copy";
import { cn } from "../../lib/cn";

const statusStyle: Record<string, string> = {
  done: "bg-emerald-500/12 text-emerald-300 border-emerald-500/25",
  partial: "bg-amber-500/12 text-amber-300 border-amber-500/25",
  assumed: "bg-sky-500/12 text-sky-300 border-sky-500/25",
  untranslated: "bg-surface-2 text-muted border-line",
};

const statusLabel: Record<string, string> = {
  done: "Đã dịch",
  partial: "Đang dịch",
  assumed: "Bản dịch cũ",
  untranslated: "Bản gốc",
};

const filters = [
  { id: "all", label: "Tất cả" },
  { id: "done", label: "Đã dịch" },
  { id: "partial", label: "Đang dịch" },
  { id: "assumed", label: "Bản dịch cũ" },
  { id: "untranslated", label: "Bản gốc" },
];

function primaryEntry(group: LibraryGroup): LibraryEntry | null {
  return group.translations[0] ?? group.source ?? group.entries[0] ?? null;
}

function coverUrl(entry: LibraryEntry | null) {
  return entry ? `/api/v2/books/cover?path=${encodeURIComponent(entry.path)}` : "";
}

type PreviewState = {
  path: string;
  name: string;
  title: string;
  chapter: number;
};

export function LibraryPage() {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");
  const [versions, setVersions] = useState<Record<string, string>>({});
  const [preview, setPreview] = useState<PreviewState | null>(null);
  const library = useQuery({ queryKey: ["library"], queryFn: () => getLibrary() });

  const previewQuery = useQuery({
    queryKey: ["preview", preview?.path, preview?.chapter],
    queryFn: () => previewBook(preview!.path, preview!.chapter),
    enabled: Boolean(preview),
    staleTime: 60_000,
  });

  const groups = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const all = library.data?.groups ?? [];
    return all.filter((group) => {
      const entry = group.entries.find((item) => item.path === versions[group.base_name]) || primaryEntry(group);
      if (filter !== "all" && entry?.status !== filter) return false;
      if (!needle) return true;
      return `${group.title} ${group.base_name} ${group.source?.name ?? ""} ${group.entries.map((item) => item.name).join(" ")}`.toLowerCase().includes(needle);
    });
  }, [filter, library.data?.groups, query, versions]);

  const counts = useMemo(() => {
    const out: Record<string, number> = { all: library.data?.groups.length ?? 0 };
    for (const group of library.data?.groups ?? []) {
      const status = primaryEntry(group)?.status || "untranslated";
      out[status] = (out[status] || 0) + 1;
    }
    return out;
  }, [library.data?.groups]);

  const chosen = (group: LibraryGroup) =>
    group.entries.find((item) => item.path === versions[group.base_name]) || primaryEntry(group);

  const openPreview = (group: LibraryGroup, entry: LibraryEntry) => {
    setPreview({ path: entry.path, name: entry.name, title: group.title, chapter: 1 });
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <p className="label">EPUB library</p>
          <h1 className="mt-2 text-3xl font-bold tracking-tight text-ink sm:text-4xl">{copy.library.title}</h1>
          <p className="mt-2 text-sm leading-6 text-muted">{copy.library.subtitle}</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Link to="/jobs/new" className="btn btn-primary">
            <Plus className="h-4 w-4" /> Dịch sách mới
          </Link>
          <button className="btn btn-secondary shrink-0" onClick={() => void library.refetch()} disabled={library.isFetching}>
            <RefreshCw className={cn("h-4 w-4", library.isFetching && "animate-spin")} />
            {copy.library.refresh}
          </button>
        </div>
      </div>

      <div className="card space-y-3 p-4">
        <div className="relative">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-faint" />
          <input
            className="input pl-9"
            type="search"
            placeholder={copy.library.search}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </div>
        <div className="flex flex-wrap gap-2">
          {filters.map((item) => (
            <button
              key={item.id}
              type="button"
              className={cn(
                "rounded-full border px-3 py-1.5 text-xs font-medium transition",
                filter === item.id
                  ? "border-accent bg-accent-soft text-accent"
                  : "border-line bg-surface-2 text-muted hover:border-line-strong hover:text-ink",
              )}
              onClick={() => setFilter(item.id)}
              aria-pressed={filter === item.id}
            >
              {item.label}
              <span className="ml-1.5 text-[10px] opacity-70">{counts[item.id] ?? 0}</span>
            </button>
          ))}
        </div>
      </div>

      {library.isLoading ? (
        <div className="card flex items-center gap-3 p-8 text-sm text-muted">
          <Loader2 className="h-4 w-4 animate-spin text-accent" /> {copy.library.loading}
        </div>
      ) : library.isError ? (
        <div className="card p-8 text-sm text-danger">
          {library.error instanceof Error ? library.error.message : "Không tải được thư viện."}
        </div>
      ) : !groups.length ? (
        <div className="card p-10 text-center">
          <LibraryBig className="mx-auto h-10 w-10 text-faint" />
          <p className="mt-3 text-sm text-muted">{copy.library.empty}</p>
        </div>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {groups.map((group) => {
            const entry = chosen(group);
            const status = entry?.status ?? "untranslated";
            const showVersion = group.entries.length > 1;
            return (
              <article key={group.base_name} className="card group flex min-w-0 flex-col p-4 transition hover:border-line-strong">
                <div className="flex min-w-0 gap-4">
                  <div className="relative h-32 w-[5.2rem] shrink-0 overflow-hidden rounded-lg border border-line bg-gradient-to-br from-surface-3 to-surface-2">
                    <BookOpen className="absolute left-1/2 top-1/2 h-6 w-6 -translate-x-1/2 -translate-y-1/2 text-faint" />
                    {entry ? (
                      <img
                        src={coverUrl(entry)}
                        alt=""
                        className="relative h-full w-full object-cover"
                        loading="lazy"
                        onError={(event) => { event.currentTarget.style.display = "none"; }}
                      />
                    ) : null}
                  </div>
                  <div className="flex min-w-0 flex-1 flex-col">
                    <h2 className="line-clamp-2 text-sm font-semibold leading-5 text-ink" title={group.title}>
                      {group.title || group.base_name}
                    </h2>
                    <div className="mt-1 truncate font-mono text-[11px] text-muted">{entry?.name || group.base_name}</div>
                    <div className="mt-2 flex flex-wrap items-center gap-2">
                      <span className={cn("rounded-full border px-2 py-0.5 text-[11px] font-medium", statusStyle[status])}>
                        {statusLabel[status] || status}
                      </span>
                      {entry?.chapters ? <span className="text-[11px] text-faint">{entry.chapters} chương</span> : null}
                    </div>
                    {showVersion ? (
                      <label className="mt-2 block">
                        <span className="sr-only">Chọn phiên bản</span>
                        <select
                          className="input min-h-8 py-1 text-[11px]"
                          value={entry?.path || ""}
                          onChange={(event) => setVersions((prev) => ({ ...prev, [group.base_name]: event.target.value }))}
                        >
                          {group.entries.map((item) => (
                            <option key={item.path} value={item.path}>
                              {statusLabel[item.status] || item.status} · {item.name}
                            </option>
                          ))}
                        </select>
                      </label>
                    ) : null}
                  </div>
                </div>
                {entry ? (
                  <div className="mt-4 flex flex-wrap gap-2 border-t border-line pt-3">
                    <Link
                      className="btn btn-primary min-h-8 flex-1 px-3 text-xs"
                      to={`/read?path=${encodeURIComponent(entry.path)}`}
                    >
                      <BookOpen className="h-3.5 w-3.5" /> {copy.library.read}
                    </Link>
                    <button className="btn btn-secondary min-h-8 px-3 text-xs" onClick={() => openPreview(group, entry)}>
                      <Eye className="h-3.5 w-3.5" /> Xem trước
                    </button>
                    {group.source ? (
                      <Link
                        className="btn btn-ghost min-h-8 px-3 text-xs"
                        to={`/jobs/new?input=${encodeURIComponent(group.source.path)}`}
                        title="Tạo job dịch từ bản gốc"
                      >
                        <Languages className="h-3.5 w-3.5" /> Dịch
                      </Link>
                    ) : null}
                  </div>
                ) : null}
              </article>
            );
          })}
        </div>
      )}

      <Dialog.Root open={Boolean(preview)} onOpenChange={(open) => { if (!open) setPreview(null); }}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-50 bg-black/70 backdrop-blur-sm" />
          <Dialog.Content className="fixed left-1/2 top-1/2 z-50 flex max-h-[90dvh] w-[calc(100vw-1.5rem)] max-w-3xl -translate-x-1/2 -translate-y-1/2 flex-col overflow-hidden rounded-2xl border border-line bg-surface shadow-2xl outline-none">
            <div className="flex items-start gap-3 border-b border-line p-4 pr-12">
              <div className="min-w-0 flex-1">
                <Dialog.Title className="truncate text-base font-semibold text-ink">{preview?.title || "Xem trước"}</Dialog.Title>
                <p className="mt-1 truncate font-mono text-[11px] text-muted">{preview?.name}</p>
              </div>
              <Dialog.Close asChild>
                <button className="absolute right-3 top-3 flex h-9 w-9 items-center justify-center rounded-lg text-muted hover:bg-surface-2 hover:text-ink" aria-label="Đóng">
                  <X className="h-4 w-4" />
                </button>
              </Dialog.Close>
            </div>
            <div className="min-h-0 flex-1 overflow-auto p-5">
              {previewQuery.isLoading ? (
                <div className="flex items-center gap-3 py-12 text-sm text-muted"><Loader2 className="h-4 w-4 animate-spin text-accent" /> Đang tải chương…</div>
              ) : previewQuery.isError ? (
                <p className="text-sm text-danger">{previewQuery.error instanceof Error ? previewQuery.error.message : "Không tải được chương."}</p>
              ) : (
                <pre className="whitespace-pre-wrap font-serif text-[15px] leading-7 text-ink">{previewQuery.data?.text || "Chương trống."}</pre>
              )}
            </div>
            <div className="flex items-center justify-between gap-3 border-t border-line p-3">
              <button
                className="btn btn-secondary min-h-9 px-3 text-xs"
                onClick={() => setPreview((prev) => prev ? { ...prev, chapter: Math.max(1, prev.chapter - 1) } : prev)}
                disabled={!preview || preview.chapter <= 1 || previewQuery.isFetching}
              >
                <ChevronLeft className="h-3.5 w-3.5" /> Trước
              </button>
              <span className="text-xs text-muted">
                Chương {preview?.chapter || 1}{previewQuery.data?.total ? ` / ${previewQuery.data.total}` : ""}
                {previewQuery.data?.shown ? ` · ${previewQuery.data.shown} ký tự` : ""}
              </span>
              <button
                className="btn btn-secondary min-h-9 px-3 text-xs"
                onClick={() => setPreview((prev) => prev ? { ...prev, chapter: prev.chapter + 1 } : prev)}
                disabled={!preview || Boolean(previewQuery.data && preview.chapter >= previewQuery.data.total) || previewQuery.isFetching}
              >
                Sau <ChevronRight className="h-3.5 w-3.5" />
              </button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </div>
  );
}
