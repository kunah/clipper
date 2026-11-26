# Build context: repo root.  docker compose build backfill
# One-off tool (profile `tools`): docker compose run --rm backfill vod <id> --dry-run
FROM python:3.12.15-slim-trixie AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package clipper-backfill

FROM python:3.12.15-slim-trixie
# ffmpeg copies the audio track out of each chunk (and remuxes fMP4 to .ts); no re-encoding.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
ENTRYPOINT ["clipper-backfill"]
CMD ["--help"]
