# Build context: repo root.  docker compose build chat-ingest
FROM python:3.12.15-slim-trixie AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package clipper-chat-ingest

FROM python:3.12.15-slim-trixie
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
CMD ["clipper-chat-ingest"]
