"""Benchmark report: metrics on stdout (markdown) + /data/report.html for listening.

    python report.py          (whisper venv: jiwer, whisper-normalizer, faster-whisper VAD)

No ground-truth transcripts exist for stream audio, so accuracy is shown three ways: public
leaderboard WER (clean benchmark sets), agreement between the models on our audio, and the
listening page (words highlight in time with the audio; differences vs. a chosen model are marked).
"""

from __future__ import annotations

import html
import json
import re
import statistics
from difflib import SequenceMatcher
from pathlib import Path

import jiwer
import numpy as np
import soundfile as sf
from faster_whisper.vad import VadOptions, get_speech_timestamps
from whisper_normalizer.english import EnglishTextNormalizer

DATA = Path("/data")
SR = 16000
AUDIO_HOURS_PER_DAY = 60  # DESIGN.md: 10 channels x 6 h

# Open ASR Leaderboard English average, v1.15.1 (9 Nov 2026); clean benchmark sets, not streams.
PUBLIC = {
    "parakeet-tdt-0.6b-v2": ("0.6B", "CC-BY-4.0", 6.93),
    "Qwen3-ASR-1.7B": ("1.7B + 0.6B aligner", "Apache-2.0", 6.15),
    "Qwen3-ASR-0.6B": ("0.6B + 0.6B aligner", "Apache-2.0", 7.07),
    "large-v3": ("1.55B", "MIT", 8.24),
    "large-v3-turbo": ("0.8B", "MIT", 8.75),
}
PHANTOMS = [
    "thank you for watching",
    "thanks for watching",
    "please subscribe",
    "like and subscribe",
    "see you in the next video",
    "subtitles by",
]

normalizer = EnglishTextNormalizer()


def token(word: str) -> str:
    return re.sub(r"[^a-z0-9']", "", word.lower())


def speech_regions(wav: Path) -> list[tuple[float, float]]:
    audio, _ = sf.read(wav, dtype="float32")
    spans = get_speech_timestamps(audio, VadOptions(speech_pad_ms=200))
    return [(s["start"] / SR, s["end"] / SR) for s in spans]


def outside_speech(words: list[dict], regions: list[tuple[float, float]]) -> int:
    def near(t: float) -> bool:
        return any(a - 0.5 <= t <= b + 0.5 for a, b in regions)

    return sum(1 for w in words if not near((w["s"] + w["e"]) / 2))


def repetition(words: list[dict]) -> bool:
    toks = [t for t in (token(w["w"]) for w in words) if t]
    grams = [" ".join(toks[i : i + 3]) for i in range(len(toks) - 2)]
    return any(grams.count(g) >= 4 for g in set(grams))


def timing_diff_ms(a: list[dict], b: list[dict]) -> float | None:
    """Median |start_a - start_b| over words both models agree on."""
    ta = [(token(w["w"]), w["s"]) for w in a if token(w["w"])]
    tb = [(token(w["w"]), w["s"]) for w in b if token(w["w"])]
    sm = SequenceMatcher(None, [t for t, _ in ta], [t for t, _ in tb], autojunk=False)
    diffs = [
        abs(ta[m.a + k][1] - tb[m.b + k][1])
        for m in sm.get_matching_blocks()
        for k in range(m.size)
    ]
    return statistics.median(diffs) * 1000 if diffs else None


def load(kind: str) -> list[dict]:
    order = list(PUBLIC)
    runs = [json.loads(p.read_text()) for p in sorted((DATA / "results").glob(f"{kind}-*.json"))]
    return sorted(runs, key=lambda r: order.index(r["name"]) if r["name"] in order else 99)


def main() -> None:
    manifest = json.loads((DATA / "manifest.json").read_text())
    chunks = manifest["chunks"]
    asr, tags = load("asr"), load("tag")
    regions = {c["id"]: speech_regions(DATA / "audio" / c["wav"]) for c in chunks}
    speech_s = sum(b - a for r in regions.values() for a, b in r)
    audio_s = sum(c["seconds"] for c in chunks)
    by_id = {r["name"]: {c["id"]: c for c in r["chunks"]} for r in asr}

    lines = [
        f"Audio: {len(chunks)} chunks, {audio_s / 60:.1f} min from "
        f"{len({c['channel'] for c in chunks})} channels; VAD speech {speech_s / audio_s:.0%}",
        "",
        "| model | params | license | leaderboard WER | x realtime | s per 60 s chunk "
        "(median / p95) | GPU-h per day | VRAM peak | load | words/min | outside speech | "
        "repetition chunks | phantom phrases | GPU busy before |",
        "|" + "---|" * 14,
    ]
    summary = []
    for r in asr:
        per60 = [c["wall_s"] / c["seconds"] * 60 for c in r["chunks"]]
        words = [w for c in r["chunks"] for w in c["words"]]
        out = sum(outside_speech(c["words"], regions[c["id"]]) for c in r["chunks"])
        reps = sum(repetition(c["words"]) for c in r["chunks"])
        text = " ".join(c["text"].lower() for c in r["chunks"])
        phantoms = sum(text.count(p) for p in PHANTOMS)
        params, lic, wer = PUBLIC.get(r["name"], ("?", "?", None))
        row = {
            "name": r["name"],
            "params": params,
            "license": lic,
            "public_wer": wer,
            "rtfx": r["rtfx"],
            "median_s": statistics.median(per60),
            "p95_s": float(np.percentile(per60, 95)),
            "gpu_h_day": AUDIO_HOURS_PER_DAY / r["rtfx"],
            "vram": r["vram_peak_mb"],
            "load_s": r["load_s"],
            "wpm": len(words) / (audio_s / 60),
            "outside": out / max(len(words), 1),
            "reps": reps,
            "busy": r.get("gpu_busy_before_pct"),
            "phantoms": phantoms,
            "versions": r["versions"],
            "config": r["config"],
        }
        summary.append(row)
        lines.append(
            f"| {r['name']} | {params} | {lic} | {wer if wer else '?'}% | {r['rtfx']:.0f}x | "
            f"{row['median_s']:.2f} / {row['p95_s']:.2f} | {row['gpu_h_day']:.2f} | "
            f"{r['vram_peak_mb']} MB | {r['load_s']:.0f} s | {row['wpm']:.0f} | "
            f"{row['outside']:.1%} | {reps} | {phantoms} | {r.get('gpu_busy_before_pct', '?')}% |"
        )

    names = [r["name"] for r in asr]
    wer_m, time_m = {}, {}
    for a in names:
        for b in names:
            if a == b:
                continue
            ids = [c["id"] for c in chunks]
            ref = [normalizer(by_id[a][i]["text"]) or "-" for i in ids]
            hyp = [normalizer(by_id[b][i]["text"]) or "-" for i in ids]
            wer_m[(a, b)] = jiwer.wer(ref, hyp)
            diffs = [timing_diff_ms(by_id[a][i]["words"], by_id[b][i]["words"]) for i in ids]
            diffs = [d for d in diffs if d is not None]
            time_m[(a, b)] = statistics.median(diffs) if diffs else None

    def matrix(title: str, m: dict, fmt) -> list[str]:
        out = [
            "",
            title,
            "",
            "| reference \\ compared | " + " | ".join(names) + " |",
            "|" + "---|" * (len(names) + 1),
        ]
        for a in names:
            cells = ["-" if a == b else fmt(m[(a, b)]) for b in names]
            out.append(f"| {a} | " + " | ".join(cells) + " |")
        return out

    lines += matrix(
        "Disagreement (WER of each model against another, normalized text):",
        wer_m,
        lambda v: f"{v:.1%}",
    )
    lines += matrix(
        "Word timing difference (median |start| difference on shared words):",
        time_m,
        lambda v: f"{v:.0f} ms" if v is not None else "?",
    )

    if tags:
        lines += [
            "",
            "| sound classifier | x realtime | s per 60 s chunk | VRAM peak | load |",
            "|---|---|---|---|---|",
        ]
        for t in tags:
            per60 = statistics.median(c["wall_s"] for c in t["chunks"]) / 60 * 60
            lines.append(
                f"| {t['name']} ({t['model']}) | {t['rtfx']:.0f}x | {per60:.2f} | "
                f"{t['vram_peak_mb']} MB | {t['load_s']:.0f} s |"
            )
        for group in ("laughter", "screaming", "shouting", "cheering", "music"):
            for t in tags:
                vals = np.array([v for c in t["chunks"] for v in c["groups"].get(group, [])])
                if len(vals):
                    lines.append(
                        f"- {t['name']} {group}: max {vals.max():.2f}, 99th pct "
                        f"{np.percentile(vals, 99):.2f}, seconds >= 0.3: {(vals >= 0.3).sum()}"
                    )

    print("\n".join(lines))
    write_html(manifest, asr, tags, summary, wer_m, time_m, regions)


def write_html(manifest, asr, tags, summary, wer_m, time_m, regions) -> None:
    data = {
        "chunks": manifest["chunks"],
        "asr": [
            {
                "name": r["name"],
                "chunks": {c["id"]: {"text": c["text"], "words": c["words"]} for c in r["chunks"]},
            }
            for r in asr
        ],
        "tags": [{"name": t["name"], "chunks": {c["id"]: c for c in t["chunks"]}} for t in tags],
        "summary": summary,
        "wer": {f"{a}|{b}": v for (a, b), v in wer_m.items()},
        "timing": {f"{a}|{b}": v for (a, b), v in time_m.items()},
        "speech": regions,
    }
    page = TEMPLATE.replace("__DATA__", json.dumps(data).replace("</", "<\\/"))
    page = page.replace("__TITLE__", html.escape("ASR benchmark"))
    (DATA / "report.html").write_text(page, encoding="utf-8")
    print("\nwrote report.html")


TEMPLATE = (Path(__file__).parent / "report_template.html").read_text(encoding="utf-8")

if __name__ == "__main__":
    main()
