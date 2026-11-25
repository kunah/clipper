# Build context: repo root.  docker compose build renderer
# The renderer (DESIGN §20): a pinned static ffmpeg with NVENC, libx264 and libass (the portable
# GPL build of jellyfin-ffmpeg 8.1.3-2, released 2026-11-03; the sha256 values are the release
# assets' digests, the amd64 one checked against a download on 2026-11-25) and the bundled OFL
# fonts (package data). No system fonts: libass can only use the bundled ones (fontsdir).
# NVENC comes from the driver: the NVIDIA runtime mounts libnvidia-encode when the container has
# the `video` driver capability (NVIDIA_DRIVER_CAPABILITIES below; compose reserves the GPU).
# Another ffmpeg: change both URLs and checksums (assets of
# https://github.com/jellyfin/jellyfin-ffmpeg/releases) and update DESIGN §20.
ARG TARGETARCH
FROM scratch AS ffmpeg-amd64
ADD --checksum=sha256:1df84d04c4322902dba81c290508cdf3d89524d210b9fe03154bea6c4e3bfbb6 \
    https://github.com/jellyfin/jellyfin-ffmpeg/releases/download/v8.1.3-2/jellyfin-ffmpeg_8.1.3-2_portable_linux64-gpl.tar.xz \
    /ffmpeg.tar.xz

FROM scratch AS ffmpeg-arm64
ADD --checksum=sha256:6522585818059cc886363fb1dee86ac9b8579fec3b914337fb92974ef9eb9884 \
    https://github.com/jellyfin/jellyfin-ffmpeg/releases/download/v8.1.3-2/jellyfin-ffmpeg_8.1.3-2_portable_linuxarm64-gpl.tar.xz \
    /ffmpeg.tar.xz

FROM ffmpeg-${TARGETARCH} AS ffmpeg-tar

FROM python:3.12.15-slim-trixie AS ffmpeg
COPY --from=ffmpeg-tar /ffmpeg.tar.xz /tmp/ffmpeg.tar.xz
RUN python -c "import tarfile; tarfile.open('/tmp/ffmpeg.tar.xz').extractall('/ffmpeg', filter='data')" \
 && /ffmpeg/ffmpeg -hide_banner -version | head -1 \
 && /ffmpeg/ffmpeg -hide_banner -filters | grep -qE ' ass .*libass' \
 && /ffmpeg/ffmpeg -hide_banner -encoders | grep -q h264_nvenc \
 && /ffmpeg/ffmpeg -hide_banner -encoders | grep -q libx264

FROM python:3.12.15-slim-trixie AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package clipper-renderer

FROM python:3.12.15-slim-trixie
COPY --from=build /app/.venv /app/.venv
COPY --from=ffmpeg /ffmpeg/ffmpeg /ffmpeg/ffprobe /usr/local/bin/
# Non-root; /work holds each job's chunks and outputs (compose mounts a volume there).
# fontconfig (inside the static ffmpeg) gets a config without font directories: no system fonts
# (libass uses the bundled ones from fontsdir), and no "Cannot load default config file" errors.
RUN useradd --system --uid 10002 --user-group --no-create-home renderer \
    && install -d -o renderer -g renderer -m 0750 /work \
    && mkdir -p /etc/fonts \
    && printf '%s\n' '<?xml version="1.0"?>' '<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">' \
       '<fontconfig><cachedir>/tmp/fontconfig</cachedir></fontconfig>' > /etc/fonts/fonts.conf
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 CLIPPER_RENDERER_TEMP_DIR=/work \
    FONTCONFIG_FILE=/etc/fonts/fonts.conf NVIDIA_DRIVER_CAPABILITIES=compute,video,utility
USER renderer
CMD ["clipper-renderer"]
