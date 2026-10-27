"""Settings for the standalone downloader test app.

Trimmed from Converter's app/infrastructure/settings.py: only the fields
app/downloader.py, app/hwaccel.py and app/ratelimit.py actually touch. No
`better_auth_secret` or any other required field — this app has no auth.
"""

import shutil
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Where downloaded cookies/scratch files live. Relative to the working directory
    # (run uvicorn from apps/api/, same as the Dockerfile's WORKDIR).
    storage_dir: Path = Path("data")

    ffmpeg: str = "ffmpeg"
    # Download/encode jobs that may run at once in this process; more wait as "queued".
    media_jobs: int = 2

    @field_validator("ffmpeg")
    @classmethod
    def _bundled_ffmpeg(cls, value: str) -> str:
        """Plain `ffmpeg` missing from PATH: use the static binary imageio-ffmpeg ships.
        A child process started by pnpm/concurrently/Node doesn't always inherit a
        user-level PATH entry (e.g. a WinGet install) the way a terminal shell does, so
        `shutil.which` can find it here but `subprocess.Popen(["ffmpeg", ...])` still
        fail once uvicorn is launched that way — this makes the watermark/MP3 encode
        work regardless of how the process was started."""
        if value == "ffmpeg" and not shutil.which("ffmpeg"):
            try:
                import imageio_ffmpeg

                return imageio_ffmpeg.get_ffmpeg_exe()
            except Exception:
                pass
        return value

    # GPU video encoding for the watermark overlay: auto = use it when a one-frame probe
    # proves it works, off = always use the CPU encoder.
    video_hwaccel: str = "auto"
    vaapi_device: str = "/dev/dri/renderD128"

    # YouTube bot-check bypass: Netscape-format cookies (plain text or base64), and an
    # optional HTTP/SOCKS proxy (http://user:pass@host:port). See .env.example.
    youtube_cookies: str = ""
    youtube_proxy: str = ""

    rate_limits: bool = True
    trust_proxy: bool = False


settings = Settings()
settings.storage_dir.mkdir(parents=True, exist_ok=True)
