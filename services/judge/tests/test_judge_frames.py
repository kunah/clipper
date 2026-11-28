"""Stills of the video for the judge: frame times, the grabber, requests with images (prompts with
a `$frames` section) and how the CLI adapter sends them."""

from __future__ import annotations

import base64
import itertools
import json
import shutil
import subprocess
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from clipper_core.timeline import Interval
from clipper_judge.claude_cli import stdin_bytes
from clipper_judge.frames import FrameGrabber, frame_times, still_command
from clipper_judge.inputs import Frame
from clipper_judge.label import ChunkVideo
from clipper_judge.params import JudgeParams
from clipper_judge.prompt import build_request, load_prompt, request_hash, retry_request
from clipper_judge.samples import sample_inputs

FFMPEG = shutil.which("ffmpeg")


def with_frames(offsets_s, data=b"\xff\xd8jpeg"):
    inputs = sample_inputs()
    t = inputs.anchor
    frames = tuple(Frame(t + timedelta(seconds=s), data + str(s).encode()) for s in offsets_s)
    return replace(inputs, frames=frames)


# --------------------------------------------------------------------------- requests


def test_prompts_without_a_frames_section_ignore_frames():
    plain = build_request(sample_inputs())
    framed = build_request(with_frames([-10, 3]))
    assert not load_prompt("v1").frames
    assert framed == plain  # same text, no images
    assert "images" not in framed.as_dict()
    assert request_hash("m", framed) == request_hash("m", plain)


def test_v2_sends_the_stills_in_time_order_with_matching_labels():
    inputs = with_frames([3, -10, 500])  # +500 s lies outside the context: left out
    req = build_request(inputs, prompt="v2")
    assert load_prompt("v2").frames
    assert [im.label for im in req.images] == ["F1 (-10.0 s)", "F2 (+3.0 s)"]
    assert "## Frames" in req.user
    assert "F1 at -10.0 s\nF2 at +3.0 s" in req.user
    assert [im["label"] for im in req.as_dict()["images"]] == ["F1 (-10.0 s)", "F2 (+3.0 s)"]
    assert all(len(im["sha256"]) == 64 for im in req.as_dict()["images"])
    # the hash covers the images' bytes
    other = build_request(with_frames([3, -10], data=b"\xff\xd8other"), prompt="v2")
    assert other.user == req.user
    assert request_hash("m", other) != request_hash("m", req)
    # the retry keeps them
    assert retry_request(req, "bad").images == req.images


def test_v2_without_stills_says_so_and_sends_no_images():
    req = build_request(sample_inputs(), prompt="v2")
    assert req.images == ()
    assert "none (no stills of the video for this context)" in req.user
    assert load_prompt("v2").version != load_prompt("v1").version


# --------------------------------------------------------------------------- the CLI's stdin


def test_stdin_is_the_text_without_images_and_stream_json_with_them():
    plain = build_request(sample_inputs(), prompt="v2")
    assert stdin_bytes(plain) == plain.user.encode("utf-8")
    req = build_request(with_frames([-4, 2]), prompt="v2")
    raw = stdin_bytes(req)
    assert raw.endswith(b"\n") and raw.count(b"\n") == 1  # one JSON line
    message = json.loads(raw)
    assert message["type"] == "user" and message["message"]["role"] == "user"
    content = message["message"]["content"]
    assert content[0] == {"type": "text", "text": req.user}
    assert [c["type"] for c in content[1:]] == ["text", "image", "text", "image"]
    assert content[1]["text"] == "F1 (-4.0 s):"
    assert content[2]["source"]["media_type"] == "image/jpeg"
    assert base64.b64decode(content[2]["source"]["data"]) == req.images[0].data


async def test_the_adapter_sends_images_as_stream_json(fake_claude):
    model = fake_claude.model()
    req = build_request(with_frames([-4, 2]), prompt="v2")
    await model.judge(req)
    (call,) = fake_claude.calls()
    argv = call["argv"]
    assert argv[argv.index("--input-format") + 1] == "stream-json"
    content = json.loads(call["stdin"])["message"]["content"]
    assert content[0]["text"] == req.user
    assert sum(c["type"] == "image" for c in content) == 2


async def test_no_input_format_flag_without_images(fake_claude):
    model = fake_claude.model()
    await model.judge(build_request(sample_inputs(), prompt="v2"))
    (call,) = fake_claude.calls()
    assert "--input-format" not in call["argv"]


# --------------------------------------------------------------------------- frame times


def test_frame_times_spread_over_the_core_where_there_is_video():
    inputs = sample_inputs()
    params = JudgeParams()
    times = frame_times(inputs, params)
    c = inputs.candidate
    pad = timedelta(seconds=params.core_pad_s)
    assert len(times) == params.frames_n
    assert times == sorted(times)
    assert c.moment_from - pad < times[0] and times[-1] < c.moment_to + pad
    gaps = {round((b - a).total_seconds(), 6) for a, b in itertools.pairwise(times)}
    assert len(gaps) == 1  # evenly spaced
    assert all(any(v.start <= t < v.end for v in inputs.video) for t in times)


def test_frame_times_fewer_for_a_short_core_none_when_off_or_without_video():
    inputs = sample_inputs()
    short = frame_times(inputs, JudgeParams(core_pad_s=0.0, frame_min_gap_s=4.0))
    assert len(short) == 2  # a 10 s moment, 4 s apart
    assert frame_times(inputs, JudgeParams(frames_n=0)) == []
    no_video = replace(inputs, video=(Interval(inputs.context.start, inputs.context.start),))
    assert frame_times(no_video) == []


def test_still_command_seeks_early_and_trims_by_timestamp():
    cmd = still_command("ffmpeg", "chunk.ts", 12.5, 640)
    assert cmd[cmd.index("-ss") + 1] == "7.500"
    graph = cmd[cmd.index("-vf") + 1]
    assert graph.startswith("trim=start=5.000,") and graph.endswith("scale=640:-2")
    near_start = still_command("ffmpeg", "chunk.ts", 2.0, 640)
    assert near_start[near_start.index("-ss") + 1] == "0.000"
    assert "trim=start=2.000," in near_start[near_start.index("-vf") + 1]


# --------------------------------------------------------------------------- the grabber


class _Store:
    def __init__(self, files: dict[str, Path]) -> None:
        self.files = files
        self.reads: list[str] = []

    def get_bytes(self, key: str) -> bytes:
        self.reads.append(key)
        return self.files[key].read_bytes()


@pytest.mark.skipif(FFMPEG is None, reason="needs ffmpeg on PATH")
def test_the_grabber_takes_jpeg_stills_from_the_chunks(tmp_path):
    src = tmp_path / "src.ts"
    subprocess.run(
        [
            FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30",
            "-t", "20", "-c:v", "libx264", "-preset", "ultrafast", "-g", "60",
            "-f", "mpegts", str(src),
        ],
        check=True,
    )  # fmt: skip
    inputs = sample_inputs()
    start = inputs.anchor - timedelta(seconds=10)
    chunk = ChunkVideo(start, start + timedelta(seconds=20), "chunks/a.ts")
    store = _Store({"chunks/a.ts": src})
    grabber = FrameGrabber(store, tmp_path / "work", ffmpeg=FFMPEG, width=160)
    times = [start + timedelta(seconds=s) for s in (1.0, 9.5, 18.0, 25.0)]  # 25 s: no chunk
    stills = grabber.grab([chunk], times)
    assert [f.at_utc for f in stills] == times[:3]
    assert all(f.data[:2] == b"\xff\xd8" and f.media_type == "image/jpeg" for f in stills)
    assert len({f.data for f in stills}) == 3  # different pictures
    assert store.reads == ["chunks/a.ts"]  # downloaded once
    assert (grabber.grabbed, grabber.failed) == (3, 0)
    grabber.close()
    assert not any((tmp_path / "work" / "chunks").iterdir())
