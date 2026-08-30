# py-translate-book

Công cụ dịch EPUB tiếng Việt (hoặc ngôn ngữ bất kỳ) chạy local, dùng các LLM
(OpenAI / Google Gemini / WebAI local proxy). Kèm **web UI quản lý** (Flask,
local-only) để theo dõi tiến trình realtime, quản lý thư viện sách, resume /
checkpoint, sửa `config.yaml` và xem ảnh minh họa.

> Mọi thứ chạy **trên máy bạn**. Web UI chỉ bind `127.0.0.1:5000`, không có
> login. Đừng đưa ra mạng công cộng nếu không tự chịu trách nhiệm bảo mật.

English version: see [README.md](README.md).

---

## Tính năng

- **Dịch EPUB** giữ nguyên cấu trúc HTML (split theo `<p>` / `<br>`, ghép lại).
- **Nhiều engine**: `openai`, `gemini`, `webai` (proxy local), cộng **mọi provider
  OpenAI-compatible** (Groq, DeepSeek, OpenRouter, Ollama, …) chỉ cần khai báo
  `type: openai_compatible` trong config.
- **Ghi đè model mỗi job**: ô `Model` tuỳ chọn trên form tạo job (để trống = mặc
  định engine), gợi ý model từ danh sách `models:` trong config.
- **Trang API Keys** (`/config/keys`): dán/sửa key từng provider qua form (kèm
  base URL + model) — không cần sửa `config.yaml` bằng tay.
- **Checkpoint / resume**: dịch dở có thể tiếp tục từ chương cuối cùng thành công.
- **Consistency**: trích quy tắc dịch (cách xưng hô, tên riêng) để nhất quán xuyên chương.
- **Illustration** (tùy chọn): sinh ảnh minh họa vào `images/generated`.
- **Discord notify**: báo khi xong job (+ thống kê chunk/file).
- **Web UI**: dashboard, tạo job, log realtime (SSE), thư viện sách, preview chương,
  resume, editor `config.yaml` (che API key), test Discord, gallery ảnh.
- **Offline tests**: suite `unittest` không tốn token AI.

---

## Yêu cầu

- **Python >= 3.10**
- pip packages trong `requirements.txt`

---

## Cài đặt

```bat
# 1) (khuyên dùng) tạo env
conda create -n translate-book python=3.11 -y
conda activate translate-book

# hoặc venv:
python -m venv .venv
.venv\Scripts\activate

# 2) cài dependency
pip install -r requirements.txt
```

## Cấu hình

File `config.yaml` bị **git-ignore** (chứa API key). Tạo nó từ mẫu:

```bat
cp config.example.yaml config.yaml
```

**Cách dễ nhất để điền key — form trên web:**

```bat
python -m webui.app     # mở http://127.0.0.1:5000 → tab "API Keys"
```

Trang **API Keys** hiện 1 thẻ cho mỗi provider (openai, gemini, webai, groq,
deepseek, openrouter, ollama, …). Dán key rồi bấm Lưu — ô để trống giữ nguyên
key cũ, tự động backup `.bak`. Không bao giờ phải sửa YAML tay.

### Schema provider

```yaml
openai:
  api_key: "sk-..."        # bắt buộc
  model: gpt-5.4-mini      # model mặc định (tuỳ chọn)
  models: [gpt-5.5, gpt-5.4-mini, ...]   # gợi ý dropdown (tuỳ chọn)
gemini:
  api_key: "AIza..."
  model: gemini-3.5-flash-lite
  analysis_model: gemini-3.5-flash-lite
  models: [...]
webai:
  base_url: "http://localhost:6969"   # proxy local
  model: "gemini-flash"               # forward nguyên văn lên server của bạn
# Provider OpenAI-compatible — thêm section nào cũng được, chỉ cần type
groq:
  type: openai_compatible
  base_url: https://api.groq.com/openai/v1
  model: openai/gpt-oss-120b
  api_key: "gsk_..."
deepseek:
  type: openai_compatible
  base_url: https://api.deepseek.com
  model: deepseek-v4-flash
  api_key: ""
ollama:
  type: openai_compatible
  base_url: http://localhost:11434/v1    # local, không cần key
  model: qwen3:14b
  api_key: ""
```

- `models:` là tuỳ chọn — chỉ để gợi ý dropdown; bạn có thể gõ tên model bất kỳ
  (để trống = mặc định của engine).
- Provider local (webai, Ollama) không cần key; provider cloud thiếu key sẽ hiện
  "⛔ chưa có key" ở trang API Keys và được cảnh báo trên form tạo job.

> **Lưu ý model (Gemini / WebAI):** các tên `gemini-3.*` và `gemini-2.*` cũ đã
> bị gỡ. Dùng `gemini-flash` / `gemini-pro` / `gemini-flash-lite`. Engine `webai`
> forward thẳng `webai.model` lên server local, nên đặt đúng tên server chấp nhận.

---

## Dùng qua CLI (`main.py`)

```bat
# Xem help
python main.py --help

# Dịch chapter 1..10 bằng WebAI
python main.py --engine webai --input book.epub --output book.dich.epub \
               --from-chapter 1 --to-chapter 10 --from-lang EN --to-lang VI

# Dùng model Groq (engine = tên section trong config)
python main.py --engine groq --input book.epub --output book.dich.epub

# Ghi đè model cho riêng job này
python main.py --engine groq --model openai/gpt-oss-20b \
               --input book.epub --output book.dich.epub

# Dùng Ollama local (không cần key)
python main.py --engine ollama --input book.epub --output book.dich.epub

# Preview nội dung 1 chương (không dịch)
python main.py --input book.epub --preview-chapter 3 --preview-chars 4000

# Test Discord webhook
python main.py --test-discord-webhook --config config.yaml

# Resume: bỏ qua checkpoint cũ và chạy lại từ đầu
python main.py --engine webai --input book.epub --output book.dich.epub --reset-checkpoint

# Tắt resume (luôn dịch từ đầu, không đọc checkpoint)
python main.py --engine webai --input book.epub --output book.dich.epub --disable-resume
```

Output: `<output>.epub` + `<output>.checkpoint.json` (lưu chương đã xong).

---

## Dùng qua Web UI (`webui/`)

```bat
# Cách chuẩn (khuyên dùng):
python -m webui.app

# Hoặc chạy trực tiếp file (vẫn được, code đã tự thêm project root vào sys.path):
python webui/app.py
```

Mở **http://127.0.0.1:5000**.

| Trang | Chức năng |
|-------|-----------|
| `/` | Dashboard: jobs gần đây + thư viện EPUB (nhóm theo tựa sách, badge trạng thái) |
| `/jobs/new` | Tạo job (engine + model tuỳ chọn, input/output, chapter range, lang, …) |
| `/jobs/<id>` | Theo dõi log realtime (SSE) + thanh progress + nút Dừng |
| `/books` | Thư viện gom nhóm: nguồn + các bản dịch cùng tựa, badge ✅/⏳/❓/📖, lọc `?filter=done|partial|assumed|untranslated`, preview chương |
| `/config/keys` | **API Keys**: form nhập/sửa key từng provider + base URL + model |
| `/config` | Sửa `config.yaml` (API key bị che, backup `.bak`), test Discord |
| `/illustrations` | Gallery ảnh minh họa đã sinh |

**Dừng job:** nút Dừng dừng an toàn tại **ranh giới chương** (không ngắt giữa một
chunk đang gọi API).

---

## Cấu trúc dự án

```
main.py                  CLI entry (gọi translator.job.run_translation)
translator/
  translator.py          Translator.core: split -> translate -> ghép, retry, consistency
  job.py                 run_translation(): orchestration chung cho CLI & web
                         (checkpoint/resume, illustration, discord, stats, progress_cb)
  epub_utils.py          đọc/ghi EPUB, duyệt chapter, inject manifest
  html_utils.py          split/assemble HTML, normalize <br>
  illustration.py        sinh ảnh minh họa
  discord_notifier.py    gửi Discord webhook
  engines/                TranslationEngine: openai / gemini / webai / compatible (+ base)
webui/
  app.py                 Flask app (routes + SSE)
  jobs.py                JobRegistry + Job (trạng thái, log, progress; lưu webui/jobs/)
  core_runner.py         chạy run_translation trong thread, forward log/event -> SSE
  config_store.py        đọc/ghi config.yaml an toàn (mask key + backup)
  books.py               liệt kê EPUB, preview chương, phân loại trạng thái dịch
                         (checkpoint/job/đuôi tên) và gom nhóm nguồn + bản dịch
  templates/  static/    giao diện (gồm keys.html — form API Keys)
tests/                   unittest offline (không gọi AI)
requirements.txt         dependency
config.example.yaml      mẫu cấu hình
```

---

## Chạy test (offline, không tốn tiền)

```bat
python -m unittest tests.test_html_utils tests.test_translator \
                     tests.test_epub_utils tests.test_job_core tests.test_engines \
                     tests.test_webui -v
```

- `test_job_core`: chạy `run_translation` với engine giả lập + EPUB nhỏ → assert ra
  output, checkpoint, stats.
- `test_engines`: factory đa provider (openai-compatible, gợi ý model, plumbing
  `model` vào `build_engine` + checkpoint).
- `test_webui`: Flask test client + job chạy thật (engine/Discord bị stub) → assert
  sinh được EPUB; kèm test trang API Keys (lưu/giữ nguyên/không rò key).

---

## Lưu ý bảo mật

- `config.yaml`, `*.checkpoint.json`, `*.p12`, `webui/jobs/` đều bị git-ignore.
- Web UI **không có xác thực**. Chỉ chạy local. Nếu cần xa, hãy qua reverse proxy
  có auth (VPN / Tailscale / Cloudflare Access) — đừng expose trần.
- API key nằm trong `config.yaml` trên máy local; đừng commit.

---

## Khắc phục nhanh

| Triệu chứng | Nguyên nhân / fix |
|-------------|------------------|
| `ModuleNotFoundError: No module named 'webui'` | chạy `python webui/app.py` sai thư mục. Dùng `python -m webui.app` từ root. |
| Dịch chạy nhưng "sai model" | `webai.model` đang là tên cũ (`gemini-3.*`). Đổi thành `gemini-flash`. |
| Job treo / retry vô hạn | do model trả về mất thẻ HTML; code đã giới hạn retry (max 30) + fallback split. |
| Web UI không hiện log | job chạy trong thread; reload trang `/jobs/<id>` sẽ tải lại từ buffer. |
