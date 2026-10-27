import { useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Bell, FileCode2, Loader2, Save, ShieldAlert } from "lucide-react";
import { getAdvancedConfig, saveAdvancedConfig, testDiscord } from "../../lib/api";

export function AdvancedConfigPage() {
  const config = useQuery({ queryKey: ["advanced-config"], queryFn: getAdvancedConfig });
  const [text, setText] = useState("");
  const [message, setMessage] = useState("");

  useEffect(() => {
    if (config.data) setText(config.data.text);
  }, [config.data]);

  const save = useMutation({
    mutationFn: () => saveAdvancedConfig(text),
    onSuccess: (data) => {
      setText(data.text);
      setMessage("Đã lưu config.yaml (backup: config.yaml.bak).");
    },
    onError: (error) => setMessage(error instanceof Error ? error.message : "Không lưu được config."),
  });
  const discord = useMutation({
    mutationFn: testDiscord,
    onSuccess: (data) => setMessage(data.success ? "Đã gửi thông báo Discord thử." : "Gửi Discord thất bại — kiểm tra webhook."),
    onError: (error) => setMessage(error instanceof Error ? error.message : "Gửi Discord thất bại."),
  });

  if (config.isLoading) {
    return <div className="flex items-center gap-3 p-8 text-sm text-muted"><Loader2 className="h-4 w-4 animate-spin text-accent" /> Đang tải config…</div>;
  }
  if (config.isError) {
    return <div className="card p-8 text-sm text-danger">{config.error instanceof Error ? config.error.message : "Không tải được config."}</div>;
  }

  return (
    <div className="mx-auto max-w-5xl space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p className="label">Cấu hình nâng cao</p>
          <h1 className="mt-2 text-3xl font-bold tracking-tight text-ink">config.yaml</h1>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-muted">
            Chỉnh trực tiếp toàn bộ cấu hình. API key hiển thị dạng <code className="rounded bg-surface-2 px-1.5 py-0.5 text-xs">********</code>; giữ nguyên để không làm mất key.
          </p>
        </div>
        <div className="flex gap-2">
          <Link to="/config/settings" className="btn btn-secondary">Cài đặt GUI</Link>
          <Link to="/keys" className="btn btn-ghost">API keys</Link>
        </div>
      </div>

      <div className="flex items-start gap-3 rounded-xl border border-amber-500/30 bg-amber-500/10 p-4 text-sm text-amber-200">
        <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" />
        <p>YAML sai định dạng sẽ bị từ chối và file cũ được giữ nguyên. Mỗi lần lưu thành công đều tạo backup <code>config.yaml.bak</code>.</p>
      </div>

      <form className="card space-y-4 p-5" onSubmit={(event) => { event.preventDefault(); save.mutate(); }}>
        <div className="flex flex-wrap items-center gap-2 text-xs text-faint">
          <FileCode2 className="h-4 w-4" />
          <span className="font-mono">{config.data?.config_path}</span>
        </div>
        <textarea
          className="min-h-[32rem] w-full rounded-xl border border-line bg-black/25 p-4 font-mono text-xs leading-6 text-ink outline-none focus:border-accent"
          spellCheck={false}
          value={text}
          onChange={(event) => setText(event.target.value)}
        />
        <div className="flex flex-wrap items-center gap-3">
          <button className="btn btn-primary" type="submit" disabled={save.isPending}>
            {save.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
            Lưu config
          </button>
          <button className="btn btn-secondary" type="button" onClick={() => discord.mutate()} disabled={discord.isPending}>
            {discord.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Bell className="h-4 w-4" />}
            Test Discord
          </button>
          {message ? <span className="text-xs text-muted">{message}</span> : null}
        </div>
      </form>
    </div>
  );
}
