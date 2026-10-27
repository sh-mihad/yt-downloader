# Build context is the repo root (render.yaml: dockerContext: .), so this file can
# COPY both apps/api and apps/web.

# YouTube PO token server (bgutil). Tag must match the bgutil-ytdlp-pot-provider pin in
# apps/api/pyproject.toml.
FROM node:22-bookworm AS pot
RUN git clone --depth 1 --single-branch --branch 2.0.2 \
      https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /pot \
    && cd /pot/server && npm ci && npx tsc

# The plain minimal frontend (one page, no design system).
FROM node:22-bookworm AS web
WORKDIR /web
COPY apps/web/package.json ./
RUN corepack enable && pnpm install
COPY apps/web .
RUN pnpm build

FROM python:3.13-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY --from=pot /usr/local/bin/node /usr/local/bin/node
COPY --from=pot /pot/server /opt/bgutil
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /srv/apps/api
COPY apps/api/pyproject.toml ./
RUN uv sync
COPY apps/api .
COPY --from=web /web/dist ./web-dist

ENV PATH="/srv/apps/api/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

CMD ["sh", "start.sh"]
