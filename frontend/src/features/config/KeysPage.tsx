import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { KeyRound, Loader2, Plus, RefreshCw, Save, Trash2, X } from "lucide-react";
import {
  addProvider,
  fetchModels,
  getKeys,
  removeProvider,
  saveKeys,
  type ProviderInfo,
} from "../../lib/api";
import { cn } from "../../lib/cn";

type Draft = {
  name: string;
  api_key: string;
  base_url: string;
  model: string;
  models: string;
  clear_models: boolean;
};

function toDraft(provider: ProviderInfo): Draft {
  return {
    name: provider.name,
    api_key: "",
    base_url: provider.base_url || "",
    model: provider.model || "",
    models: provider.models_text || "",
    clear_models: false,
  };
}

export function KeysPage() {
  const queryClient = useQueryClient();
  const keys = useQuery({ queryKey: ["keys"], queryFn: getKeys });
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [status, setStatus] = useState<Record<string, string>>({});
  const [saved, setSaved] = useState(false);
  const [newProvider, setNewProvider] = useState({ name: "", base_url: "", api_key: "", model: "" });
  const [addError, setAddError] = useState("");

  useEffect(() => {
    if (!keys.data) return;
    setDrafts((prev) => {
      const next = { ...prev };
      for (const provider of keys.data.providers) {
        if (!next[provider.name]) next[provider.name] = toDraft(provider);
      }
      return next;
    });
  }, [keys.data]);

  const providers = keys.data?.providers || [];
  const updateDraft = (name: string, patch: Partial<Draft>) =>
    setDrafts((prev) => ({ ...prev, [name]: { ...(prev[name] || { name }), ...patch } }));

  const save = useMutation({
    mutationFn: () =>
      saveKeys(
        providers.map((provider) => {
          const draft = drafts[provider.name] || toDraft(provider);
          return {
            name: provider.name,
            api_key: draft.api_key,
            base_url: draft.base_url,
            model: draft.model,
            models: draft.models,
            clear_models: draft.clear_models,
          };
        }),
      ),
    onSuccess: async (data) => {
      setSaved(true);
      setDrafts(Object.fromEntries(data.providers.map((provider) => [provider.name, toDraft(provider)])));
      queryClient.setQueryData(["keys"], data);
      window.setTimeout(() => setSaved(false), 2500);
    },
  });

  const remove = useMutation({
    mutationFn: (name: string) => removeProvider(name),
    onSuccess: (data) => {
      queryClient.setQueryData(["keys"], data);
      setDrafts(Object.fromEntries(data.providers.map((provider) => [provider.name, toDraft(provider)])));
    },
  });

  const add = useMutation({
    mutationFn: () => addProvider(newProvider),
    onSuccess: (data) => {
      queryClient.setQueryData(["keys"], data);
      setNewProvider({ name: "", base_url: "", api_key: "", model: "" });
      setAddError("");
    },
    onError: (error) => setAddError(error instanceof Error ? error.message : "Không thêm được provider."),
  });

  const [fetching, setFetching] = useState<Record<string, boolean>>({});
  const liveModels = async (name: string) => {
    setFetching((prev) => ({ ...prev, [name]: true }));
    setStatus((prev) => ({ ...prev, [name]: "Đang lấy model…" }));
    try {
      const data = await fetchModels(name);
      const current = (drafts[name]?.models || "").split("\n").map((line) => line.trim()).filter(Boolean);
      const seen = new Set(current);
      let added = 0;
      for (const model of data.models || []) {
        if (!seen.has(model)) {
          seen.add(model);
          current.push(model);
          added += 1;
        }
      }
      updateDraft(name, { models: current.join("\n"), clear_models: false });
      setStatus((prev) => ({ ...prev, [name]: added ? `Đã thêm ${added} model` : "Không có model mới" }));
    } catch (error) {
      setStatus((prev) => ({ ...prev, [name]: error instanceof Error ? error.message : "Lấy model thất bại" }));
    } finally {
      setFetching((prev) => ({ ...prev, [name]: false }));
    }
  };

  const dirty = useMemo(() => {
    if (!keys.data) return false;
    return providers.some((provider) => {
      const draft = drafts[provider.name];
      if (!draft) return false;
      return Boolean(draft.api_key || draft.clear_models) ||
        draft.base_url !== (provider.base_url || "") ||
        draft.model !== (provider.model || "") ||
        draft.models !== (provider.models_text || "");
    });
  }, [drafts, keys.data, providers]);

  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <div>
        <p className="label">Cấu hình</p>
        <h1 className="mt-2 text-3xl font-bold tracking-tight text-ink">API Keys</h1>
        <p className="mt-2 max-w-2xl text-sm leading-6 text-muted">
          Key được lưu trong <code className="rounded bg-surface-2 px-1.5 py-0.5 text-xs">config.yaml</code>. Để trống để giữ key hiện có.
        </p>
      </div>

      {saved ? <div className="rounded-xl border border-emerald-500/30 bg-emerald-500/10 p-3 text-sm text-emerald-300">Đã lưu cấu hình.</div> : null}
      {keys.isError ? <div className="rounded-xl border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-300">{keys.error instanceof Error ? keys.error.message : "Không tải được cấu hình."}</div> : null}

      <form className="space-y-4" onSubmit={(event) => { event.preventDefault(); save.mutate(); }}>
        {providers.map((provider) => {
          const draft = drafts[provider.name] || toDraft(provider);
          return (
            <section key={provider.name} className="card p-5">
              <div className="flex flex-wrap items-center gap-2">
                <KeyRound className="h-4 w-4 text-accent" />
                <h2 className="font-semibold text-ink">{provider.name}</h2>
                <span className="rounded-full bg-surface-2 px-2 py-0.5 text-[11px] text-muted">{provider.type}</span>
                <span className={cn("text-xs", provider.ready ? "text-emerald-300" : "text-warning")}>
                  {provider.ready ? (provider.needs_key ? "Đã có key" : "Local") : "Thiếu key"}
                </span>
                {provider.removable ? (
                  <button
                    type="button"
                    className="btn btn-ghost ml-auto min-h-8 px-2 text-xs text-red-300"
                    onClick={() => {
                      if (window.confirm(`Xoá provider ${provider.name}?`)) remove.mutate(provider.name);
                    }}
                  >
                    <Trash2 className="h-3.5 w-3.5" /> Xoá
                  </button>
                ) : null}
              </div>

              <div className="mt-4 grid gap-4 sm:grid-cols-2">
                <label className="space-y-2">
                  <span className="label">API key</span>
                  <input
                    className="input"
                    type="password"
                    autoComplete="off"
                    value={draft.api_key}
                    onChange={(event) => updateDraft(provider.name, { api_key: event.target.value })}
                    placeholder={provider.has_key ? "Đã đặt key — để trống để giữ" : "Nhập API key"}
                  />
                </label>
                {provider.show_base ? (
                  <label className="space-y-2">
                    <span className="label">Base URL</span>
                    <input
                      className="input font-mono text-xs"
                      value={draft.base_url}
                      onChange={(event) => updateDraft(provider.name, { base_url: event.target.value })}
                      placeholder="https://api.example.com/v1"
                    />
                  </label>
                ) : null}
                <label className="space-y-2">
                  <span className="label">Model mặc định</span>
                  <input
                    className="input font-mono text-xs"
                    value={draft.model}
                    onChange={(event) => updateDraft(provider.name, { model: event.target.value })}
                    placeholder="Để trống dùng mặc định"
                  />
                </label>
              </div>

              <details className="mt-4 rounded-xl border border-line bg-surface-2/40 p-3">
                <summary className="cursor-pointer text-xs font-medium text-muted">Danh sách model gợi ý</summary>
                <div className="mt-3 space-y-2">
                  <textarea
                    className="input min-h-24 font-mono text-xs"
                    rows={4}
                    value={draft.models}
                    onChange={(event) => updateDraft(provider.name, { models: event.target.value, clear_models: false })}
                    placeholder="Mỗi dòng một model"
                  />
                  <div className="flex flex-wrap items-center gap-3">
                    <button
                      type="button"
                      className="btn btn-ghost min-h-8 px-2 text-xs"
                      onClick={() => void liveModels(provider.name)}
                      disabled={fetching[provider.name]}
                    >
                      <RefreshCw className={cn("h-3.5 w-3.5", fetching[provider.name] && "animate-spin")} /> Lấy model live
                    </button>
                    <label className="flex items-center gap-2 text-xs text-muted">
                      <input
                        type="checkbox"
                        className="accent-[var(--color-accent)]"
                        checked={draft.clear_models}
                        onChange={(event) => updateDraft(provider.name, { clear_models: event.target.checked })}
                      />
                      Xoá danh sách
                    </label>
                    <span className="text-xs text-faint">{status[provider.name] || ""}</span>
                  </div>
                </div>
              </details>
            </section>
          );
        })}

        <div className="flex flex-wrap items-center gap-3">
          <button className="btn btn-primary" type="submit" disabled={save.isPending || !dirty}>
            {save.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
            Lưu tất cả
          </button>
          <Link to="/config/settings" className="btn btn-secondary">Cài đặt GUI</Link>
          <Link to="/config" className="btn btn-ghost">YAML nâng cao</Link>
        </div>
      </form>

      <section className="card p-5">
        <h2 className="flex items-center gap-2 text-sm font-semibold text-ink"><Plus className="h-4 w-4 text-accent" /> Thêm provider OpenAI-compatible</h2>
        <p className="mt-1 text-xs leading-5 text-muted">Dùng cho endpoint tương thích OpenAI như Ollama, LM Studio, proxy nội bộ…</p>
        {addError ? <p className="mt-3 text-xs text-danger">{addError}</p> : null}
        <form className="mt-4 grid gap-3 sm:grid-cols-2" onSubmit={(event) => { event.preventDefault(); add.mutate(); }}>
          <label className="space-y-2">
            <span className="label">Tên</span>
            <input className="input" value={newProvider.name} onChange={(event) => setNewProvider((prev) => ({ ...prev, name: event.target.value }))} pattern="[A-Za-z0-9_\-]+" required placeholder="my-provider" />
          </label>
          <label className="space-y-2">
            <span className="label">Base URL</span>
            <input className="input font-mono text-xs" value={newProvider.base_url} onChange={(event) => setNewProvider((prev) => ({ ...prev, base_url: event.target.value }))} required placeholder="http://127.0.0.1:11434/v1" />
          </label>
          <label className="space-y-2">
            <span className="label">API key</span>
            <input className="input" type="password" value={newProvider.api_key} onChange={(event) => setNewProvider((prev) => ({ ...prev, api_key: event.target.value }))} autoComplete="off" />
          </label>
          <label className="space-y-2">
            <span className="label">Model</span>
            <input className="input font-mono text-xs" value={newProvider.model} onChange={(event) => setNewProvider((prev) => ({ ...prev, model: event.target.value }))} placeholder="llama3" />
          </label>
          <div className="sm:col-span-2">
            <button className="btn btn-secondary" type="submit" disabled={add.isPending}>
              {add.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}
              Thêm provider
            </button>
          </div>
        </form>
      </section>

      <p className="flex items-center gap-2 text-xs text-faint"><X className="h-3.5 w-3.5" /> Key không bao giờ được gửi xuống trình duyệt sau khi lưu.</p>
    </div>
  );
}
