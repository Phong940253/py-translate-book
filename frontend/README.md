# WebUI v2 (React)

React + Vite + TypeScript + Tailwind UI served by the existing Flask app on the
official **port 5000**: the classic v1 UI stays at `/`, the v2 SPA is at `/v2`.

## Build and run

```powershell
cd frontend
npm install
npm run build

cd ..
python -m webui.app_v2
```

Open <http://127.0.0.1:5000/v2/>.

The Vite build output goes to `webui/static/v2/` and is git-ignored; run
`npm run build` after frontend changes so Flask serves the latest bundle. If the
bundle is missing, `/v2` returns a clear 503 with the build command.

## Development

```powershell
cd frontend
npm run dev
```

The dev server listens on <http://127.0.0.1:5173/v2/> and proxies `/api`,
`/set-lang`, `/books`, `/read`, and `/static` to port 5000.

## Scope

- Dashboard, library, music/BGM, settings, and the reader at `/v2/read`.
- BGM import uses `yt-dlp`; `ffmpeg` must be on `PATH` for MP3 conversion.
- Reader TTS/read-aloud runs in React: `/api/tts/stream` audio is preloaded a
  few paragraphs ahead (`useReaderTts`) so playback does not stall between
  paragraphs.
