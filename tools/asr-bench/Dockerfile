# Speech-model benchmark image: one venv per framework so their dependencies can't clash.
#   docker build -t clipper/asr-bench tools/asr-bench
# See tools/asr-bench/README.md for how to run it.
FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04

COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /bin/uv
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg libsndfile1 build-essential ca-certificates \
 && rm -rf /var/lib/apt/lists/*
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python UV_LINK_MODE=copy HF_HOME=/models/hf \
    TORCH_HOME=/models/torch PYTHONUNBUFFERED=1
RUN uv python install 3.12

# faster-whisper (CTranslate2; uses the image's cuBLAS/cuDNN) + report tooling
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /venv/whisper --python 3.12 \
 && uv pip install --python /venv/whisper/bin/python \
      faster-whisper nvidia-ml-py jiwer whisper-normalizer numpy soundfile
# NVIDIA NeMo (Parakeet)
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /venv/nemo --python 3.12 \
 && uv pip install --python /venv/nemo/bin/python "nemo_toolkit[asr]" nvidia-ml-py soundfile
# Qwen3-ASR + forced aligner (transformers backend)
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /venv/qwen --python 3.12 \
 && uv pip install --python /venv/qwen/bin/python qwen-asr nvidia-ml-py soundfile
# Sound classifiers (AudioSet): AST, CED
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /venv/tagger --python 3.12 \
 && uv pip install --python /venv/tagger/bin/python \
      torch torchaudio transformers nvidia-ml-py soundfile numpy

WORKDIR /bench
COPY *.py *.html /bench/
