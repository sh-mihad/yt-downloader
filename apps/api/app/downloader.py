"""YouTube video and audio downloader using yt-dlp.

Ported from Converter's apps/api/app/modules/media_downloader/service.py, trimmed to
YouTube only (the real module also handles TikTok/Instagram/Facebook/X) and decoupled
from MongoDB/auth/credits for this standalone Render test. Everything else — the
quality ladder, the in-memory job dict, the watermark/MP3 ffmpeg encode, the streamed-
watermark path, the cookie/proxy loading, the Android-client bot-check fallback chain —
is unchanged, since reproducing that behavior faithfully is the whole point.
"""

import contextlib
import json
import logging
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, NamedTuple

import yt_dlp
from fastapi import HTTPException

from app import hwaccel
from app.settings import settings
from app.ssrf import validate_public_url

log = logging.getLogger("downloader")

YOUTUBE_PATTERN = re.compile(r"(youtube\.com|youtu\.be)", re.IGNORECASE)


def detect_platform(url: str) -> str:
    if YOUTUBE_PATTERN.search(url):
        return "youtube"
    raise HTTPException(400, "Paste a YouTube link (youtube.com or youtu.be).")


def clean_title(title: str) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|]', "", title).strip()
    return cleaned[:90] if cleaned else "media"


def get_ffmpeg_path() -> str | None:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    return shutil.which("ffmpeg")


def get_cookie_file() -> Path | None:
    """Return path to YouTube cookies file if configured via settings or present on disk."""
    import os

    env_cookies = getattr(settings, "youtube_cookies", None) or os.environ.get("YOUTUBE_COOKIES")
    if env_cookies:
        content = env_cookies.strip()
        if (content.startswith('"') and content.endswith('"')) or (content.startswith("'") and content.endswith("'")):
            content = content[1:-1].strip()

        if content:
            import base64

            try:
                decoded = base64.b64decode(content, validate=True).decode("utf-8")
                if "# Netscape" in decoded or "\t" in decoded:
                    content = decoded
            except Exception:
                pass

            if "\\n" in content:
                content = content.replace("\\n", "\n")

            if not content.startswith("# Netscape"):
                content = "# Netscape HTTP Cookie File\n" + content

            cookie_path = settings.storage_dir / "youtube_cookies.txt"
            cookie_path.parent.mkdir(parents=True, exist_ok=True)
            cookie_path.write_text(content, encoding="utf-8")
            # Netscape rows are 7 tab-separated fields; a dashboard that turns tabs into
            # spaces leaves yt-dlp with none.
            rows = [ln.split("\t") for ln in content.splitlines() if ln and not ln.startswith("# ")]
            names = {r[5] for r in rows if len(r) == 7}
            if "__Secure-3PSID" not in names and "SAPISID" not in names:
                log.warning(
                    "YOUTUBE_COOKIES has %d valid rows and no signed-in YouTube cookie: paste it base64-encoded",
                    len(names),
                )
            log.info("Loaded YouTube cookies from environment into %s (%d bytes)", cookie_path, len(content))
            return cookie_path

    cookie_candidates = [
        settings.storage_dir / "cookies.txt",
        settings.storage_dir / "youtube_cookies.txt",
        Path("cookies.txt"),
    ]
    for p in cookie_candidates:
        if p.exists() and p.is_file() and p.stat().st_size > 0:
            log.info("Found YouTube cookies on disk at %s (%d bytes)", p, p.stat().st_size)
            return p
    return None


MAX_MEDIA_BYTES = 4 * 1024**3
TOO_BIG = f"This video is over the {MAX_MEDIA_BYTES // 1024**3} GB download limit. Pick a lower quality."
LIVE = "Live streams can't be downloaded. Try again once the stream has ended."


def check_stream_url(url: Any) -> None:
    """A stream URL yt-dlp hands back must be http(s) on a public address, like the
    link itself: a page (or a redirect) can name one on the server's own network."""
    if not isinstance(url, str):
        raise HTTPException(400, "We couldn't get that video. Check the link and try again.")
    validate_public_url(url, add_scheme=False)


def _before_download(info: dict[str, Any], incomplete: Any = False) -> None:
    """yt-dlp's `match_filter`: refuses a live stream, a file already known to be too
    big, or a stream URL that isn't public, before a byte is downloaded."""
    if incomplete:
        return None
    if info.get("is_live"):
        raise HTTPException(400, LIVE)
    streams = info.get("requested_formats") or [info]
    if sum(_size(f) for f in streams) > MAX_MEDIA_BYTES:
        raise HTTPException(413, TOO_BIG)
    for f in streams:
        check_stream_url(f.get("url"))
    return None


def _byte_cap(limit: int) -> Callable[[dict[str, Any]], None]:
    """A progress hook that stops the download once its streams pass `limit` bytes
    together — yt-dlp's own `max_filesize` only reads Content-Length, so a stream
    without one (HLS/DASH fragments) had no cap at all."""
    seen: dict[str, int] = {}

    def hook(d: dict[str, Any]) -> None:
        if d.get("status") != "downloading":
            return
        seen[str(d.get("filename") or "")] = int(d.get("downloaded_bytes") or 0)
        if sum(seen.values()) > limit or int(d.get("total_bytes") or 0) > limit:
            raise HTTPException(413, TOO_BIG)

    return hook


def format_media_error(exc: Exception) -> str:
    """Formats a yt-dlp exception into a plain, friendly message."""
    msg = str(exc)
    low = msg.lower()
    if "not a bot" in low or "bot verification" in low or "page needs to be reloaded" in low:
        return (
            "YouTube isn't letting us fetch this video right now. This is the bot-check "
            "this test app exists to probe — try YOUTUBE_COOKIES or YOUTUBE_PROXY."
        )
    if "Requested format is not available" in msg:
        return "That quality isn't available for this video. Pick another one."
    if "Private video" in msg:
        return "This video is private, so we can't download it."
    if "Video unavailable" in msg:
        return "This video isn't available any more."
    if "is not a valid URL" in msg or "Unsupported URL" in msg:
        return "We can't use that link. Paste a YouTube video or music link."
    return "We couldn't get that video. Check the link and try again."


def js_runtimes() -> dict[str, dict[str, str]]:
    """YouTube hides its streams behind signature/"n" challenges yt-dlp solves in
    JavaScript. Without a runtime a signed-in request gets no formats at all. The pip
    `deno` package ships one; Node is the fallback where that's missing."""
    try:
        import deno

        return {"deno": {"path": deno.find_deno_bin()}}
    except Exception:
        return {"deno": {}, "node": {}}


# YouTube clients to retry with when the default ones hit the bot check; each is
# challenged differently, and the PO token plugin (bgutil, started by start.sh) supplies
# the tokens the web-based ones ask cloud IPs for.
FALLBACK_CLIENTS = ["tv", "web_safari", "mweb", "android"]


def get_ydl_base_opts(include_cookies: bool = True) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 3,
        "ignoreerrors": False,
        "js_runtimes": js_runtimes(),
    }
    ffmpeg_path = get_ffmpeg_path()
    if ffmpeg_path:
        opts["ffmpeg_location"] = ffmpeg_path

    if include_cookies:
        cookie_file = get_cookie_file()
        if cookie_file:
            opts["cookiefile"] = str(cookie_file)

    import os

    proxy = getattr(settings, "youtube_proxy", "") or os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY")
    if proxy:
        opts["proxy"] = proxy

    return opts


def extract_media_info(url: str) -> dict[str, Any]:
    safe_url = validate_public_url(url)
    platform = detect_platform(safe_url)

    opts = get_ydl_base_opts()
    opts["skip_download"] = True

    info: dict[str, Any] | None = None
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(safe_url, download=False)
    except Exception as exc:
        # Extraction with cookies can fail (expired/invalid cookies); retry without them.
        if "cookiefile" in opts:
            log.info("Extraction with cookies failed (%s); trying fallback without cookies...", exc)
            fallback_opts = get_ydl_base_opts(include_cookies=False)
            fallback_opts["skip_download"] = True
            try:
                with yt_dlp.YoutubeDL(fallback_opts) as ydl:
                    info = ydl.extract_info(safe_url, download=False)
            except Exception:
                pass

        # Still failed (a cloud datacenter IP blocked with bot verification): the mobile
        # client fallback bypasses that challenge differently.
        if not info:
            log.info("Extraction failed (%s); trying fallback with mobile android client...", exc)
            android_opts = get_ydl_base_opts(include_cookies=False)
            android_opts["skip_download"] = True
            android_opts["extractor_args"] = {"youtube": {"player_client": FALLBACK_CLIENTS}}
            try:
                with yt_dlp.YoutubeDL(android_opts) as ydl:
                    info = ydl.extract_info(safe_url, download=False)
            except Exception as e2:
                log.warning("Android client fallback extraction also failed: %s", e2)

        if not info:
            log.warning("yt-dlp metadata extraction failed for %s: %s", safe_url, exc)
            raise HTTPException(400, format_media_error(exc)) from None

    if not info:
        raise HTTPException(404, "We couldn't find a video or song at that link. Check it and try again.")

    title = info.get("title") or "Video"
    thumbnail = info.get("thumbnail") or ""
    duration = info.get("duration") or 0
    uploader = info.get("uploader") or info.get("channel") or "YouTube"

    # No direct CDN link is sent to the client: it would skip the quality cap and the watermark.
    return {
        "url": safe_url,
        "platform": platform,
        "title": title,
        "thumbnail": thumbnail,
        "duration": duration,
        "uploader": uploader,
        "formats": quality_options(info),
    }


# ---- qualities: what the video really has ----

PREMIUM_ABOVE = 1080  # kept for parity with the real app's Pro-tier label; unenforced here
LADDER = (
    (4320, "8K"),
    (2160, "4K Ultra HD"),
    (1440, "2K Quad HD"),
    (1080, "Full HD"),
    (720, "HD"),
    (480, "Standard"),
    (360, "Basic"),
    (240, "Data saver"),
    (144, "Lowest"),
)
MP3_KBPS = {320: "Highest quality", 256: "High quality", 192: "Standard", 128: "Smaller file", 96: "Smallest file"}
# a stream a few pixels over a rung (1088 lines for "1080p") still counts as that rung
TOLERANCE = 16
_FORMAT = re.compile(r"^(?:mp4(v?)_(\d{3,4})|mp3_(\d{2,3})|m4a)$")
FORMAT_KEYS = (*(f"mp4_{p}" for p, _ in LADDER), *(f"mp3_{k}" for k in MP3_KBPS), "m4a")


def format_key(format_id: str) -> str:
    return format_id.replace("mp4v_", "mp4_")


class Choice(NamedTuple):
    kind: str  # "video" | "audio"
    value: int  # video: the short side in px (720 = 720p); mp3: kbps; m4a: 0
    portrait: bool = False

    @property
    def premium(self) -> bool:
        return self.kind == "video" and self.value > PREMIUM_ABOVE


def parse_format(format_id: str) -> Choice:
    """A quality id from `quality_options` back into what to fetch. Anything else is refused."""
    m = _FORMAT.match(format_id)
    if not m:
        raise HTTPException(400, "Pick one of the qualities shown for this video.")
    portrait, height, kbps = m.groups()
    if height:
        if int(height) not in dict(LADDER):
            raise HTTPException(400, "Pick one of the qualities shown for this video.")
        return Choice("video", int(height), bool(portrait))
    if kbps:
        if int(kbps) not in MP3_KBPS:
            raise HTTPException(400, "Pick one of the qualities shown for this video.")
        return Choice("audio", int(kbps))
    return Choice("audio", 0)


def _size(f: dict) -> int:
    return int(f.get("filesize") or f.get("filesize_approx") or 0)


def quality_options(info: dict) -> list[dict]:
    """Every resolution the video really has (8K down to 144p, by its short side so
    portrait clips read right), each with its size when the site reports one, then MP3
    at five bitrates and the original audio."""
    fmts = info.get("formats") or []
    videos = [f for f in fmts if f.get("vcodec") not in (None, "none") and f.get("height") and f.get("width")]
    audio_only = (f for f in fmts if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none"))
    audio = max((_size(f) for f in audio_only), default=0)
    biggest = max(videos, key=lambda f: f["height"] * f["width"], default=None)
    portrait = bool(biggest and biggest["height"] > biggest["width"])
    rungs: dict[int, int] = {}
    for f in videos:
        short = min(f["height"], f["width"])
        rung = min((p for p, _ in LADDER if p >= short - TOLERANCE), default=LADDER[0][0])
        rungs[rung] = max(rungs.get(rung, 0), _size(f))
    if not rungs:
        rungs = dict.fromkeys((1080, 720, 480, 360), 0)

    out: list[dict] = [
        {
            "id": f"mp4{'v' if portrait else ''}_{p}",
            "kind": "video",
            "ext": "mp4",
            "quality": f"{p}p",
            "label": name,
            "premium": p > PREMIUM_ABOVE,
            "size": rungs[p] + audio if rungs[p] else None,
        }
        for p, name in LADDER
        if p in rungs
    ]
    duration = info.get("duration") or 0
    out += [
        {
            "id": f"mp3_{k}",
            "kind": "audio",
            "ext": "mp3",
            "quality": f"{k} kbps",
            "label": label,
            "premium": False,
            "size": int(duration * k * 1000 / 8) or None,
        }
        for k, label in MP3_KBPS.items()
    ]
    out.append(
        {
            "id": "m4a",
            "kind": "audio",
            "ext": "m4a",
            "quality": "Original",
            "label": "As uploaded, no re-encoding",
            "premium": False,
            "size": audio or None,
        }
    )
    return out


# ---- watermark overlay on the downloaded video ----

WATERMARK_TEXT = "YT-POC"


class Probe(NamedTuple):
    height: int
    seconds: float  # 0 when unknown
    kbps: int


def _probe(ffmpeg: str, src: Path) -> Probe:
    """Height, length and video bitrate from ffmpeg's own stream summary (no ffprobe
    dependency)."""
    text = subprocess.run(
        [ffmpeg, "-nostdin", "-hide_banner", "-i", str(src)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    ).stderr
    size = re.search(r"Video:.*?, (\d{2,5})x(\d{2,5})", text)
    length = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", text)
    rate = re.search(r"Video:.*?(\d+) kb/s", text) or re.search(r"bitrate: (\d+) kb/s", text)
    return Probe(
        int(size.group(2)) if size else 720,
        int(length.group(1)) * 3600 + int(length.group(2)) * 60 + float(length.group(3)) if length else 0.0,
        int(rate.group(1)) if rate else 0,
    )


def bitrate_args(source_kbps: int, height: int) -> list[str]:
    """Rate control for the watermark encode: the source's own bitrate plus a little for
    the re-encode, so the file stays about the size it was."""
    kbps = round(source_kbps * 1.15) if source_kbps else max(800, round(height * 2.8))
    return ["-b:v", f"{kbps}k", "-maxrate", f"{kbps * 3 // 2}k", "-bufsize", f"{kbps * 2}k"]


def watermark_png(height: int, dest: Path) -> None:
    """The mark: plain text in white on a dark translucent pill, about 3.5% of the video's height."""
    from PIL import Image, ImageDraw, ImageFont

    size = max(14, round(height * 0.035))
    font = ImageFont.load_default(size=size)
    left, top, right, bottom = font.getbbox(WATERMARK_TEXT)
    pad_x, pad_y = round(size * 0.7), round(size * 0.45)
    w, h = right - left + 2 * pad_x, bottom - top + 2 * pad_y
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((0, 0, w - 1, h - 1), radius=h // 2, fill=(21, 18, 11, 150))
    draw.text((pad_x - left, pad_y - top), WATERMARK_TEXT, font=font, fill=(255, 255, 255, 235))
    img.save(dest)


def _watermark_cmd(ffmpeg: str, src: Path, mark: Path, out: Path, hw: str | None, rate: list[str]) -> list[str]:
    """Overlay the mark and re-encode only the video: on the GPU when one works, else
    x264 `superfast`, at the source's bitrate. Audio is copied untouched."""
    video = [hw, "-preset", "veryfast"] if hw else ["libx264", "-preset", "superfast"]
    return [
        ffmpeg, "-nostdin", "-y", "-loglevel", "error", "-progress", "pipe:1", "-nostats", *hwaccel.device_args(hw),
        "-i", str(src), "-i", str(mark),
        "-filter_complex", f"[0:v][1:v]overlay=W-w-W*0.025:H-h-H*0.035,{hwaccel.pixel_filter(hw)}[v]",
        "-map", "[v]", "-map", "0:a?", "-c:a", "copy", "-c:v", *video, *rate, "-movflags", "+faststart", str(out),
    ]  # fmt: skip


def add_watermark(src: Path, on_progress: Callable[[float], None] | None = None) -> Path:
    """Burns the mark into the bottom-right corner. Returns the new file next to `src`,
    which is removed. `-nostdin` matters: without it ffmpeg inherits this process's
    console stdin and waits on it forever."""
    ffmpeg = settings.ffmpeg
    mark, out = src.with_name("watermark.png"), src.with_name("watermarked.mp4")
    info = _probe(ffmpeg, src)
    watermark_png(info.height, mark)
    rate = bitrate_args(info.kbps, info.height)
    for hw in dict.fromkeys((hwaccel.encoder_for("libx264"), None)):
        code, err = _run_with_progress(_watermark_cmd(ffmpeg, src, mark, out, hw, rate), info.seconds, on_progress)
        if code == 0:
            break
        log.warning("watermark with %s failed: %s", hw or "libx264", err)
    else:
        raise RuntimeError("couldn't add the watermark")
    src.unlink(missing_ok=True)
    mark.unlink(missing_ok=True)
    return out


def _run_with_progress(cmd: list[str], seconds: float, on_progress: Callable[[float], None] | None) -> tuple[int, str]:
    """Runs an ffmpeg command that has `-progress pipe:1`, reporting its position
    (0..1 of `seconds`). Returns the exit code and the end of stderr."""
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=err, text=True)
        assert proc.stdout is not None
        for line in proc.stdout:
            if on_progress and seconds and line.startswith("out_time_us=") and line[12:].strip().isdigit():
                on_progress(min(1.0, int(line[12:]) / 1e6 / seconds))
        code = proc.wait(timeout=4 * 3600)
        err.seek(0)
        return code, err.read().decode(errors="replace")[-300:]


# lame's fast mode, twice its default speed; at a fixed bitrate the difference is inaudible
MP3_SPEED = ["-compression_level", "7"]


def encode_mp3(src: Path, kbps: int, on_progress: Callable[[float], None] | None = None) -> Path:
    """The site's best audio -> MP3 at `kbps`, next to `src` (which is removed)."""
    ffmpeg = settings.ffmpeg
    out = src.with_name("audio.mp3")
    cmd = [
        ffmpeg, "-nostdin", "-y", "-loglevel", "error", "-progress", "pipe:1", "-nostats",
        "-i", str(src), "-vn", "-c:a", "libmp3lame", "-b:a", f"{kbps}k", *MP3_SPEED, str(out),
    ]  # fmt: skip
    code, err = _run_with_progress(cmd, _probe(ffmpeg, src).seconds, on_progress)
    if code:
        raise RuntimeError(f"couldn't make the MP3: {err}")
    src.unlink(missing_ok=True)
    return out


DOWNLOAD_EXECUTOR = ThreadPoolExecutor(max_workers=max(1, settings.media_jobs), thread_name_prefix="media-worker")
JOBS_LOCK = threading.Lock()


class DownloadJob:
    def __init__(self, job_id: str, url: str, format_id: str):
        self.job_id = job_id
        self.url = url
        self.format_id = format_id
        self.status = "queued"  # queued, downloading, converting, completed, failed
        self.percent = 0.0
        self.eta: int | None = None
        self.speed_str = "0 B/s"
        self.downloaded_str = "0 MB"
        self.total_str = "Estimating..."
        self.stage = "Queued..."
        self.filename: str | None = None
        self.file_path: Path | None = None
        self.media_type: str | None = None
        self.error: str | None = None
        # set for a watermarked video served while it's encoded (`stream_watermarked`);
        # file_path stays None then
        self.stream: StreamPlan | None = None
        self.created_at = time.time()
        self.updated_at = time.time()


JOBS: dict[str, DownloadJob] = {}

# Off by default: see the comment at its one use in create_download_job's worker().
STREAM_WATERMARK = False

# A finished job stays downloadable this long, as many times as the browser asks.
JOB_TTL = 30 * 60
JOBS_DIR = Path(tempfile.gettempdir()) / "media-jobs"
_JOB_ID = re.compile(r"^[0-9a-f]{12}$")


def job_dir(job_id: str) -> Path:
    return JOBS_DIR / job_id


def _save(job: DownloadJob) -> None:
    """The finished job's record next to its file. Never fails the job: without it only
    a restart loses it."""
    record = {
        "filename": job.filename,
        "media_type": job.media_type,
        "file": job.file_path.name if job.file_path else None,
        "stream": job.stream._asdict() if job.stream else None,
        "created_at": job.created_at,
    }
    with contextlib.suppress(OSError):
        job_dir(job.job_id).mkdir(parents=True, exist_ok=True)
        (job_dir(job.job_id) / "job.json").write_text(json.dumps(record), encoding="utf-8")


def _load(job_id: str) -> DownloadJob | None:
    """A finished job this process doesn't hold (it restarted), from its folder, while
    it's still fresh."""
    if not _JOB_ID.match(job_id):  # the id becomes a path: only our own 12 hex characters
        return None
    try:
        record = json.loads((job_dir(job_id) / "job.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if time.time() - record["created_at"] > JOB_TTL:
        return None
    job = DownloadJob(job_id, "", "")
    job.status, job.percent, job.eta, job.stage = "completed", 100.0, 0, "Ready"
    job.filename, job.media_type, job.created_at = record["filename"], record["media_type"], record["created_at"]
    job.file_path = job_dir(job_id) / record["file"] if record["file"] else None
    if stream := record["stream"]:
        job.stream = StreamPlan(**{**stream, "inputs": [(u, h) for u, h in stream["inputs"]]})
    return job


def get_job(job_id: str) -> DownloadJob | None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    return job or _load(job_id)


def sweep_jobs() -> None:
    """Forget jobs past JOB_TTL and delete their folders (and any a restart left behind)."""
    now = time.time()
    with JOBS_LOCK:
        for jid in [jid for jid, j in JOBS.items() if now - j.created_at > JOB_TTL]:
            JOBS.pop(jid, None)
    if JOBS_DIR.exists():
        for d in JOBS_DIR.iterdir():
            with contextlib.suppress(OSError):
                if now - d.stat().st_mtime > JOB_TTL:
                    shutil.rmtree(d, ignore_errors=True)


def create_download_job(url: str, format_id: str, watermark: bool = False) -> str:
    """Create a background download/encode job with real-time progress."""
    sweep_jobs()

    job_id = uuid.uuid4().hex[:12]
    job = DownloadJob(job_id, url, format_id)
    with JOBS_LOCK:
        JOBS[job_id] = job

    choice = parse_format(format_id)
    mp3 = choice.kind == "audio" and choice.value > 0 and bool(get_ffmpeg_path())
    span = 50.0 if watermark or mp3 else 90.0

    def worker():
        converting_event = threading.Event()
        measured = threading.Event()  # the watermark encode reports real progress: the creep stops

        def ffmpeg_ticker():
            converting_event.wait()
            while job.status == "converting":
                time.sleep(0.4)
                if job.status != "converting":
                    break
                if measured.is_set():
                    continue
                with JOBS_LOCK:
                    if job.percent < 99.0:
                        job.percent = round(min(99.0, job.percent + 0.7), 1)
                        job.updated_at = time.time()

        threading.Thread(target=ffmpeg_ticker, daemon=True).start()

        streams: dict[str, list[int]] = {}
        finished: set[str] = set()

        def expected(d: dict[str, Any]) -> int:
            sizes = [
                f.get("filesize") or f.get("filesize_approx") or 0
                for f in (d.get("info_dict") or {}).get("requested_formats") or []
            ]
            seen = sum(t for _, t in streams.values())
            return max(sum(sizes) if all(sizes) else 0, seen)

        def hook(d: dict[str, Any]):
            status = d.get("status")
            job.updated_at = time.time()
            name = str(d.get("filename") or "")
            if status == "downloading":
                job.status = "downloading"
                down = d.get("downloaded_bytes", 0)
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                streams[name] = [down, max(total, down)]
                all_down = sum(s[0] for s in streams.values())
                all_total = expected(d)
                if all_total > 0:
                    job.percent = max(job.percent, round(min(span, all_down / all_total * span), 1))
                    job.downloaded_str = f"{all_down / (1024 * 1024):.1f} MB"
                    job.total_str = f"{all_total / (1024 * 1024):.1f} MB"
                else:
                    job.percent = max(job.percent, 15.0)
                    job.downloaded_str = f"{all_down / (1024 * 1024):.1f} MB"
                    job.total_str = "Calculating..."

                eta = d.get("eta")
                if eta is not None:
                    job.eta = max(1, int(eta))

                speed = d.get("speed")
                if speed:
                    if speed > 1024 * 1024:
                        job.speed_str = f"{speed / (1024 * 1024):.1f} MB/s"
                    elif speed > 1024:
                        job.speed_str = f"{speed / 1024:.0f} KB/s"
                    else:
                        job.speed_str = f"{speed:.0f} B/s"

                job.stage = "Downloading from YouTube..."
            elif status == "finished":
                finished.add(name)
                requested = (d.get("info_dict") or {}).get("requested_formats") or []
                if len(finished) < max(1, len(requested)):
                    return  # another stream (audio after video) is still to come
                job.status = "converting"
                job.percent = max(job.percent, span + 1)
                job.eta = None
                job.stage = "Processing..."
                converting_event.set()

        with JOBS_LOCK:
            job.status, job.percent, job.stage = "downloading", 2.0, "Looking up the video..."

        try:
            # The streamed-watermark shortcut (resolve_stream: hand ffmpeg YouTube's
            # direct googlevideo.com URL instead of downloading first) is disabled by
            # default (STREAM_WATERMARK = False below). YouTube's CDN frequently 403s a
            # request for that signed URL that doesn't come from yt-dlp's own HTTP
            # client — a separate, unrelated flakiness point from the bot-check this
            # app exists to test. Downloading through yt-dlp first (the branch below)
            # always uses the client that already proved it could fetch the stream.
            plan = resolve_stream(url, format_id) if STREAM_WATERMARK and watermark and choice.kind == "video" else None
            if plan:
                with JOBS_LOCK:
                    job.stream, job.filename, job.media_type = plan, plan.filename, "video/mp4"
                    job.status, job.percent, job.eta = "completed", 100.0, 0
                    job.stage = "Ready, starting your download..."
                _save(job)
                return
            file_path, filename, media_type = download_media_stream(
                url, format_id, progress_hook=hook, prefer_h264=watermark, dest=job_dir(job_id)
            )
            watermarking = watermark and media_type.startswith("video/")
            if watermarking or mp3:
                start = time.time()
                with JOBS_LOCK:
                    job.status, job.eta, job.stage = "converting", None, "Preparing your file..."
                    job.percent = max(job.percent, span)
                measured.set()
                converting_event.set()

                def encoded(f: float) -> None:
                    spent = time.time() - start
                    with JOBS_LOCK:
                        job.percent = max(job.percent, round(span + (99 - span) * f, 1))
                        job.eta = max(1, round(spent / f * (1 - f))) if f > 0.02 else None
                        job.updated_at = time.time()

                if watermarking:
                    file_path = add_watermark(file_path, encoded)
                    filename = str(Path(filename).with_suffix(".mp4"))
                else:
                    file_path = encode_mp3(file_path, choice.value, encoded)
                    filename = str(Path(filename).with_suffix(".mp3"))
            with JOBS_LOCK:
                job.file_path = file_path
                job.filename = filename
                job.media_type = media_type
                job.status = "completed"
                job.percent = 100.0
                job.eta = 0
                job.stage = "100% Complete"
            _save(job)
        except Exception as exc:
            with JOBS_LOCK:
                job.status = "failed"
                job.error = exc.detail if isinstance(exc, HTTPException) else format_media_error(exc)
            shutil.rmtree(job_dir(job_id), ignore_errors=True)
            log.warning("Media download job %s failed: %s", job_id, exc)

    DOWNLOAD_EXECUTOR.submit(worker)
    return job_id


def _video_formats(choice: Choice, prefer_h264: bool, merge: bool) -> tuple[str, str]:
    """yt-dlp format selectors for a video choice: the normal one, and the capped
    single-file one the Android fallback uses. `prefer_h264`: the file is re-encoded
    next (watermark), so the H.264 stream at the asked resolution comes first — it
    decodes several times faster than AV1/VP9."""
    side, cap = ("width" if choice.portrait else "height"), choice.value + TOLERANCE
    single = f"best[{side}<={cap}][ext=mp4]/best[{side}<={cap}]"
    if not merge:
        return single, single
    rung = f"[{side}<={cap}][{side}>={choice.value - TOLERANCE}]"
    h264 = f"bestvideo{rung}[vcodec^=avc1]+bestaudio[ext=m4a]/" if prefer_h264 else ""
    return (
        f"{h264}bestvideo[{side}<={cap}][ext=mp4]+bestaudio[ext=m4a]/"
        f"bestvideo[{side}<={cap}]+bestaudio/best[{side}<={cap}]"
    ), single


def _extract(safe_url: str, opts: dict[str, Any], download: bool, android_format: str) -> dict:
    """yt-dlp with the fallbacks YouTube needs: without cookies when they're stale, then
    the Android client with a still-capped single-file format."""
    meta = None
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            meta = ydl.extract_info(safe_url, download=download)
    except HTTPException:
        raise  # our own refusal: another client would only download it again
    except Exception as exc:
        if "cookiefile" in opts:
            log.info("yt-dlp with cookies failed (%s); trying without cookies...", exc)
            fallback_opts = dict(opts)
            fallback_opts.pop("cookiefile", None)
            try:
                with yt_dlp.YoutubeDL(fallback_opts) as ydl:
                    meta = ydl.extract_info(safe_url, download=download)
            except Exception:
                pass

        if not meta:
            log.info("yt-dlp failed (%s); trying the mobile android client...", exc)
            android_opts = dict(opts)
            android_opts.pop("cookiefile", None)
            android_opts["extractor_args"] = {"youtube": {"player_client": FALLBACK_CLIENTS}}
            android_opts["format"] = android_format
            try:
                with yt_dlp.YoutubeDL(android_opts) as ydl:
                    meta = ydl.extract_info(safe_url, download=download)
            except Exception as e2:
                log.warning("Android client fallback also failed: %s", e2)

        if not meta:
            log.warning("yt-dlp failed for %s: %s", safe_url, exc)
            raise HTTPException(400, format_media_error(exc)) from None
    if not meta:
        raise HTTPException(500, "We couldn't download that. Try again in a moment.")
    return meta


# ---- streamed watermark: YouTube's streams -> ffmpeg -> the browser, all at once ----

STREAMABLE = ("https", "http", "m3u8", "m3u8_native")


class StreamPlan(NamedTuple):
    inputs: list[tuple[str, dict[str, str]]]  # (url, HTTP headers), video first
    height: int
    kbps: int
    filename: str


def resolve_stream(url: str, format_id: str) -> StreamPlan | None:
    """The chosen quality's direct stream URLs, without downloading anything. None when
    a stream can't be read straight from its URL: the caller downloads first instead."""
    safe_url = validate_public_url(url)
    choice = parse_format(format_id)
    fmt, android = _video_formats(choice, prefer_h264=True, merge=True)
    opts = {**get_ydl_base_opts(), "format": fmt}
    meta = _extract(safe_url, opts, download=False, android_format=android)
    if meta.get("is_live"):
        raise HTTPException(400, LIVE)
    streams = meta.get("requested_formats") or [meta]
    if any(not f.get("url") or f.get("protocol") not in STREAMABLE for f in streams):
        return None
    for f in streams:
        check_stream_url(f["url"])
    video = next((f for f in streams if f.get("vcodec") not in (None, "none")), streams[0])
    return StreamPlan(
        [(f["url"], dict(f.get("http_headers") or {})) for f in streams],
        int(video.get("height") or 720),
        int(video.get("vbr") or video.get("tbr") or 0),
        clean_title(meta.get("title") or "download") + ".mp4",
    )


# what ffmpeg may open for YouTube's stream: never file:, concat:, subfile: or anything
# else a playlist could name
STREAM_PROTOCOLS = "http,https,tls,tcp,crypto,httpproxy"


def _input_args(url: str, headers: dict[str, str]) -> list[str]:
    agent = headers.get("User-Agent")
    rest = "".join(f"{k}: {v}\r\n" for k, v in headers.items() if k.lower() != "user-agent")
    web = ["-protocol_whitelist", STREAM_PROTOCOLS, "-reconnect", "1", "-reconnect_streamed", "1",
           "-reconnect_delay_max", "5"]  # fmt: skip
    return [
        *(["-user_agent", agent] if agent else []),
        *(["-headers", rest] if rest else []),
        *(web if "://" in url else []),
        "-i", url,
    ]  # fmt: skip


def stream_cmd(ffmpeg: str, plan: StreamPlan, mark: Path, hw: str | None) -> list[str]:
    """Reads YouTube's streams, burns the mark in, and writes fragmented MP4 to stdout:
    a file the browser can save while it's still being made."""
    n = len(plan.inputs)
    video = [hw, "-preset", "veryfast"] if hw else ["libx264", "-preset", "superfast"]
    return [
        ffmpeg, "-nostdin", "-loglevel", "error", *hwaccel.device_args(hw),
        *(arg for u, h in plan.inputs for arg in _input_args(u, h)), "-i", str(mark),
        "-filter_complex", f"[0:v][{n}:v]overlay=W-w-W*0.025:H-h-H*0.035,{hwaccel.pixel_filter(hw)}[v]",
        "-map", "[v]", "-map", f"{n - 1}:a?", "-c:a", "copy",
        "-c:v", *video, *bitrate_args(plan.kbps, plan.height), "-fs", str(MAX_MEDIA_BYTES),
        "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1",
    ]  # fmt: skip


MAX_STREAMS = max(1, settings.media_jobs)
MAX_STREAMS_PER_JOB = 2
STREAMS: dict[str, int] = {}
STREAMS_LOCK = threading.Lock()


class StreamsBusy(Exception):
    """No stream slot is free; the message says why, for the 429."""


def stream_in_slot(job_id: str, plan: StreamPlan) -> Iterator[bytes]:
    """Holds one stream slot from the first chunk until it ends or is closed."""
    with STREAMS_LOCK:
        if STREAMS.get(job_id, 0) >= MAX_STREAMS_PER_JOB:
            raise StreamsBusy("This file is already downloading. Wait for it to finish, then try again.")
        if sum(STREAMS.values()) >= MAX_STREAMS:
            raise StreamsBusy("Too many downloads are being made right now. Try again in a minute.")
        STREAMS[job_id] = STREAMS.get(job_id, 0) + 1
    try:
        yield from stream_watermarked(plan)
    finally:
        with STREAMS_LOCK:
            if (left := STREAMS.pop(job_id, 1) - 1) > 0:
                STREAMS[job_id] = left


def stream_watermarked(plan: StreamPlan) -> Iterator[bytes]:
    """The watermarked file, chunk by chunk, as ffmpeg makes it."""
    with tempfile.TemporaryDirectory(prefix="media-wm-") as tmp:
        mark = Path(tmp) / "watermark.png"
        watermark_png(plan.height, mark)
        for hw in dict.fromkeys((hwaccel.encoder_for("libx264"), None)):
            with tempfile.TemporaryFile() as err:
                proc = subprocess.Popen(
                    stream_cmd(settings.ffmpeg, plan, mark, hw),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=err,
                )
                assert proc.stdout is not None
                sent = False
                try:
                    while chunk := proc.stdout.read(64 * 1024):
                        sent = True
                        yield chunk
                    code = proc.wait()
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait()
                if code == 0:
                    return
                err.seek(0)
                log.warning(
                    "streamed watermark with %s failed%s: %s",
                    hw or "libx264",
                    " mid-file" if sent else "",
                    err.read().decode(errors="replace")[-300:],
                )
                if sent:
                    return


def download_media_stream(
    url: str,
    format_id: str,
    progress_hook: Callable[[dict[str, Any]], None] | None = None,
    prefer_h264: bool = False,
    dest: Path | None = None,
) -> tuple[Path, str, str]:
    """Downloads into `dest` (a job's folder) or a fresh temp folder."""
    safe_url = validate_public_url(url)
    scratch_dir = dest or Path(tempfile.gettempdir()) / f"media-dl-{uuid.uuid4().hex}"
    scratch_dir.mkdir(parents=True, exist_ok=True)

    out_tmpl = str(scratch_dir / "stream.%(ext)s")

    opts = get_ydl_base_opts()
    opts["outtmpl"] = out_tmpl
    opts["max_filesize"] = MAX_MEDIA_BYTES
    opts["match_filter"] = _before_download
    opts["progress_hooks"] = [_byte_cap(MAX_MEDIA_BYTES), *([progress_hook] if progress_hook else [])]

    choice = parse_format(format_id)
    is_audio = choice.kind == "audio"

    ffmpeg_path = get_ffmpeg_path()
    android_format = "bestaudio/best"

    if is_audio:
        if choice.value and ffmpeg_path:
            opts["format"] = "bestaudio/best"
            media_type = "audio/mpeg"
        else:
            opts["format"] = "bestaudio[ext=m4a]/bestaudio/best"
            media_type = "audio/mp4"
    else:
        opts["format"], android_format = _video_formats(choice, prefer_h264, merge=bool(ffmpeg_path))
        if ffmpeg_path:
            opts["merge_output_format"] = "mp4"
        media_type = "video/mp4"

    meta = _extract(safe_url, opts, download=True, android_format=android_format)

    files = [f for f in scratch_dir.glob("*") if f.is_file() and not f.name.endswith((".part", ".ytdl"))]
    if not files:
        raise HTTPException(500, "We couldn't download that. Try again in a moment.")

    files.sort(key=lambda f: f.stat().st_size, reverse=True)
    downloaded_file = files[0]
    filename = clean_title(meta.get("title") or "download") + downloaded_file.suffix

    return downloaded_file, filename, media_type
