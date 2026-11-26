# Build context: repo root.  docker compose build transcriber
# CUDA image: CTranslate2 (faster-whisper) and onnxruntime-gpu (CED; its CUDA 12 build) both use
# the base image's CUDA 12.8 runtime, cuBLAS and cuDNN 9. Same base as tools/asr-bench, so the
# layers are shared. No torch: CED runs as an ONNX graph exported at build time (stage
# ced-export; only the .onnx is kept).
# Layers, slowest-changing first: apt, Silero model, CED model, Python, third-party venv (rebuilt
# only when uv.lock or a used pyproject changes), then the workspace packages (~1 MB per change).

FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04 AS python
COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python UV_PYTHON_PREFERENCE=only-managed
RUN uv python install 3.12.15

# CED-base as ONNX: float16 transformer, float32 mel frontend inside the graph, exported from the
# pinned Hub weights (mispeech/ced-base @ db3e14a, model.safetensors) with the pinned CPU
# toolchain of ced_export/requirements.txt. The build fails unless the file is byte-identical to
# the evaluated export (sha256 = clipper_transcriber.ced.CED_ONNX_SHA256; a test keeps the two
# equal); that also covers the weights, which aren't checked on their own. CED's Hub code also
# downloads topel/ConvNeXt-Tiny-AT's class_labels_indices.csv from that repo's unpinned main
# branch: it only names the labels (not in the graph), so the build depends on that repo being
# there, and check_labels.py fails the build unless the group labels sit at the AudioSet indices
# the transcriber reads. Needs Hub access (~345 MB, not cached); the venv and the weights are
# deleted in the same step, so the layer holds only /out.
FROM python AS ced-export
RUN --mount=type=bind,source=services/transcriber/ced_export,target=/ced_export \
    uv venv /x --python 3.12.15 \
 && uv pip install --no-cache --python /x/bin/python -r /ced_export/requirements.txt \
      --index https://download.pytorch.org/whl/cpu --index-strategy unsafe-best-match \
 && HF_HOME=/tmp/hf HF_HUB_DISABLE_PROGRESS_BARS=1 \
    /x/bin/python /ced_export/export_onnx.py --dtype fp16 --out /out/ced-base-fp16.onnx \
 && echo "76cbe72ec8aa39d1291fcdb1bbd722cfa3be5b99a932ba7d85428478c46a6dc6  /out/ced-base-fp16.onnx" \
    | sha256sum -c - \
 && /x/bin/python /ced_export/check_labels.py /out/id2label.json \
 && rm -rf /x /tmp/hf

FROM python AS deps
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
# Only what the dependencies depend on (the members clipper-transcriber needs).
COPY pyproject.toml uv.lock .python-version ./
COPY libs/core/pyproject.toml libs/core/
COPY services/transcriber/pyproject.toml services/transcriber/
# onnxruntime-gpu (CUDA 12 build, from ORT's feed; uv.lock pins its wheel) and no CPU
# `onnxruntime` (the root pyproject's override keeps faster-whisper's requirement out on Linux).
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_PYTHON_DOWNLOADS=never uv sync --frozen --no-dev --no-install-workspace \
      --package clipper-transcriber --extra whisper

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
# CED at clipper_transcriber.ced.CED_ONNX_PATH (the default of CLIPPER_TRANSCRIBER_TAGGER_MODEL_PATH).
# (No --chmod: it would also apply to the created /opt/ced, making it untraversable; the file is
# 644 already.)
COPY --from=ced-export /out/ced-base-fp16.onnx /opt/ced/ced-base-fp16.onnx
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
    uv sync --frozen --no-dev --no-editable --package clipper-transcriber --extra whisper
# Whisper's weights are downloaded into the `models` volume on first start. No progress bars:
# every log line is JSON.
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 HF_HOME=/models/hf \
    HF_HUB_DISABLE_PROGRESS_BARS=1 \
    CLIPPER_TRANSCRIBER_MODELS_DIR=/models \
    CLIPPER_TRANSCRIBER_VAD_MODEL_PATH=/opt/silero_vad.onnx
VOLUME /models
# No NVIDIA entrypoint: its banner isn't JSON.
ENTRYPOINT []
CMD ["clipper-transcriber"]
