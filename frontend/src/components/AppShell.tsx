import type { LucideIcon } from "lucide-react";
import { Link, NavLink, Outlet } from "react-router-dom";
import {
  BookMarked,
  BookOpen,
  FileCode2,
  Image as ImageIcon,
  KeyRound,
  LayoutDashboard,
  LibraryBig,
  Music2,
  PlusCircle,
  Settings2,
  SunMoon,
} from "lucide-react";
import { cn } from "../lib/cn";
import { copy } from "../lib/copy";
import { legacyReaderHref } from "../lib/legacy";
import { useTheme } from "../lib/theme";

type NavItemDef = {
  to: string;
  label: string;
  icon: LucideIcon;
  end?: boolean;
};

const primaryNav: NavItemDef[] = [
  { to: "/", label: copy.nav.dashboard, icon: LayoutDashboard, end: true },
  { to: "/jobs/new", label: "Dịch mới", icon: PlusCircle },
  { to: "/library", label: copy.nav.library, icon: LibraryBig },
  { to: "/illustrations", label: "Minh hoạ", icon: ImageIcon },
  { to: "/music", label: copy.nav.music, icon: Music2 },
];

const configNav: NavItemDef[] = [
  { to: "/keys", label: "API Keys", icon: KeyRound },
  { to: "/config/settings", label: "Cài đặt dịch", icon: Settings2 },
  { to: "/config", label: "YAML nâng cao", icon: FileCode2 },
  { to: "/settings", label: "Giao diện", icon: Settings2 },
];

const mobileNav: NavItemDef[] = [
  { to: "/", label: "Tổng quan", icon: LayoutDashboard, end: true },
  { to: "/jobs/new", label: "Dịch", icon: PlusCircle },
  { to: "/library", label: "Thư viện", icon: LibraryBig },
  { to: "/music", label: "Nhạc", icon: Music2 },
  { to: "/settings", label: "Cài đặt", icon: Settings2 },
];

function NavItem({ item, compact = false }: { item: NavItemDef; compact?: boolean }) {
  const Icon = item.icon;
  return (
    <NavLink
      to={item.to}
      end={item.end}
      className={({ isActive }) =>
        cn(
          "focus-ring flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition-colors",
          compact ? "flex-col gap-1 rounded-lg px-1 py-1.5 text-[10px]" : "",
          isActive
            ? "bg-accent-soft text-accent"
            : "text-muted hover:bg-surface-2 hover:text-ink",
        )
      }
    >
      <Icon className={compact ? "h-5 w-5" : "h-[18px] w-[18px]"} />
      <span className={compact ? "max-w-full truncate" : ""}>{item.label}</span>
    </NavLink>
  );
}

export function AppShell() {
  const { theme, cycleTheme } = useTheme();

  return (
    <div className="min-h-dvh bg-bg text-ink">
      <aside className="fixed inset-y-0 left-0 z-30 hidden w-64 flex-col overflow-y-auto border-r border-line bg-surface/80 px-4 py-5 backdrop-blur md:flex">
        <div className="flex items-center gap-3 px-2">
          <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-accent text-accent-ink shadow-lg shadow-accent/20">
            <BookOpen className="h-5 w-5" />
          </span>
          <div className="min-w-0">
            <div className="truncate text-sm font-bold tracking-tight text-ink">{copy.appName}</div>
            <div className="text-[11px] font-medium uppercase tracking-[0.16em] text-faint">{copy.appSub}</div>
          </div>
        </div>

        <nav className="mt-7 space-y-1" aria-label="Điều hướng chính">
          {primaryNav.map((item) => <NavItem key={item.to} item={item} />)}
        </nav>

        <div className="mt-7">
          <p className="px-3 text-[10px] font-semibold uppercase tracking-[0.16em] text-faint">Cấu hình</p>
          <nav className="mt-2 space-y-1" aria-label="Cấu hình">
            {configNav.map((item) => <NavItem key={item.to} item={item} />)}
          </nav>
        </div>

        <div className="mt-auto space-y-3 pt-8">
          <Link
            to={legacyReaderHref()}
            className="focus-ring flex items-center gap-3 rounded-xl border border-line bg-surface-2 px-3 py-2.5 text-sm text-muted transition hover:border-line-strong hover:text-ink"
          >
            <BookMarked className="h-[18px] w-[18px] text-accent" />
            <span className="min-w-0">
              <span className="block font-medium">{copy.sidebar.reader}</span>
              <span className="block truncate text-[11px] text-faint">TTS, tìm kiếm &amp; ghi chú</span>
            </span>
          </Link>
          <button
            type="button"
            onClick={cycleTheme}
            className="focus-ring flex w-full items-center justify-between rounded-xl px-3 py-2 text-sm text-muted hover:bg-surface-2 hover:text-ink"
          >
            <span className="flex items-center gap-3"><SunMoon className="h-[18px] w-[18px]" /> {copy.sidebar.theme}</span>
            <span className="rounded-md bg-surface-2 px-2 py-0.5 text-[11px] uppercase text-faint">{theme}</span>
          </button>
          <p className="px-3 text-[11px] leading-5 text-faint">WebUI v2 · port 5000</p>
        </div>
      </aside>

      <header className="sticky top-0 z-20 flex h-14 items-center gap-3 border-b border-line bg-bg/90 px-4 backdrop-blur md:hidden">
        <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-accent text-accent-ink">
          <BookOpen className="h-[18px] w-[18px]" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-bold text-ink">{copy.appName}</div>
          <div className="text-[10px] uppercase tracking-[0.16em] text-faint">{copy.appSub}</div>
        </div>
        <button
          type="button"
          onClick={cycleTheme}
          className="focus-ring flex h-10 w-10 items-center justify-center rounded-xl text-muted hover:bg-surface-2 hover:text-ink"
          aria-label={copy.sidebar.theme}
        >
          <SunMoon className="h-5 w-5" />
        </button>
      </header>

      <main className="min-h-dvh w-full md:pl-64">
        <div className="mx-auto w-full max-w-[1240px] px-4 pb-40 pt-6 sm:px-6 md:px-8 md:pb-28 md:pt-8">
          <Outlet />
        </div>
      </main>

      <nav className="fixed inset-x-0 bottom-0 z-30 border-t border-line bg-surface/95 pb-[env(safe-area-inset-bottom)] backdrop-blur md:hidden" aria-label="Điều hướng di động">
        <div className="grid grid-cols-5 gap-0.5 px-1.5 py-1.5">
          {mobileNav.map((item) => <NavItem key={item.to} item={item} compact />)}
        </div>
      </nav>
    </div>
  );
}
