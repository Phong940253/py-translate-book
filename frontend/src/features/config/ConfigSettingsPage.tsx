import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Bell, Loader2, Save, Settings2 } from "lucide-react";
import {
  getSettingsConfig,
  saveSettingsConfig,
  testDiscord,
  type ConfigField,
  type ConfigGroup,
} from "../../lib/api";

function FieldInput({ field, value, onChange }: { field: ConfigField; value: unknown; onChange: (value: unknown) => void }) {
  if (field.kind === "bool") {
    return (
      <label className="flex items-center gap-3 rounded-xl border border-line bg-surface-2/50 px-3 py-2.5">
        <input
          type="checkbox"
          className="h-4 w-4 accent-[var(--color-accent)]"
          checked={Boolean(value)}
          onChange={(event) => onChange(event.target.checked)}
        />
        <span className="text-sm text-ink">{field.label_text}</span>
      </label>
    );
  }
  if (field.kind === "text") {
    return (
      <textarea
        className="input min-h-24 font-mono text-xs"
        rows={field.extra.rows || 3}
        value={String(value ?? "")}
        onChange={(event) => onChange(event.target.value)}
      />
    );
  }
  return (
    <input
      className="input"
      type={field.kind === "int" || field.kind === "float" ? "number" : "text"}
      step={field.extra.step ?? (field.kind === "int" ? 1 : "any")}
      min={field.extra.min}
      max={field.extra.max}
      value={String(value ?? "")}
      onChange={(event) => onChange(field.kind === "int" || field.kind === "float" ? event.target.value : event.target.value)}
    />
  );
}

function GroupCard({ group, values, onChange }: { group: ConfigGroup; values: Record<string, unknown>; onChange: (path: string, value: unknown) => void }) {
  return (
    <section className="card p-5">
      <h2 className="flex items-center gap-2 border-b border-line pb-3 text-sm font-semibold text-ink">
        <Settings2 className="h-4 w-4 text-accent" /> {group.label_text}
      </h2>
      <div className="mt-4 grid gap-4 sm:grid-cols-2">
        {group.fields.map((field) => (
          <div key={field.path} className={field.kind === "text" ? "sm:col-span-2" : ""}>
            <label className="mb-2 block text-xs font-medium text-muted">{field.label_text}</label>
            <FieldInput field={field} value={values[field.path]} onChange={(value) => onChange(field.path, value)} />
            <p className="mt-1 font-mono text-[10px] text-faint">{field.path}</p>
          </div>
        ))}
      </div>
    </section>
  );
}

export function ConfigSettingsPage() {
  const queryClient = useQueryClient();
  const settings = useQuery({ queryKey: ["settings-config"], queryFn: getSettingsConfig });
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [saved, setSaved] = useState(false);
  const [discord, setDiscord] = useState<string>("");

  useEffect(() => {
    if (!settings.data) return;
    const next: Record<string, unknown> = {};
    for (const group of settings.data.groups) {
      for (const field of group.fields) next[field.path] = field.value;
    }
    setValues(next);
  }, [settings.data]);

  const save = useMutation({
    mutationFn: () => saveSettingsConfig(values),
    onSuccess: async () => {
      setSaved(true);
      await queryClient.invalidateQueries({ queryKey: ["settings-config"] });
      window.setTimeout(() => setSaved(false), 2500);
    },
  });

  const discordTest = useMutation({
    mutationFn: testDiscord,
    onSuccess: (data) => setDiscord(data.success ? "Đã gửi thông báo thử." : "Gửi thất bại — kiểm tra webhook."),
    onError: (error) => setDiscord(error instanceof Error ? error.message : "Gửi thất bại."),
  });

  if (settings.isLoading) {
    return <div className="flex items-center gap-3 p-8 text-sm text-muted"><Loader2 className="h-4 w-4 animate-spin text-accent" /> Đang tải cài đặt…</div>;
  }
  if (settings.isError || !settings.data) {
    return <div className="card p-8 text-sm text-danger">{settings.error instanceof Error ? settings.error.message : "Không tải được cài đặt."}</div>;
  }

  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p className="label">Cấu hình</p>
          <h1 className="mt-2 text-3xl font-bold tracking-tight text-ink">Cài đặt dịch</h1>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-muted">
            Các thiết lập không chứa API key. Thay đổi được lưu vào <code className="rounded bg-surface-2 px-1.5 py-0.5 text-xs">config.yaml</code>.
          </p>
        </div>
        <div className="flex gap-2">
          <Link to="/keys" className="btn btn-secondary">API keys</Link>
          <Link to="/config" className="btn btn-ghost">YAML nâng cao</Link>
        </div>
      </div>

      {saved ? <div className="rounded-xl border border-emerald-500/30 bg-emerald-500/10 p-3 text-sm text-emerald-300">Đã lưu cài đặt.</div> : null}
      {save.isError ? (
        <div className="rounded-xl border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-300">
          {save.error instanceof Error ? save.error.message : "Không lưu được cài đặt."}
        </div>
      ) : null}

      <form className="space-y-4" onSubmit={(event) => { event.preventDefault(); save.mutate(); }}>
        {settings.data.groups.map((group) => (
          <GroupCard
            key={group.label}
            group={group}
            values={values}
            onChange={(path, value) => setValues((prev) => ({ ...prev, [path]: value }))}
          />
        ))}

        <div className="flex flex-wrap items-center gap-3">
          <button className="btn btn-primary" type="submit" disabled={save.isPending}>
            {save.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
            Lưu cài đặt
          </button>
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => discordTest.mutate()}
            disabled={discordTest.isPending}
          >
            {discordTest.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Bell className="h-4 w-4" />}
            Test Discord
          </button>
          {discord ? <span className="text-xs text-muted">{discord}</span> : null}
        </div>
      </form>
    </div>
  );
}
