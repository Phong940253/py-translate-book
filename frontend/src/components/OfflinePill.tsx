import { Loader2, WifiOff } from "lucide-react";
import { formatBytes } from "../lib/api";
import { cn } from "../lib/cn";
import { copy } from "../lib/copy";
import { useOfflineProgress, useOnline } from "../lib/offline";

/* Global status chip: shows an active book download anywhere in the app (the
   reader route sits outside AppShell) and announces offline mode. */
export function OfflinePill() {
  const online = useOnline();
  const progress = useOfflineProgress();
  const active = Object.values(progress)[0];

  if (online && !active) return null;

  return (
    <div
      role="status"
      title={active ? copy.offline.downloading : copy.offline.offlineHint}
      className={cn(
        "fixed right-3 top-16 z-40 flex max-w-[calc(100vw-1.5rem)] items-center gap-2 rounded-full border px-3 py-1.5 text-xs font-medium shadow-lg backdrop-blur",
        active
          ? "border-accent/40 bg-accent-soft text-accent"
          : "border-amber-500/40 bg-amber-500/12 text-amber-300",
      )}
    >
      {active ? (
        <>
          <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin" />
          <span className="truncate">
            {copy.offline.downloading} {active.pct}% · {formatBytes(active.bytes)}
            {active.chaptersTotal ? ` · ${active.chaptersDone}/${active.chaptersTotal}` : ""}
          </span>
        </>
      ) : (
        <>
          <WifiOff className="h-3.5 w-3.5 shrink-0" />
          <span>{copy.offline.offline}</span>
        </>
      )}
    </div>
  );
}
