import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useSearchParams, Link } from "react-router-dom";
import { AlertTriangle, ArrowLeft, Loader2, Play, RefreshCw, Wand2 } from "lucide-react";
import { createJob, fetchModels, getKeys, getLibrary, getTtsStatus } from "../../lib/api";
import { cn } from "../../lib/cn";

type FormState = {
  engine: string;
  model: string;
  input: string;
  output: string;
  configPath: string;
  fromChapter: string;
  toChapter: string;
  fromLang: string;
  toLang: string;
  description: string;
  openaiBatch: boolean;
  resetCheckpoint: boolean;
  disableResume: boolean;
  ttsDiskCache: boolean;
  ttsVoice: string;
};

const initialForm: FormState = {
  engine: "",
  model: "",
  input: "",
  output: "",
  configPath: "",
  fromChapter: "",
  toChapter: "",
  fromLang: "EN",
  toLang: "VI",
  description: "",
  openaiBatch: false,
  resetCheckpoint: false,
  disableResume: false,
  ttsDiskCache: false,
  ttsVoice: "",
};

function Field({
  label,
  hint,
  children,
  className,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <label className={cn("block space-y-2", className)}>
      <span className="label">{label}</span>
      {children}
      {hint ? <span className="block text-xs leading-5 text-faint">{hint}</span> : null}
    </label>
  );
}

export function NewJobPage() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const queryClient = useQueryClient();
  const [form, setForm] = useState<FormState>(() => ({
    ...initialForm,
    input: searchParams.get("input") || "",
  }));
  const [outputTouched, setOutputTouched] = useState(false);

  const keys = useQuery({ queryKey: ["keys"], queryFn: getKeys });
  const library = useQuery({ queryKey: ["library"], queryFn: () => getLibrary() });
  const tts = useQuery({ queryKey: ["tts-status"], queryFn: getTtsStatus, staleTime: 60_000 });

  useEffect(() => {
    if (form.engine || !keys.data?.providers.length) return;
    const providers = keys.data.providers;
    // Book translation in this project uses the local webai proxy.  Prefer it
    // over a keyed cloud provider when a job form is opened for the first time.
    const first = providers.find((provider) => provider.name === "webai" && provider.ready)
      || providers.find((provider) => provider.ready)
      || providers[0];
    setForm((prev) => ({ ...prev, engine: first.name }));
  }, [form.engine, keys.data?.providers]);

  useEffect(() => {
    if (keys.data?.config_path && !form.configPath) {
      setForm((prev) => ({ ...prev, configPath: keys.data?.config_path || "" }));
    }
  }, [form.configPath, keys.data?.config_path]);

  useEffect(() => {
    if (outputTouched || !form.input) return;
    setForm((prev) => ({ ...prev, output: `${prev.input}.translated.epub` }));
  }, [form.input, outputTouched]);

  const models = useQuery({
    queryKey: ["job-models", form.engine],
    queryFn: () => fetchModels(form.engine),
    enabled: Boolean(form.engine),
    retry: 0,
    staleTime: 60_000,
  });

  const suggestions = useMemo(() => {
    const live = models.data?.models?.filter(Boolean) || [];
    if (live.length) return live;
    return keys.data?.model_suggestions?.[form.engine] || [];
  }, [form.engine, keys.data?.model_suggestions, models.data?.models]);

  const books = useMemo(() => {
    const groups = library.data?.groups || [];
    return groups
      .flatMap((group) => group.entries)
      .map((entry) => ({ path: entry.path, label: entry.name }))
      .sort((a, b) => a.label.localeCompare(b.label));
  }, [library.data?.groups]);

  const mutation = useMutation({
    mutationFn: createJob,
    onSuccess: async ({ job }) => {
      await queryClient.invalidateQueries({ queryKey: ["jobs"] });
      navigate(`/jobs/${job.id}`);
    },
  });

  const missing = keys.data?.engine_missing || [];
  const update = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }));

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    if (!form.engine || !form.input.trim()) return;
    mutation.mutate({
      engine: form.engine,
      model: form.model.trim() || null,
      input: form.input.trim(),
      output: form.output.trim() || `${form.input.trim()}.translated.epub`,
      config_path: form.configPath,
      from_chapter: form.fromChapter ? Number(form.fromChapter) : null,
      to_chapter: form.toChapter ? Number(form.toChapter) : null,
      from_lang: form.fromLang || "EN",
      to_lang: form.toLang || "VI",
      description: form.description.trim() || null,
      openai_batch: form.openaiBatch,
      reset_checkpoint: form.resetCheckpoint,
      disable_resume: form.disableResume,
      tts_disk_cache: form.ttsDiskCache,
      tts_voice: form.ttsVoice || null,
    });
  };

  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <p className="label">Dịch sách</p>
          <h1 className="mt-2 text-3xl font-bold tracking-tight text-ink">Job dịch mới</h1>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-muted">
            Chọn sách, engine và khoảng chương. Job chạy nền, có thể theo dõi log, dừng và tiếp tục.
          </p>
        </div>
        <button className="btn btn-ghost shrink-0" onClick={() => navigate(-1)}>
          <ArrowLeft className="h-4 w-4" /> Quay lại
        </button>
      </div>

      <form className="card space-y-6 p-5 sm:p-7" onSubmit={submit}>
        <div className="grid gap-5 sm:grid-cols-2">
          <Field label="Engine" hint={missing.length ? `Chưa có key: ${missing.join(", ")}` : "Engine lấy từ config.yaml."}>
            <select
              className="input"
              value={form.engine}
              onChange={(event) => update("engine", event.target.value)}
              required
            >
              <option value="">Chọn engine…</option>
              {keys.data?.providers.map((provider) => (
                <option key={provider.name} value={provider.name}>
                  {provider.name}{provider.ready ? "" : " (thiếu key)"}
                </option>
              ))}
            </select>
          </Field>

          <Field label="Model" hint="Để trống để dùng model mặc định của engine.">
            <div className="flex gap-2">
              <input
                className="input"
                list="job-models"
                value={form.model}
                onChange={(event) => update("model", event.target.value)}
                placeholder="Mặc định"
              />
              <button
                type="button"
                className="btn btn-secondary shrink-0 px-3"
                onClick={() => void models.refetch()}
                disabled={!form.engine || models.isFetching}
                aria-label="Tải lại danh sách model"
                title="Tải lại danh sách model"
              >
                <RefreshCw className={cn("h-4 w-4", models.isFetching && "animate-spin")} />
              </button>
            </div>
            <datalist id="job-models">
              {suggestions.map((model) => <option key={model} value={model} />)}
            </datalist>
            {models.isError ? <span className="block text-xs text-warning">Không lấy được model live; dùng gợi ý trong config.</span> : null}
          </Field>

          <Field label="Giọng TTS" hint="Voice này được dùng cho disk cache; reader vẫn có thể chọn voice riêng.">
            <select className="input" value={form.ttsVoice} onChange={(event) => update("ttsVoice", event.target.value)}>
              <option value="">Mặc định của TTS</option>
              {tts.data?.voices.map((voice) => (
                <option key={voice.id} value={voice.id}>{voice.label || voice.id}</option>
              ))}
            </select>
          </Field>

          <Field label="Sách nguồn" hint="Đường dẫn tuyệt đối tới file EPUB." className="sm:col-span-2">
            <input
              className="input font-mono text-xs"
              list="job-books"
              value={form.input}
              onChange={(event) => update("input", event.target.value)}
              placeholder="F:\\…\\book.epub"
              required
            />
            <datalist id="job-books">
              {books.map((book) => <option key={book.path} value={book.path}>{book.label}</option>)}
            </datalist>
          </Field>

          <Field label="File đầu ra" className="sm:col-span-2">
            <input
              className="input font-mono text-xs"
              value={form.output}
              onChange={(event) => {
                setOutputTouched(true);
                update("output", event.target.value);
              }}
              placeholder="Mặc định: <input>.translated.epub"
            />
          </Field>

          <Field label="Chương bắt đầu">
            <input
              className="input"
              type="number"
              min={1}
              value={form.fromChapter}
              onChange={(event) => update("fromChapter", event.target.value)}
              placeholder="1"
            />
          </Field>
          <Field label="Chương kết thúc">
            <input
              className="input"
              type="number"
              min={1}
              value={form.toChapter}
              onChange={(event) => update("toChapter", event.target.value)}
              placeholder="Đến hết sách"
            />
          </Field>

          <Field label="Ngôn ngữ nguồn">
            <input className="input" value={form.fromLang} onChange={(event) => update("fromLang", event.target.value)} />
          </Field>
          <Field label="Ngôn ngữ đích">
            <input className="input" value={form.toLang} onChange={(event) => update("toLang", event.target.value)} />
          </Field>

          <Field label="Mô tả / ghi chú" className="sm:col-span-2">
            <input
              className="input"
              value={form.description}
              onChange={(event) => update("description", event.target.value)}
              placeholder="Ví dụ: truyện đô thị, giữ xưng hô thân mật…"
            />
          </Field>

          <Field label="Config path" hint="Thường để mặc định config.yaml ở thư mục dự án." className="sm:col-span-2">
            <input
              className="input font-mono text-xs"
              value={form.configPath}
              onChange={(event) => update("configPath", event.target.value)}
            />
          </Field>
        </div>

        <div className="grid gap-2 rounded-xl border border-line bg-surface-2/60 p-4 text-sm">
          {[
            ["openaiBatch", "Dùng OpenAI Batch API", "Chỉ có tác dụng với engine hỗ trợ batch."],
            ["resetCheckpoint", "Xoá checkpoint trước khi chạy", "Bắt đầu lại từ chương đầu, không nối checkpoint cũ."],
            ["disableResume", "Không tự động resume", "Bỏ qua checkpoint hiện có."],
            ["ttsDiskCache", "Tạo disk cache cho TTS", "Sau khi dịch xong, pre-render audio toàn bộ EPUB vào cache/tts để đọc ngay."],
          ].map(([key, label, hint]) => (
            <label key={key} className="flex items-start gap-3">
              <input
                type="checkbox"
                className="mt-1 h-4 w-4 accent-[var(--color-accent)]"
                checked={form[key as keyof FormState] as boolean}
                onChange={(event) => update(key as keyof FormState, event.target.checked as never)}
              />
              <span>
                <span className="font-medium text-ink">{label}</span>
                <span className="mt-0.5 block text-xs text-faint">{hint}</span>
              </span>
            </label>
          ))}
        </div>

        {mutation.isError ? (
          <div className="flex items-start gap-2 rounded-xl border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-300">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <span>{mutation.error instanceof Error ? mutation.error.message : "Không tạo được job."}</span>
          </div>
        ) : null}

        <div className="flex flex-col-reverse gap-3 border-t border-line pt-5 sm:flex-row sm:items-center sm:justify-between">
          <span className="text-xs text-faint">
            Job sẽ chạy nền trên server; đóng tab không làm dừng job.
          </span>
          <button
            className="btn btn-primary"
            type="submit"
            disabled={mutation.isPending || !form.engine || !form.input.trim()}
          >
            {mutation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
            Bắt đầu dịch
          </button>
        </div>
      </form>

      <div className="card flex items-start gap-3 p-4 text-sm text-muted">
        <Wand2 className="mt-0.5 h-4 w-4 shrink-0 text-accent" />
        <p>
          Cần cấu hình engine? Mở <Link className="text-accent hover:underline" to="/keys">API Keys</Link> hoặc{" "}
          <Link className="text-accent hover:underline" to="/config">YAML nâng cao</Link>.
        </p>
      </div>
    </div>
  );
}
