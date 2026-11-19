# Speech model benchmark

Picks the transcriber's speech model and sound classifier from measurements on real stream audio
and this machine's GPU. Not part of the uv workspace: the models run in their own Docker image
(one venv per framework).

```sh
# 1. audio: N minutes from live English channels -> data/bench/audio (60 s chunks, .aac + .wav)
uv run python tools/asr-bench/collect.py caseoh_ hasanabi jynxzi --minutes 4

# 2. image (CUDA + faster-whisper, NeMo, qwen-asr, AudioSet classifiers)
docker build -t clipper/asr-bench tools/asr-bench

# 3. run each model (Git Bash: MSYS_NO_PATHCONV=1 keeps /data paths intact).
#    Keep the GPU otherwise idle while this runs, or the speed numbers are off.
export MSYS_NO_PATHCONV=1
run() { docker run --rm --gpus all -v asr-bench-models:/models \
          -v "$(cygpath -w "$PWD/data/bench"):/data" clipper/asr-bench "$@"; }
run /venv/whisper/bin/python run_asr.py faster-whisper large-v3-turbo
run /venv/whisper/bin/python run_asr.py faster-whisper large-v3
run /venv/nemo/bin/python run_asr.py nemo nvidia/parakeet-tdt-0.6b-v2
run /venv/qwen/bin/python run_asr.py qwen Qwen/Qwen3-ASR-1.7B
run /venv/qwen/bin/python run_asr.py qwen Qwen/Qwen3-ASR-0.6B
run /venv/tagger/bin/python run_tagger.py ast
run /venv/tagger/bin/python run_tagger.py ced

# 4. report: tables on stdout, data/bench/report.html to listen (open it in a browser)
run /venv/whisper/bin/python report.py
```

Model weights are cached in the `asr-bench-models` volume. There is no human transcript of the
audio, so the report shows public leaderboard WER, agreement between the models, and a listening
page where each model's words highlight in time with the audio.
