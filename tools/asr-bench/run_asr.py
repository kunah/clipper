"""Run one speech model over the benchmark chunks (inside the bench image).

    python run_asr.py <engine> <model> [--out NAME]
    engines: faster-whisper | nemo | qwen

Each 60 s chunk is transcribed on its own, like the transcriber will do. Measures load time,
wall time per chunk (after one warm-up), GPU memory (NVML, whole device, minus the baseline
before loading) and writes words with timestamps to /data/results/asr-<NAME>.json.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import platform
import threading
import time
from importlib import metadata
from pathlib import Path

import pynvml

DATA = Path("/data")


class GpuMemory:
    """Samples used GPU memory every 20 ms; reports MB above the starting baseline."""

    def __init__(self) -> None:
        pynvml.nvmlInit()
        self.handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        self.gpu = pynvml.nvmlDeviceGetName(self.handle)
        # How busy the GPU was before we started (games, video...): >10 % skews speed numbers.
        samples = []
        for _ in range(10):
            samples.append(pynvml.nvmlDeviceGetUtilizationRates(self.handle).gpu)
            time.sleep(0.1)
        self.busy_before = round(sum(samples) / len(samples))
        if self.busy_before > 10:
            print(f"WARNING: GPU {self.busy_before}% busy before start; speed numbers are off")
        self.baseline = self.used()
        self.peak = self.baseline
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample, daemon=True)
        self._thread.start()

    def used(self) -> int:
        return pynvml.nvmlDeviceGetMemoryInfo(self.handle).used // 2**20

    def _sample(self) -> None:
        while not self._stop.is_set():
            self.peak = max(self.peak, self.used())
            time.sleep(0.02)

    def above_baseline(self, value: int) -> int:
        return value - self.baseline

    def stop(self) -> None:
        self._stop.set()


# ----------------------------------------------------------------------------- engines
# Each returns (load(), transcribe(wav) -> (text, [(word, start_s, end_s)])).


def faster_whisper_engine(model: str):
    import soundfile as sf
    from faster_whisper import WhisperModel

    m = WhisperModel(model, device="cuda", compute_type="float16")

    def transcribe(wav: str):
        # Decode ourselves (16 kHz mono float32): faster-whisper's own file reading via PyAV
        # breaks with current PyAV, and the transcriber decodes the .aac itself anyway.
        audio, _ = sf.read(wav, dtype="float32")
        segments, _ = m.transcribe(
            audio,
            language="en",
            beam_size=5,
            word_timestamps=True,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        words = [(w.word.strip(), w.start, w.end) for s in segments for w in (s.words or [])]
        return " ".join(w for w, _, _ in words), words

    return transcribe, {"compute_type": "float16", "beam_size": 5, "vad_filter": True}


def nemo_engine(model: str):
    import nemo.collections.asr as nemo_asr
    import torch

    m = nemo_asr.models.ASRModel.from_pretrained(model_name=model).to("cuda").eval()

    def transcribe(wav: str):
        with torch.inference_mode():
            out = m.transcribe([wav], timestamps=True, batch_size=1, verbose=False)[0]
        words = [(w["word"], float(w["start"]), float(w["end"])) for w in out.timestamp["word"]]
        return out.text, words

    return transcribe, {"precision": "float32", "decoding": "greedy (default)"}


def qwen_engine(model: str):
    import torch
    from qwen_asr import Qwen3ASRModel

    m = Qwen3ASRModel.from_pretrained(
        model,
        dtype=torch.bfloat16,
        device_map="cuda:0",
        max_inference_batch_size=8,
        max_new_tokens=1024,
        forced_aligner="Qwen/Qwen3-ForcedAligner-0.6B",
        forced_aligner_kwargs=dict(dtype=torch.bfloat16, device_map="cuda:0"),
    )

    def transcribe(wav: str):
        r = m.transcribe(audio=[wav], language=["English"], return_time_stamps=True)[0]
        items = list(r.time_stamps or [])
        words = [(i.text, float(i.start_time), float(i.end_time)) for i in items]
        if words and max(e for _, _, e in words) > 1000:  # milliseconds, not seconds
            words = [(w, s / 1000, e / 1000) for w, s, e in words]
        return r.text, words

    return transcribe, {"dtype": "bfloat16", "aligner": "Qwen/Qwen3-ForcedAligner-0.6B"}


ENGINES = {"faster-whisper": faster_whisper_engine, "nemo": nemo_engine, "qwen": qwen_engine}
PACKAGES = {
    "faster-whisper": ["faster-whisper", "ctranslate2"],
    "nemo": ["nemo_toolkit", "torch"],
    "qwen": ["qwen-asr", "transformers", "torch"],
}


def versions(engine: str) -> dict[str, str]:
    out = {"python": platform.python_version()}
    for pkg in PACKAGES[engine]:
        with contextlib.suppress(metadata.PackageNotFoundError):
            out[pkg] = metadata.version(pkg)
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("engine", choices=list(ENGINES))
    p.add_argument("model")
    p.add_argument("--out", help="result name (default: model name)")
    args = p.parse_args()
    manifest = json.loads((DATA / "manifest.json").read_text())
    chunks = manifest["chunks"]

    mem = GpuMemory()
    t0 = time.perf_counter()
    transcribe, config = ENGINES[args.engine](args.model)
    load_s = time.perf_counter() - t0
    after_load = mem.used()

    transcribe(str(DATA / "audio" / chunks[0]["wav"]))  # warm-up (CUDA kernels, caches)
    results = []
    for c in chunks:
        t = time.perf_counter()
        text, words = transcribe(str(DATA / "audio" / c["wav"]))
        wall = time.perf_counter() - t
        results.append(
            {
                "id": c["id"],
                "seconds": c["seconds"],
                "wall_s": round(wall, 4),
                "text": text,
                "words": [{"w": w, "s": round(s, 3), "e": round(e, 3)} for w, s, e in words],
            }
        )
        print(f"{c['id']}: {wall:6.2f} s, {len(words)} words", flush=True)
    mem.stop()

    name = args.out or args.model.split("/")[-1]
    audio_s = sum(r["seconds"] for r in results)
    wall_s = sum(r["wall_s"] for r in results)
    out = {
        "name": name,
        "engine": args.engine,
        "model": args.model,
        "config": config,
        "versions": versions(args.engine),
        "gpu": mem.gpu,
        "gpu_busy_before_pct": mem.busy_before,
        "load_s": round(load_s, 2),
        "vram_after_load_mb": mem.above_baseline(after_load),
        "vram_peak_mb": mem.above_baseline(mem.peak),
        "audio_s": round(audio_s, 2),
        "wall_s": round(wall_s, 3),
        "rtfx": round(audio_s / wall_s, 1),
        "chunks": results,
    }
    (DATA / "results").mkdir(exist_ok=True)
    (DATA / "results" / f"asr-{name}.json").write_text(json.dumps(out, indent=1))
    print(f"{name}: {out['rtfx']}x realtime, peak {out['vram_peak_mb']} MB, load {load_s:.1f} s")


if __name__ == "__main__":
    main()
