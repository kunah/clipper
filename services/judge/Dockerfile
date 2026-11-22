# Build context: repo root.  docker compose build judge
# The judge (DESIGN §19) with a pinned Claude Code CLI for CLIPPER_JUDGE_MODEL=claude-cli (6d).

# The Claude Code CLI: the native binary of one release, straight from Anthropic's release bucket
# (the URL the native installer uses), checked against the sha256 that release's manifest lists.
# The manifest's signature was checked on 2026-11-22: good signature by the release key
# 31DD DE24 DDFA B679 F42D 7BD2 BAA9 29FF 1A7E CACE (code.claude.com/docs/en/setup, "Binary
# integrity"); the win32-x64 checksum matches the CLI on the dev PC. No Node, no installer
# script, no auto-update (the binary is root-owned; the adapter sets DISABLE_AUTOUPDATER and
# DISABLE_UPDATES). ~363 MiB (381,121,040 bytes for linux-x64) on top of the slim image.
# Another version: change it in both URLs and both checksums (`platforms.linux-x64|linux-arm64`
# of https://downloads.claude.ai/claude-code-releases/<version>/manifest.json, after verifying
# manifest.json.sig) and update DESIGN §19.
ARG TARGETARCH
FROM scratch AS claude-amd64
ADD --chmod=0755 \
    --checksum=sha256:f390eaa0814e6a1bff74cbb47acb74cb9c89d30ced809fe0af5af00c7bfe8ebb \
    https://downloads.claude.ai/claude-code-releases/2.1.330/linux-x64/claude /claude

FROM scratch AS claude-arm64
ADD --chmod=0755 \
    --checksum=sha256:6016f90cac783e367fb1aa97c768a2efb94a0128a5b46b70fb3d8fcbbd9dd444 \
    https://downloads.claude.ai/claude-code-releases/2.1.330/linux-arm64/claude /claude

FROM claude-${TARGETARCH} AS claude

FROM python:3.12.15-slim-trixie AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package clipper-judge

FROM python:3.12.15-slim-trixie
COPY --from=build /app/.venv /app/.venv
COPY --from=claude /claude /usr/local/bin/claude
# Non-root. /var/lib/clipper-judge is the CLI's own HOME and CLAUDE_CONFIG_DIR (the adapter's
# claude_config_dir; compose mounts the `judge-claude` volume there, which takes this owner).
RUN useradd --system --uid 10001 --user-group --create-home --home-dir /home/judge judge \
    && install -d -o judge -g judge -m 0700 /var/lib/clipper-judge
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 \
    CLIPPER_JUDGE_CLAUDE_CONFIG_DIR=/var/lib/clipper-judge
USER judge
CMD ["clipper-judge"]
