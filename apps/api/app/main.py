"""Entry point: uvicorn app.main:app.

Mounts /api/media/* and, when the frontend has been built (apps/web/dist, copied into
the image as web-dist by the Dockerfile), serves it as a static SPA at "/" — same
origin as the API, so there's no CORS to configure and no second Render service.
"""

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.router import router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="yt-downloader-poc")
app.include_router(router, prefix="/api")


@app.get("/api/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


DIST = Path(__file__).resolve().parents[1] / "web-dist"
if DIST.exists():
    app.mount("/", StaticFiles(directory=DIST, html=True), name="web")
