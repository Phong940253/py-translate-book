# py-translate-book

A **local** EPUB translator (e.g. to Vietnamese, or any language pair) powered by
LLMs (OpenAI / Google Gemini / WebAI local proxy). Includes a **Flask management
web UI** (local-only) for realtime progress, a book library, resume/checkpoint,
editing `config.yaml`, and browsing illustrations.

> Everything runs **on your machine**. The web UI only binds `127.0.0.1:5000` and
> has **no login**. Do not expose it to the public internet unless you accept the
> security responsibility.

---

## Features

- **Translate EPUB** while preserving HTML structure (split on `<p>` / `<br>`, reassemble).
- **Multiple engines**: `openai`, `gemini`, `webai` (local proxy), plus any
  **OpenAI-compatible provider** (Groq, DeepSeek, OpenRouter, Ollama, …) added
  purely via config (`type: openai_compatible`).
- **Per-job model override**: optional `Model` field on the create-job form
  (blank = engine default), model suggestions from config `models:`.
- **API Keys page** (`/config/keys`): paste/update each provider's key through
  a form, with base URL + model — no manual `config.yaml` editing.
- **Checkpoint / resume**: an interrupted job can continue from the last completed chapter.
- **Consistency**: extracts translation rules (honorifics, character names) to stay
  consistent across chapters.
- **Illustration** (optional): generates illustrations into `images/generated`.
- **Discord notifications**: ping when a job finishes (+ chunk/file stats).
- **Web UI**: dashboard, job creation, realtime log (SSE), book library, chapter
  preview, resume, `config.yaml` editor (API keys masked), Discord test, image gallery.
- **Offline tests**: a `unittest` suite that costs no AI tokens.

---

## Requirements

- **Python >= 3.10**
- pip packages listed in `requirements.txt`

---

## Installation

```bat
# 1) (recommended) create an env
conda create -n translate-book python=3.11 -y
conda activate translate-book

# or venv:
python -m venv .venv
.venv\Scripts\activate

# 2) install dependencies
pip install -r requirements.txt
```

## Configuration

`config.yaml` is **git-ignored** (it holds your API keys). Copy the template:

```bat
cp config.example.yaml config.yaml
```

**Easiest way to fill in keys — the web UI form:**

```bat
python -m webui.app        # open http://127.0.0.1:5000 → tab "API Keys"
```

The **API Keys** page shows one card per provider (openai, gemini, webai,
groq, deepseek, openrouter, ollama, …). Paste a key and hit Save — a blank
field keeps the existing key, and `.bak` backups are made automatically. You
never have to edit YAML by hand.

### Provider schema

```yaml
openai:
  api_key: "sk-..."        # required
  model: gpt-5.4-mini      # optional default model for the openai engine
  models: [gpt-5.5, gpt-5.4-mini, ...]   # optional dropdown suggestions
gemini:
  api_key: "AIza..."
  model: gemini-3.5-flash-lite
  analysis_model: gemini-3.5-flash-lite
  models: [...]
webai:
  base_url: "http://localhost:6969"   # local proxy address
  model: "gemini-flash"               # forwarded verbatim to your server
# OpenAI-compatible providers — add any section with `type: openai_compatible`
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
  base_url: http://localhost:11434/v1    # local, no key needed
  model: qwen3:14b
  api_key: ""
```

- `models:` is optional — it only populates the dropdown suggestions; you can
  type any model name freely (blank = engine default).
- Local providers (webai, Ollama) work without a key; cloud providers without
  a key are shown as "⛔ chưa có key" on the API Keys page and flagged on the
  create-job form.

> **Model name note (Gemini / WebAI):** the old names `gemini-3.*` and
> `gemini-2.*` have been removed. Use `gemini-flash` / `gemini-pro` /
> `gemini-flash-lite`. The `webai` engine forwards `webai.model` verbatim to
> your local server, so set a name **that server accepts**.

---

## CLI usage (`main.py`)

```bat
# Show help
python main.py --help

# Translate chapters 1..10 with WebAI
python main.py --engine webai --input book.epub --output book.dich.epub \
               --from-chapter 1 --to-chapter 10 --from-lang EN --to-lang VI

# Use a Groq model (engine = config section name)
python main.py --engine groq --input book.epub --output book.dich.epub

# Override the model for this job only
python main.py --engine groq --model openai/gpt-oss-20b \
               --input book.epub --output book.dich.epub

# Use a local Ollama model (no API key needed)
python main.py --engine ollama --input book.epub --output book.dich.epub

# Preview a chapter's text (no translation)
python main.py --input book.epub --preview-chapter 3 --preview-chars 4000

# Test the Discord webhook
python main.py --test-discord-webhook --config config.yaml

# Resume: discard the old checkpoint and re-run from the start
python main.py --engine webai --input book.epub --output book.dich.epub --reset-checkpoint

# Disable resume (always translate from the start, ignore checkpoint)
python main.py --engine webai --input book.epub --output book.dich.epub --disable-resume
```

Outputs: `<output>.epub` + `<output>.checkpoint.json` (records finished chapters).

---

## Web UI usage (`webui/`)

```bat
# Recommended:
python -m webui.app

# Or run the file directly (works too; project root is auto-added to sys.path):
python webui/app.py
```

Open **http://127.0.0.1:5000**.

| Page | Purpose |
|------|---------|
| `/` | Dashboard: recent jobs + EPUB library |
| `/jobs/new` | Create a job (engine + optional model, input/output, chapter range, languages, …) |
| `/jobs/<id>` | Realtime log (SSE) + progress bar + Stop button |
| `/books` | List EPUBs, chapter counts, checkpoint status, chapter preview |
| `/config/keys` | **API Keys**: form to add/update each provider's key + base URL + model |
| `/config` | Advanced: edit raw `config.yaml` (keys masked, `.bak` backup), test Discord |
| `/illustrations` | Gallery of generated illustrations |

**Stopping a job:** the Stop button halts safely at a **chapter boundary** (it will
not abort a chunk that is mid-API-call).

---

## Project structure

```
main.py                  CLI entry (calls translator.job.run_translation)
translator/
  translator.py          Translator core: split -> translate -> reassemble, retry, consistency
  job.py                 run_translation(): shared orchestration for CLI & web
                         (checkpoint/resume, illustration, discord, stats, progress_cb)
  epub_utils.py          read/write EPUB, iterate chapters, inject manifest
  html_utils.py          split/assemble HTML, normalize <br>
  illustration.py        generate illustrations
  discord_notifier.py    send Discord webhook
  engines/                TranslationEngine: openai / gemini / webai / compatible (+ base)
webui/
  app.py                 Flask app (routes + SSE)
  jobs.py                JobRegistry + Job (state, log, progress; persisted in webui/jobs/)
  core_runner.py         runs run_translation in a thread, forwards log/events -> SSE
  config_store.py        safe read/write of config.yaml (mask keys + backup)
  books.py               list EPUBs, preview chapters
  templates/  static/    UI (incl. keys.html — API Keys form)
tests/                   offline unittest (no AI calls)
requirements.txt         dependencies
config.example.yaml      config template
```

---

## Running the tests (offline, free)

```bat
python -m unittest tests.test_html_utils tests.test_translator \
                     tests.test_epub_utils tests.test_job_core tests.test_engines \
                     tests.test_webui -v
```

- `test_job_core`: runs `run_translation` with a fake engine + a small EPUB and
  asserts the output, checkpoint, and stats are produced.
- `test_engines`: multi-provider factory (openai-compatible providers, model
  suggestions, model override plumbing into `build_engine` + checkpoint).
- `test_webui`: Flask test client + a real job run (engine/Discord stubbed) and
  asserts an EPUB is produced; also covers the API Keys page (save/keep/leak-safe).

---

## Security notes

- `config.yaml`, `*.checkpoint.json`, `*.p12`, and `webui/jobs/` are git-ignored.
- The web UI has **no authentication**. Run it locally only. If you need remote
  access, put it behind an authenticated reverse proxy (VPN / Tailscale /
  Cloudflare Access) — never expose it bare.
- API keys live in `config.yaml` on your local machine; do not commit them.

---

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `ModuleNotFoundError: No module named 'webui'` | ran `python webui/app.py` from the wrong directory. Use `python -m webui.app` from the project root. |
| Translation runs but uses the "wrong model" | `webai.model` is an old name (`gemini-3.*`). Change it to `gemini-flash`. |
| Job hangs / retries forever | model returned HTML with lost tags; code caps retries (max 30) + falls back to splitting. |
| Web UI shows no log | jobs run in a thread; reloading `/jobs/<id>` reloads from the buffer. |

---

Tiếng Việt: see [README.vi.md](README.vi.md).
