"""Routes for the standalone YouTube downloader test app.

Ported from Converter's apps/api/app/modules/media_downloader/controller.py, with the
Mongo-backed auth/credits/admin-config layer (`OptionalUser`, `catalog.store.snapshot()`)
stripped out — this POC has no users, no plans, and no admin console. The two things
that layer provided (the watermark on/off switch and a disabled-formats list) become
plain module constants below. Every route is still rate-limited per IP (`app.ratelimit`),
and the streamed-watermark path still caps concurrent ffmpeg encodes
(`downloader.stream_in_slot`), same as the real app.
"""

import asyncio
import json
import re
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel

from app import downloader as service
from app import ratelimit

router = APIRouter(prefix="/media", tags=["media"])

# Stand-ins for Converter's admin-editable MediaConfig (site.media.watermark /
# site.media.disabled_formats). No admin console here, so they're constants.
WATERMARK_ENABLED = True
DISABLED_FORMATS: set[str] = set()


class MediaInfoRequest(BaseModel):
    url: str


class StartJobRequest(BaseModel):
    url: str
    format: str = "mp4_720"


async def _limit(request: Request, name: str, per_minute: int) -> None:
    await ratelimit.hit(f"{name}:ip:{ratelimit.client_ip(request)}", per_minute, 60)


@router.post("/info")
async def media_info(body: MediaInfoRequest, request: Request) -> dict[str, Any]:
    """Title, thumbnail, duration and every quality the video has, minus the ones
    `DISABLED_FORMATS` turns off."""
    await _limit(request, "media-info", 30)
    info = await asyncio.to_thread(service.extract_media_info, body.url)
    return {**info, "formats": [f for f in info["formats"] if service.format_key(f["id"]) not in DISABLED_FORMATS]}


@router.post("/job")
async def create_job(body: StartJobRequest, request: Request) -> dict[str, str]:
    """Start an asynchronous download job with real-time telemetry."""
    await _limit(request, "media-job", 10)
    choice = service.parse_format(body.format)
    if service.format_key(body.format) in DISABLED_FORMATS:
        raise HTTPException(403, "That quality isn't available right now. Pick another one.")
    watermark = WATERMARK_ENABLED and choice.kind == "video"
    return {"job_id": service.create_download_job(body.url, body.format, watermark)}


@router.get("/progress/{job_id}")
async def job_progress(job_id: str, request: Request):
    """Server-Sent Events stream for real-time progress, percentage, ETA and speed."""
    await ratelimit.hit(f"media-progress:ip:{ratelimit.client_ip(request)}", 60, 60)

    async def event_generator():
        while True:
            job = service.get_job(job_id)
            if not job:
                yield f"data: {json.dumps({'status': 'failed', 'error': 'Job not found or expired'})}\n\n"
                break

            payload = {
                "status": job.status,
                "percent": job.percent,
                "eta": job.eta,
                "speed_str": job.speed_str,
                "downloaded_str": job.downloaded_str,
                "total_str": job.total_str,
                "stage": job.stage,
                "filename": job.filename,
                "error": job.error,
                "download_url": (
                    None
                    if job.status != "completed"
                    else f"/api/media/stream/{job_id}"
                    if job.stream
                    else f"/api/media/file/{job_id}"
                ),
            }
            yield f"data: {json.dumps(payload)}\n\n"

            if job.status in ("completed", "failed"):
                break
            await asyncio.sleep(0.35)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


_DL_TOKEN = re.compile(r"^[0-9a-f]{8,32}$")


def _mark_started(response: Response, dl: str | None) -> Response:
    if dl and _DL_TOKEN.match(dl):
        response.set_cookie(f"dl_{dl}", "1", max_age=120, path="/", samesite="lax")
    return response


def _resume(first: bytes, rest: Iterator[bytes]) -> Iterator[bytes]:
    # `yield from` passes a closed connection on to `rest`, which then stops ffmpeg.
    yield first
    yield from rest


@router.get("/stream/{job_id}")
async def stream_job_file(job_id: str, dl: str | None = None):
    """A watermarked video, sent while ffmpeg makes it (fragmented MP4, no length up
    front): the browser starts saving as soon as it would for a clean file."""
    job = service.get_job(job_id)
    if not job or not job.stream:
        raise HTTPException(404, "This download is no longer available. Start it again.")
    chunks = service.stream_in_slot(job_id, job.stream)
    try:
        first = await asyncio.to_thread(next, chunks, b"")
    except service.StreamsBusy as busy:
        raise HTTPException(429, str(busy), headers={"Retry-After": "30"}) from None
    if not first:
        raise HTTPException(502, "We couldn't make that file. Start it again.")
    name = job.stream.filename
    return _mark_started(
        StreamingResponse(
            _resume(first, chunks),
            media_type="video/mp4",
            headers={
                "Content-Disposition": f"attachment; filename=\"download.mp4\"; filename*=UTF-8''{quote(name)}",
                "Cache-Control": "no-store",
            },
        ),
        dl,
    )


@router.get("/file/{job_id}")
async def get_job_file(job_id: str, dl: str | None = None):
    """The finished file, as often as the browser asks until the job expires."""
    job = service.get_job(job_id)
    if not job or not job.file_path or not job.file_path.exists():
        raise HTTPException(404, "This download is no longer available. Start it again.")

    return _mark_started(
        FileResponse(
            path=job.file_path,
            media_type=job.media_type or "application/octet-stream",
            filename=job.filename or "download",
        ),
        dl,
    )
