# Build context: repo root.  docker compose build transcriber
# CUDA image: CTranslate2 (faster-whisper) uses the base image's cuBLAS + cuDNN 9. Same base as
# tools/asr-bench, so the layers are shared. torch is the CPU build (CED runs on the CPU).
# Layers, slowest-changing first: apt, Silero model, Python, third-party venv (~1.7 GB, rebuilt
# only when uv.lock or a used pyproject changes), then the workspace packages (~1 MB per change).

FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04 AS deps
COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    UV_PYTHON_PREFERENCE=only-managed UV_PROJECT_ENVIRONMENT=/app/.venv
RUN uv python install 3.12.15
WORKDIR /src
# Only what the dependencies depend on (the members clipper-transcriber needs).
COPY pyproject.toml uv.lock .python-version ./
COPY libs/core/pyproject.toml libs/core/
COPY services/transcriber/pyproject.toml services/transcriber/
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_PYTHON_DOWNLOADS=never uv sync --frozen --no-dev --no-install-workspace \
      --package clipper-transcriber --extra whisper --extra ced

FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04
# ffmpeg decodes the chunk audio to 16 kHz PCM; tzdata for psycopg's time zone lookup.
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ffmpeg tzdata \
 && rm -rf /var/lib/apt/lists/*
# Silero VAD v6.2 baked in, checked against the sha256 pinned in clipper_transcriber/vad.py (a
# test keeps the two in sync): no GitHub access at runtime.
ADD --checksum=sha256:1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3 --chmod=644 \
    https://raw.githubusercontent.com/snakers4/silero-vad/5cd7945676eb32225748052e2e6a0580e4686a08/src/silero_vad/data/silero_vad.onnx \
    /opt/silero_vad.onnx
COPY --from=deps /opt/uv-python /opt/uv-python
COPY --from=deps /app/.venv /app/.venv
# The workspace packages (clipper-core, clipper-transcriber) on top. uv and the sources are
# mounted, not copied into the image; only the files the packages need, so other changes in the
# repo don't rebuild this layer.
RUN --mount=from=ghcr.io/astral-sh/uv:0.13.9,source=/uv,target=/bin/uv \
    --mount=type=bind,source=pyproject.toml,target=/src/pyproject.toml \
    --mount=type=bind,source=uv.lock,target=/src/uv.lock \
    --mount=type=bind,source=.python-version,target=/src/.python-version \
    --mount=type=bind,source=libs/core,target=/src/libs/core,rw \
    --mount=type=bind,source=services/transcriber,target=/src/services/transcriber,rw \
    --mount=type=cache,target=/root/.cache/uv \
    cd /src \
 && UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    UV_PYTHON_PREFERENCE=only-managed UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv \
    uv sync --frozen --no-dev --no-editable --package clipper-transcriber \
      --extra whisper --extra ced
# Model weights (Whisper, CED + its label list) are downloaded into the `models` volume on first
# start. No progress bars: every log line is JSON.
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 HF_HOME=/models/hf \
    HF_HUB_DISABLE_PROGRESS_BARS=1 \
    CLIPPER_TRANSCRIBER_MODELS_DIR=/models \
    CLIPPER_TRANSCRIBER_VAD_MODEL_PATH=/opt/silero_vad.onnx
VOLUME /models
# No NVIDIA entrypoint: its banner isn't JSON.
ENTRYPOINT []
CMD ["clipper-transcriber"]
