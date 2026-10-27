"""In-memory fixed-window rate limiter.

Trimmed from Converter's app/core/ratelimit.py, which keeps counts in MongoDB so every
API process shares one count. This standalone app is one process with no shared state
to worry about, so a plain dict + lock does the same job. Same `hit(key, limit, window)`
signature as the original.
"""

import threading
import time

from fastapi import HTTPException, Request

_WINDOWS: dict[str, tuple[int, int]] = {}  # key -> (bucket, count)
_LOCK = threading.Lock()


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def hit(key: str, limit: int, window: int) -> None:
    """Counts one request against `key`; 429 once more than `limit` arrive in `window` seconds."""
    bucket = int(time.time() // window)
    with _LOCK:
        b, n = _WINDOWS.get(key, (bucket, 0))
        n = n + 1 if b == bucket else 1
        _WINDOWS[key] = (bucket, n)
    if n > limit:
        retry = window - int(time.time()) % window
        raise HTTPException(
            429, f"Too many requests. Wait {retry} seconds and try again.", headers={"Retry-After": str(retry)}
        )
