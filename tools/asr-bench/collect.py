"""Collect benchmark audio: N minutes of audio_only from live channels, cut into 60 s chunks.

    uv run python tools/asr-bench/collect.py caseoh_ hasanabi --minutes 4

Writes data/bench/audio/{channel}_{nn}.aac (what the transcriber gets from the recorder) and a
16 kHz mono .wav next to each (what the models read), plus manifest.json. Runs natively.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

OUT = Path("data/bench")


def record(channel: str, seconds: int, raw: Path) -> subprocess.Popen:
    cmd = [
        sys.executable,
        "-m",
        "streamlink",
        "--hls-duration",
        str(seconds),
        "--force",
        "-o",
        str(raw),
        f"https://www.twitch.tv/{channel}",
        "audio_only",
    ]
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)


def split(channel: str, raw: Path, audio_dir: Path) -> list[Path]:
    pattern = audio_dir / f"{channel}_%02d.aac"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(raw),
            "-map",
            "0:a:0",
            "-c",
            "copy",
            "-f",
            "segment",
            "-segment_time",
            "60",
            "-segment_format",
            "adts",
            "-reset_timestamps",
            "1",
            str(pattern),
        ],
        check=True,
    )
    chunks = sorted(audio_dir.glob(f"{channel}_*.aac"))
    for aac in chunks:
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(aac),
                "-ar",
                "16000",
                "-ac",
                "1",
                str(aac.with_suffix(".wav")),
            ],
            check=True,
        )
    return chunks


def duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(out.stdout.strip())


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("channels", nargs="+")
    p.add_argument("--minutes", type=float, default=4)
    args = p.parse_args()
    raw_dir, audio_dir = OUT / "raw", OUT / "audio"
    raw_dir.mkdir(parents=True, exist_ok=True)
    audio_dir.mkdir(parents=True, exist_ok=True)
    started = datetime.now(UTC).isoformat()
    seconds = int(args.minutes * 60) + 5
    procs = {c: record(c, seconds, raw_dir / f"{c}.media") for c in args.channels}
    manifest = {"collected_at": started, "chunks": []}
    for channel, proc in procs.items():
        _, err = proc.communicate()
        raw = raw_dir / f"{channel}.media"
        if not raw.exists() or raw.stat().st_size == 0:
            print(f"{channel}: nothing recorded ({err.strip()[-200:]})")
            continue
        for aac in split(channel, raw, audio_dir):
            wav = aac.with_suffix(".wav")
            seconds_ = duration(wav)
            if seconds_ < 20:  # drop the short tail
                aac.unlink()
                wav.unlink()
                continue
            manifest["chunks"].append(
                {
                    "id": aac.stem,
                    "channel": channel,
                    "aac": aac.name,
                    "wav": wav.name,
                    "seconds": round(seconds_, 3),
                }
            )
        print(f"{channel}: {sum(1 for c in manifest['chunks'] if c['channel'] == channel)} chunks")
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    total = sum(c["seconds"] for c in manifest["chunks"])
    print(f"total: {len(manifest['chunks'])} chunks, {total / 60:.1f} min")


if __name__ == "__main__":
    main()
