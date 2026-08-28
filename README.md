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
- **Multiple engines**: `openai`, `gemini`, `webai` (local OpenAI-compatible proxy).
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

`config.yaml` is **git-ignored** (it holds your API keys). Create it from the template:

```bat
cp config.example.yaml config.yaml
```

Then fill in `openai.api_key` / `gemini.api_key` / `webai.base_url` /
`discord.webhook_url`. See `config.example.yaml` for every valid key.

> **Model name note (Gemini / WebAI):** the old names `gemini-3.*` and `gemini-2.*`
> have been removed. Use `gemini-flash` / `gemini-pro` / `gemini-flash-lite`. The
> `webai` engine forwards `webai.model` verbatim to your local server, so set a name
> **that server accepts**.

---

## CLI usage (`main.py`)

```bat
# Show help
python main.py --help

# Translate chapters 1..10 with WebAI
python main.py --engine webai --input book.epub --output book.dich.epub \
               --from-chapter 1 --to-chapter 10 --from-lang EN --to-lang VI

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
| `/jobs/new` | Create a job (engine, input/output, chapter range, languages, …) |
| `/jobs/<id>` | Realtime log (SSE) + progress bar + Stop button |
| `/books` | List EPUBs, chapter counts, checkpoint status, chapter preview |
| `/config` | Edit `config.yaml` (API keys masked, `.bak` backup), test Discord |
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
  engines/                TranslationEngine: openai / gemini / webai (+ base)
webui/
  app.py                 Flask app (routes + SSE)
  jobs.py                JobRegistry + Job (state, log, progress; persisted in webui/jobs/)
  core_runner.py         runs run_translation in a thread, forwards log/events -> SSE
  config_store.py        safe read/write of config.yaml (mask keys + backup)
  books.py               list EPUBs, preview chapters
  templates/  static/    UI
tests/                   offline unittest (no AI calls)
requirements.txt         dependencies
config.example.yaml      config template
```

---

## Running the tests (offline, free)

```bat
python -m unittest tests.test_html_utils tests.test_translator \
                     tests.test_epub_utils tests.test_job_core tests.test_webui -v
```

- `test_job_core`: runs `run_translation` with a fake engine + a small EPUB and
  asserts the output, checkpoint, and stats are produced.
- `test_webui`: Flask test client + a real job run (engine/Discord stubbed) and
  asserts an EPUB is produced.

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
