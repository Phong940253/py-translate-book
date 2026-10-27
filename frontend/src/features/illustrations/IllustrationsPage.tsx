import { useQuery } from "@tanstack/react-query";
import * as Dialog from "@radix-ui/react-dialog";
import { Image as ImageIcon, Loader2, RefreshCw, X } from "lucide-react";
import { useState } from "react";
import { getIllustrations, type Illustration } from "../../lib/api";
import { cn } from "../../lib/cn";

export function IllustrationsPage() {
  const images = useQuery({ queryKey: ["illustrations"], queryFn: getIllustrations });
  const [selected, setSelected] = useState<Illustration | null>(null);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="label">Thư viện ảnh</p>
          <h1 className="mt-2 text-3xl font-bold tracking-tight text-ink">Minh hoạ</h1>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-muted">
            Ảnh do pipeline minh hoạ tạo ra, lưu trong <span className="font-mono text-xs">{images.data?.dir || "images/generated"}</span>.
          </p>
        </div>
        <button className="btn btn-secondary" onClick={() => void images.refetch()} disabled={images.isFetching}>
          <RefreshCw className={cn("h-4 w-4", images.isFetching && "animate-spin")} /> Làm mới
        </button>
      </div>

      {images.isLoading ? (
        <div className="card flex items-center gap-3 p-8 text-sm text-muted"><Loader2 className="h-4 w-4 animate-spin text-accent" /> Đang tải ảnh…</div>
      ) : images.isError ? (
        <div className="card p-8 text-sm text-danger">{images.error instanceof Error ? images.error.message : "Không tải được thư viện ảnh."}</div>
      ) : !images.data?.images.length ? (
        <div className="card p-12 text-center">
          <ImageIcon className="mx-auto h-10 w-10 text-faint" />
          <p className="mt-3 text-sm text-muted">Chưa có ảnh minh hoạ nào.</p>
          <p className="mt-1 text-xs text-faint">Bật minh hoạ trong Cài đặt dịch rồi chạy một job mới.</p>
        </div>
      ) : (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
          {images.data.images.map((image) => (
            <button
              key={image.name}
              type="button"
              className="group overflow-hidden rounded-2xl border border-line bg-surface text-left transition hover:border-line-strong"
              onClick={() => setSelected(image)}
            >
              <div className="aspect-[3/4] overflow-hidden bg-surface-2">
                <img src={image.url} alt={image.name} loading="lazy" className="h-full w-full object-cover transition duration-300 group-hover:scale-[1.03]" />
              </div>
              <div className="truncate px-3 py-2 font-mono text-[11px] text-muted">{image.name}</div>
            </button>
          ))}
        </div>
      )}

      <Dialog.Root open={Boolean(selected)} onOpenChange={(open) => { if (!open) setSelected(null); }}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm" />
          <Dialog.Content className="fixed inset-0 z-50 flex flex-col items-center justify-center p-4 outline-none">
            <Dialog.Title className="sr-only">{selected?.name || "Ảnh minh hoạ"}</Dialog.Title>
            <Dialog.Close asChild>
              <button className="absolute right-4 top-4 flex h-11 w-11 items-center justify-center rounded-full bg-black/50 text-white hover:bg-black/70" aria-label="Đóng">
                <X className="h-5 w-5" />
              </button>
            </Dialog.Close>
            {selected ? (
              <>
                <img src={selected.url} alt={selected.name} className="max-h-[82vh] max-w-full rounded-xl object-contain shadow-2xl" />
                <div className="mt-3 flex items-center gap-4 text-xs text-white/80">
                  <span className="font-mono">{selected.name}</span>
                  <a className="text-accent hover:underline" href={selected.url} target="_blank" rel="noreferrer">Mở ảnh gốc</a>
                </div>
              </>
            ) : null}
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </div>
  );
}
