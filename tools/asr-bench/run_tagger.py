"""Run one AudioSet sound classifier over the benchmark chunks (inside the bench image).

    python run_tagger.py ast | ced

Scores every second of every chunk: windows start each second; a second's score is the max of
the windows covering it. Each model gets its native window: AST was trained on 10 s clips and
pads anything shorter (which dilutes its scores), CED takes any length (2 s windows).
Groups of AudioSet labels (laughter, screaming, ...) take the max over their labels.
Writes /data/results/tag-<name>.json.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from run_asr import GpuMemory

DATA = Path("/data")
SR = 16000
HOP_S = 1

GROUPS = {
    "laughter": [
        "Laughter",
        "Baby laughter",
        "Giggle",
        "Snicker",
        "Belly laugh",
        "Chuckle, chortle",
    ],
    "screaming": ["Screaming"],
    "shouting": ["Shout", "Yell", "Battle cry", "Children shouting"],
    "cheering": ["Cheering", "Applause", "Crowd"],
    "gasp": ["Gasp"],
    "crying": ["Crying, sobbing", "Whimper"],
    "music": ["Music"],
    "singing": ["Singing"],
    "speech": ["Speech"],
    "gunfire_explosion": ["Gunshot, gunfire", "Explosion", "Burst, pop"],
}

MODELS = {  # name -> (Hugging Face repo, window seconds)
    "ast": ("MIT/ast-finetuned-audioset-10-10-0.4593", 10),
    "ced": ("mispeech/ced-base", 2),
}


def load(name: str):
    from transformers import AutoFeatureExtractor, AutoModelForAudioClassification

    remote = name == "ced"  # CED ships its own model code on the Hub
    repo = MODELS[name][0]
    fe = AutoFeatureExtractor.from_pretrained(repo, trust_remote_code=remote)
    model = AutoModelForAudioClassification.from_pretrained(repo, trust_remote_code=remote)
    model = model.to("cuda").eval().half()
    labels = model.config.id2label

    def scores(windows: np.ndarray) -> np.ndarray:
        """[n, samples] float32 audio -> [n, classes] probabilities."""
        if name == "ast":
            inputs = fe(list(windows), sampling_rate=SR, return_tensors="pt")
        else:
            inputs = fe(torch.from_numpy(windows), sampling_rate=SR, return_tensors="pt")
        inputs = {
            k: v.to("cuda").half() if v.is_floating_point() else v.to("cuda")
            for k, v in inputs.items()
        }
        with torch.inference_mode():
            out = model(**inputs).logits.float()
        # CED's model code already ends in a sigmoid (its "logits" are probabilities); AST's don't.
        return (out if name == "ced" else torch.sigmoid(out)).cpu().numpy()

    return scores, labels


def _window(audio: np.ndarray, start_s: int, window_s: int) -> np.ndarray:
    w = audio[start_s * SR : (start_s + window_s) * SR]
    return np.pad(w, (0, window_s * SR - len(w)))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("name", choices=list(MODELS))
    args = p.parse_args()
    chunks = json.loads((DATA / "manifest.json").read_text())["chunks"]

    mem = GpuMemory()
    t0 = time.perf_counter()
    scores, id2label = load(args.name)
    window_s = MODELS[args.name][1]
    load_s = time.perf_counter() - t0
    index = {label: int(i) for i, label in id2label.items()}
    groups = {
        g: [index[label] for label in labels if label in index] for g, labels in GROUPS.items()
    }

    def run(chunk: dict) -> dict:
        audio, sr = sf.read(DATA / "audio" / chunk["wav"], dtype="float32")
        assert sr == SR
        seconds = int(len(audio) // SR)
        starts = range(0, max(seconds - window_s + 1, 1), HOP_S)
        windows = np.stack([_window(audio, s, window_s) for s in starts])
        probs = np.concatenate([scores(windows[i : i + 32]) for i in range(0, len(windows), 32)])
        per_second = np.zeros((seconds, probs.shape[1]), dtype=np.float32)
        for w, s in enumerate(starts):
            for sec in range(s, min(s + window_s, seconds)):
                per_second[sec] = np.maximum(per_second[sec], probs[w])
        top = [
            [(id2label[int(i)], round(float(row[i]), 3)) for i in np.argsort(row)[::-1][:3]]
            for row in per_second
        ]
        return {
            "id": chunk["id"],
            "groups": {
                g: [round(float(v), 3) for v in per_second[:, ids].max(axis=1)]
                for g, ids in groups.items()
                if ids
            },
            "top": top,
        }

    run(chunks[0])  # warm-up
    results, wall = [], 0.0
    for c in chunks:
        t = time.perf_counter()
        results.append(run(c))
        torch.cuda.synchronize()
        dt = time.perf_counter() - t
        results[-1]["wall_s"] = round(dt, 4)
        wall += dt
        print(f"{c['id']}: {dt:.2f} s", flush=True)
    mem.stop()
    audio_s = sum(c["seconds"] for c in chunks)
    out = {
        "name": args.name,
        "model": MODELS[args.name][0],
        "window_s": window_s,
        "gpu": mem.gpu,
        "gpu_busy_before_pct": mem.busy_before,
        "load_s": round(load_s, 2),
        "vram_peak_mb": mem.above_baseline(mem.peak),
        "audio_s": round(audio_s, 2),
        "wall_s": round(wall, 3),
        "rtfx": round(audio_s / wall, 1),
        "missing_labels": [lbl for ls in GROUPS.values() for lbl in ls if lbl not in index],
        "chunks": results,
    }
    (DATA / "results").mkdir(exist_ok=True)
    (DATA / "results" / f"tag-{args.name}.json").write_text(json.dumps(out))
    print(f"{args.name}: {out['rtfx']}x realtime, peak {out['vram_peak_mb']} MB")


if __name__ == "__main__":
    main()
