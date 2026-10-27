#!/bin/sh
# Render (and other PaaS hosts) sometimes inject HTTP_PROXY/HTTPS_PROXY platform-wide for
# their own internal routing, pointing at an address that can't reach the public internet.
# yt-dlp and Node's own fetch both fall back to these ambient env vars whenever present,
# regardless of what we pass as our own opt-in YOUTUBE_PROXY — so unless the operator
# explicitly set YOUTUBE_PROXY themselves, strip the ambient ones before anything starts.
if [ -z "$YOUTUBE_PROXY" ]; then
  unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
fi

# YouTube PO token server on 127.0.0.1:4416; the yt-dlp plugin finds it there, and skips
# it when it's down. Decision 27 in Converter's apps/api/docs/decisions.md explains why
# this exists: from a cloud datacenter IP, YouTube answers "confirm you're not a bot"
# even with valid cookies, and this server supplies the proof-of-origin token it wants.
[ -f /opt/bgutil/build/main.js ] && (while true; do node /opt/bgutil/build/main.js; sleep 2; done) &

# --proxy-headers/--forwarded-allow-ips so Render's own proxy IP doesn't become the
# client's address in logs/rate-limit keys.
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --proxy-headers --forwarded-allow-ips="*"
