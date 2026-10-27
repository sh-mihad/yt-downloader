# yt-downloader-poc

A disposable, standalone test of one question: **does YouTube extraction/download work
from this host?** Converter's own `/video-downloader` page blocks YouTube client-side
because Render's datacenter IPs trip YouTube's "confirm you're not a bot" check. This
app is the same stack and the same request flow (yt-dlp + ffmpeg + the `bgutil` PO-token
server), stripped of everything else (no auth, no database, no credits/plans, no admin
config), so the only variable left is: can this particular host reach YouTube at all.

Not something to point real users at: no shared rate-limit state across instances, no
persistence beyond an OS-temp-dir TTL sweep, no auth.

## Local run

One command, from this folder (first time only: `cd apps/api && uv sync && cd ../..`
to install the Python deps, and `pnpm install` for the frontend):

```bash
pnpm dev
```

Runs the API (`:8010`, auto-reload) and the Vite dev server (`:5173`) together via
`concurrently` (`package.json`'s `dev` script). Open `http://localhost:5173`. Run just
one side with `pnpm dev:api` or `pnpm dev:web`.

The dev server proxies `/api` to `http://localhost:8010` (`apps/web/vite.config.ts`).
**Not port 8000** — that's Converter's own `apps/api` dev port; if you have the real
Converter app's `pnpm dev` running at the same time, both can run side by side without
colliding.

Optional: copy `apps/api/.env.example` to `apps/api/.env` and fill in `YOUTUBE_COOKIES`
(base64 of a Netscape `cookies.txt`, exported from a throwaway Google account signed
into YouTube in a private window) and/or `YOUTUBE_PROXY` if you want to test the bypass
paths locally too — though the whole point is that locally it usually isn't needed.

## Deploy to Render

1. Push this folder to its own new GitHub repo (it's a separate git root from
   `C:\converter` on purpose).
2. On Render: **New** → **Blueprint**, point it at the repo (it reads `render.yaml`), or
   **New** → **Web Service** → Docker, Dockerfile path `apps/api/Dockerfile`, root
   directory `.` (the build needs both `apps/api` and `apps/web`, so don't scope the
   root directory down to `apps/api`).
3. In the Render dashboard, set `YOUTUBE_COOKIES` and (if needed) `YOUTUBE_PROXY` as
   environment variables — never commit them (`render.yaml` marks them `sync: false`
   for exactly that reason).
4. Deploy, then open the service's `.onrender.com` URL.

## What "it works" looks like

```bash
curl -s -X POST https://<your-service>.onrender.com/api/media/info \
  -H 'content-type: application/json' \
  -d '{"url":"https://www.youtube.com/watch?v=dQw4w9WgXcQ"}'
```

- **Success**: a 200 with a `formats` array.
- **Still blocked**: a 400 whose message is `format_media_error`'s bot-check sentence
  ("YouTube isn't letting us fetch this video right now..."). Check the Render logs for
  the `bgutil` server's startup line and any `"trying fallback..."` log lines from
  `app/downloader.py` to see which bypass path was attempted and whether any of them
  got through.

If it's still blocked, try in this order (same order Converter's own
`docs/architecture.md` recommends for the real feature): `YOUTUBE_COOKIES` first, then
`YOUTUBE_PROXY` (a residential/home proxy — YouTube's check is IP-reputation based, so a
datacenter proxy won't help).

## Project layout

```
apps/api/app/
  main.py         FastAPI app; mounts the router and (if built) the frontend
  router.py       the 5 routes: /info, /job, /progress/{id} (SSE), /stream/{id}, /file/{id}
  downloader.py   yt-dlp + ffmpeg logic: info extraction, quality ladder, the in-memory
                  job dict, watermark/MP3 encode, the streamed-watermark path
  settings.py     env-driven config (youtube cookies/proxy, ffmpeg path, job limits)
  ssrf.py         blocks a pasted link from pointing at a private/internal address
  ratelimit.py    in-memory per-IP rate limiting
  hwaccel.py      GPU video-encoder probing for the watermark overlay
apps/web/src/
  App.tsx         the one page: URL -> info -> quality picker -> job -> SSE progress -> download
```
