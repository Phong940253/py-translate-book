import { ArrowRight, BookMarked, Check, FileCode2, Image as ImageIcon, KeyRound, Languages, Palette, SlidersHorizontal } from "lucide-react";
import { Link } from "react-router-dom";
import { copy } from "../../lib/copy";
import { cn } from "../../lib/cn";
import { legacyReaderHref } from "../../lib/legacy";
import { useLanguage } from "../../lib/language";
import { useTheme, type Theme } from "../../lib/theme";

const themes: { id: Theme; label: string; swatch: string }[] = [
  { id: "light", label: "Sáng", swatch: "bg-[#f3f5f8]" },
  { id: "sepia", label: "Sepia", swatch: "bg-[#f3ead9]" },
  { id: "dark", label: "Tối", swatch: "bg-[#151a22]" },
  { id: "black", label: "Đen", swatch: "bg-black" },
  { id: "system", label: "Theo hệ thống", swatch: "bg-[linear-gradient(135deg,#f3f5f8_50%,#151a22_50%)]" },
  { id: "custom", label: "Tự chọn", swatch: "bg-[linear-gradient(135deg,#f7f3e8_50%,#312b22_50%)]" },
];

export function SettingsPage() {
  const { theme, setTheme, customBg, customFg, setCustomColors } = useTheme();
  const { locale, setLanguage } = useLanguage();

  return (
    <div className="space-y-6">
      <div>
        <p className="label">Tùy chọn</p>
        <h1 className="mt-2 text-3xl font-bold tracking-tight text-ink sm:text-4xl">{copy.settings.title}</h1>
        <p className="mt-2 text-sm leading-6 text-muted">{copy.settings.subtitle}</p>
      </div>

      <section className="card p-5 sm:p-6">
        <div className="flex items-center gap-3">
          <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-surface-2 text-accent"><Palette className="h-5 w-5" /></span>
          <div>
            <h2 className="text-base font-semibold text-ink">{copy.settings.theme}</h2>
            <p className="text-sm text-muted">{copy.settings.themeHint}</p>
          </div>
        </div>
        <div className="mt-5 grid gap-3 sm:grid-cols-3">
          {themes.map((item) => (
            <button
              key={item.id}
              className={cn(
                "focus-ring flex items-center gap-3 rounded-xl border p-3 text-left transition",
                theme === item.id ? "border-accent bg-accent-soft" : "border-line bg-surface-2 hover:border-line-strong",
              )}
              onClick={() => setTheme(item.id)}
              aria-pressed={theme === item.id}
            >
              <span className={cn("h-8 w-8 rounded-lg border border-line-strong", item.swatch)} />
              <span className="flex-1 text-sm font-medium text-ink">{item.label}</span>
              {theme === item.id ? <Check className="h-4 w-4 text-accent" /> : null}
            </button>
          ))}
        </div>
        {theme === "custom" ? (
          <div className="mt-5 grid gap-4 rounded-xl border border-line bg-surface-2/50 p-4 sm:grid-cols-2">
            <label className="space-y-2 text-sm">
              <span className="block font-medium text-ink">Màu nền</span>
              <span className="flex items-center gap-3">
                <input
                  type="color"
                  className="h-10 w-14 cursor-pointer rounded-lg border border-line bg-surface p-1"
                  value={customBg}
                  onChange={(event) => setCustomColors(event.target.value, customFg)}
                />
                <span className="font-mono text-xs text-muted">{customBg}</span>
              </span>
            </label>
            <label className="space-y-2 text-sm">
              <span className="block font-medium text-ink">Màu chữ</span>
              <span className="flex items-center gap-3">
                <input
                  type="color"
                  className="h-10 w-14 cursor-pointer rounded-lg border border-line bg-surface p-1"
                  value={customFg}
                  onChange={(event) => setCustomColors(customBg, event.target.value)}
                />
                <span className="font-mono text-xs text-muted">{customFg}</span>
              </span>
            </label>
          </div>
        ) : null}
      </section>

      <section className="card p-5 sm:p-6">
        <div className="flex items-center gap-3">
          <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-surface-2 text-accent"><Languages className="h-5 w-5" /></span>
          <div>
            <h2 className="text-base font-semibold text-ink">{copy.settings.language}</h2>
            <p className="text-sm text-muted">{copy.settings.languageHint}</p>
          </div>
        </div>
        <div className="mt-5 flex flex-wrap gap-3">
          <button className={cn("btn", locale === "vi" ? "btn-primary" : "btn-secondary")} type="button" aria-pressed={locale === "vi"} onClick={() => setLanguage("vi")}>
            <Check className="h-4 w-4" /> Tiếng Việt
          </button>
          <button className={cn("btn", locale === "en" ? "btn-primary" : "btn-secondary")} type="button" aria-pressed={locale === "en"} onClick={() => setLanguage("en")}>
            <Check className="h-4 w-4" /> English
          </button>
        </div>
        <p className="mt-3 text-xs text-faint">
          Chọn ngôn ngữ cho toàn bộ giao diện v2; tiêu đề sách và nội dung chương giữ nguyên theo nguồn.
        </p>
      </section>

      <section className="card border-dashed p-5 sm:p-6">
        <div className="flex items-start gap-3">
          <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-surface-2 text-accent"><BookMarked className="h-5 w-5" /></span>
          <div>
            <h2 className="text-base font-semibold text-ink">{copy.settings.reader}</h2>
            <p className="mt-1 max-w-2xl text-sm leading-6 text-muted">{copy.settings.readerHint}</p>
            <Link className="btn btn-secondary mt-4" to={legacyReaderHref()}>Mở reader v2 (TTS)</Link>
          </div>
        </div>
      </section>

      <section className="card p-5 sm:p-6">
        <div className="flex items-center gap-3">
          <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-surface-2 text-accent"><SlidersHorizontal className="h-5 w-5" /></span>
          <div>
            <h2 className="text-base font-semibold text-ink">Cấu hình dịch & dữ liệu</h2>
            <p className="text-sm text-muted">Các trang cấu hình đầy đủ của v1 đã có bản v2.</p>
          </div>
        </div>
        <div className="mt-5 grid gap-3 sm:grid-cols-2">
          {[
            { to: "/keys", label: "API Keys", hint: "Provider, model, base URL", icon: KeyRound },
            { to: "/config/settings", label: "Cài đặt dịch", hint: "Chunk, minh hoạ, consistency, Discord", icon: SlidersHorizontal },
            { to: "/config", label: "YAML nâng cao", hint: "Toàn bộ config.yaml, backup tự động", icon: FileCode2 },
            { to: "/illustrations", label: "Minh hoạ", hint: "Thư viện ảnh đã tạo", icon: ImageIcon },
          ].map((item) => {
            const Icon = item.icon;
            return (
              <Link
                key={item.to}
                to={item.to}
                className="focus-ring flex items-center gap-3 rounded-xl border border-line bg-surface-2/50 p-3 transition hover:border-line-strong"
              >
                <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-surface text-accent"><Icon className="h-4 w-4" /></span>
                <span className="min-w-0 flex-1">
                  <span className="block text-sm font-medium text-ink">{item.label}</span>
                  <span className="block truncate text-xs text-faint">{item.hint}</span>
                </span>
                <ArrowRight className="h-4 w-4 shrink-0 text-faint" />
              </Link>
            );
          })}
        </div>
      </section>
    </div>
  );
}
