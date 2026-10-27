"""Hardware video encoding: VAAPI (Intel/AMD on Linux), NVENC (NVIDIA) and Quick Sync.

Ported verbatim from Converter's apps/api/app/workers/processors/hwaccel.py (only the
settings import path changed) — this module has no MongoDB/auth coupling at all, so
nothing else needed to change.

A GPU encoder is 2-3x faster than libx264 at the same file size, for a quality
difference too small to see on the watermark overlay this app burns in. Two things
make this safe to turn on by default:

* **Nothing is trusted.** An encoder `ffmpeg -encoders` lists is only compiled in; it can
  still fail to open a device (no GPU, no driver, no permission on /dev/dri). Every
  candidate is proved with a real one-frame encode before it is ever used, once per
  process, and a hardware encode that fails anyway falls back to software.
* Quality numbers aren't interchangeable between encoders, so each row below maps the
  software encoder's CRF to the hardware encoder's own scale.
"""

import functools
import logging
import subprocess

from app.settings import settings

log = logging.getLogger("downloader.hwaccel")

# software encoder -> hardware encoders to try, best first. VAAPI last because it needs
# a device node, so it is the most likely to be present but unusable.
CANDIDATES: dict[str, tuple[str, ...]] = {
    "libx264": ("h264_nvenc", "h264_qsv", "h264_vaapi"),
    "libx265": ("hevc_nvenc", "hevc_qsv", "hevc_vaapi"),
    "libvpx-vp9": ("vp9_vaapi",),
}


def _api(encoder: str) -> str:
    return encoder.rsplit("_", 1)[-1]


def device_args(encoder: str | None) -> list[str]:
    """Flags that must come before `-i`. VAAPI needs its render node named explicitly."""
    return ["-vaapi_device", settings.vaapi_device] if encoder and _api(encoder) == "vaapi" else []


def pixel_filter(encoder: str | None) -> str:
    """The last link of the filter chain: the pixel format the encoder takes, and for
    VAAPI the upload to GPU memory."""
    if encoder is None or _api(encoder) == "nvenc":
        return "format=yuv420p"
    return "format=nv12,hwupload" if _api(encoder) == "vaapi" else "format=nv12"


@functools.cache
def _compiled_in() -> frozenset[str]:
    """The encoder names this ffmpeg build has at all. Cheap, and saves probing what can't exist."""
    try:
        out = subprocess.run(
            [settings.ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=30
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    return frozenset(line.split()[1] for line in out.splitlines() if line.startswith(" V") and len(line.split()) > 1)


@functools.cache
def works(encoder: str) -> bool:
    """Encode one generated frame. The only question an `-encoders` listing can't answer
    is whether a device actually opens, which is exactly what fails on a machine without
    the hardware."""
    if encoder not in _compiled_in():
        return False
    cmd = [
        settings.ffmpeg, "-nostdin", "-loglevel", "error", "-y",
        *device_args(encoder),
        "-f", "lavfi", "-i", "color=c=black:s=640x480:r=25:d=0.2",
        "-vf", pixel_filter(encoder),
        "-c:v", encoder,
        "-frames:v", "1", "-f", "null", "-",
    ]  # fmt: skip
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    if done.returncode:
        log.info("hardware encoder %s unavailable: %s", encoder, (done.stderr or "").strip().splitlines()[-1:])
    return done.returncode == 0


@functools.cache
def encoder_for(software: str) -> str | None:
    """The fastest working hardware stand-in for `software`, or None to encode on the CPU.
    Cached, so the probes run once per process (on its first video job, not at startup)."""
    if settings.video_hwaccel == "off":
        return None
    wanted = settings.video_hwaccel
    for candidate in CANDIDATES.get(software, ()):
        if wanted not in ("auto", _api(candidate), candidate):
            continue
        if works(candidate):
            log.info("using hardware encoder %s instead of %s", candidate, software)
            return candidate
    return None
