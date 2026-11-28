"""`clipper-judge label` (DESIGN §19, decided 2026-11-25): the blind labelling set, its page, and
the eval over labelled drafts."""

import io
import json
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from clipper_core.db import create_engine, session_factory
from clipper_core.storage import keys
from clipper_core.timeline import Interval
from clipper_judge import evaluate, label
from clipper_judge.evalset import EvalSetError, load_drafts, load_labels
from clipper_judge.fakes import FakeJudgeModel
from clipper_judge.label import (
    ChunkVideo,
    LabelError,
    LabelStore,
    Piece,
    case_key,
    pick_drafts,
    piece_command,
    plan_pieces,
    spread,
)
from clipper_judge.main import parse_args
from clipper_judge.reads import CandidateInfo
from clipper_judge.settings import JudgeSettings

T = datetime(2026, 11, 23, 15, 0, tzinfo=UTC)


def at(s: float) -> datetime:
    return T + timedelta(seconds=s)


def cand(stream_id: int = 1, peak: float = 0.0) -> CandidateInfo:
    return CandidateInfo(
        id=0,
        stream_id=stream_id,
        channel_id=1,
        source="live",
        status="suppressed",
        trigger="chat",
        peak_utc=at(peak),
        chat_delay_s=8.0,
        delay_source="default",
        moment_from=at(peak - 6),
        moment_to=at(peak + 4),
        window_from=at(peak - 90),
        window_to=at(peak + 30),
        score=4.2,
        moment_type="mixed",
    )


# --------------------------------------------------------------------------- pure parts


def test_pieces_follow_the_chunks_and_jump_over_gaps():
    chunks = [
        ChunkVideo(at(-120), at(-60), "a"),
        ChunkVideo(at(-60), at(0.2), "b"),  # jitter: overlaps the next chunk by 0.2 s
        ChunkVideo(at(0), at(60), "c"),
        ChunkVideo(at(80), at(140), "d"),  # after a 20 s gap (an ad)
        ChunkVideo(at(140), at(140.3), "e"),  # a sliver: left out
    ]
    pieces = plan_pieces(Interval(at(-90), at(100)), chunks)
    assert [(p.chunk_key, p.offset_s, p.duration_s) for p in pieces] == [
        ("a", 30.0, 30.0),
        ("b", 0.0, 60.2),
        ("c", 0.2, 59.8),  # starts where b ended: no overlap
        ("d", 0.0, 20.0),
    ]
    assert [p.video_s for p in pieces] == [0.0, 30.0, 90.2, 150.0]
    assert pieces[3].start == at(80)  # the video jumps over the gap
    assert pieces[0].as_dict() == {"v": 0.0, "u": at(-90).isoformat(), "d": 30.0}
    assert plan_pieces(Interval(at(500), at(600)), chunks) == []


def test_spread_and_draft_picking():
    assert spread(list(range(10)), 3) == [0, 3, 6]
    assert spread([1, 2], 5) == [1, 2] and spread([1, 2], 0) == []
    drafts = [
        ("d1-a", cand(1, 50.0), {}),  # 50 s from the candidate at 0: too close
        ("d1-b", cand(1, 200.0), {}),
        ("d1-c", cand(1, 250.0), {}),  # 50 s from d1-b: too close
        ("d1-d", cand(1, 400.0), {}),
        ("d2-a", cand(2, 30.0), {}),  # another stream: the candidate doesn't count there
    ]
    picked = pick_drafts(drafts, [cand(1, 0.0)], max_drafts=10, min_gap_s=90)
    assert [d[0] for d in picked] == ["d1-b", "d1-d", "d2-a"]
    assert len(pick_drafts(drafts, [], max_drafts=2, min_gap_s=90)) == 2
    assert case_key("297") == case_key("297") != case_key("298")
    assert case_key("d2415-1795445048").startswith("k") and len(case_key("x")) == 9


def test_arguments():
    args = parse_args(["label", "prepare", "--out", "x", "--stream", "1", "2"])
    assert args.cmd == "label" and args.label_cmd == "prepare" and args.stream == [1, 2]
    assert args.max_drafts == 25 and args.min_gap_s == 90.0 and args.encoder == "auto"
    with pytest.raises(SystemExit):
        parse_args(["label", "prepare", "--out", "x"])  # no cases chosen
    args = parse_args(["label", "serve", "somewhere", "--port", "9000"])
    assert args.dir == Path("somewhere") and args.port == 9000 and not args.open


# --------------------------------------------------------------------------- the page


def make_set(tmp_path: Path) -> Path:
    d = tmp_path / "set"
    (d / "video").mkdir(parents=True)
    cases = []
    for case_id, kind in (("297", "candidate"), ("d1-1795445048", "draft")):
        key = case_key(case_id)
        (d / "video" / f"{key}.mp4").write_bytes(bytes(range(256)) * 4)  # 1 KiB
        cases.append(
            {
                "key": key,
                "case_id": case_id,
                "kind": kind,
                "stream_id": 1,
                "channel": "ohnepixel",
                "title": "GTA 6",
                "category": "Grand Theft Auto VI",
                "anchor_utc": at(0).isoformat(),
                "moment": [at(-6).isoformat(), at(4).isoformat()],
                "context": [at(-90).isoformat(), at(30).isoformat()],
                "video": f"video/{key}.mp4",
                "duration_s": 120.0,
                "segments": [{"v": 0.0, "u": at(-90).isoformat(), "d": 120.0}],
            }
        )
    (d / "cases.json").write_text(
        json.dumps({"format": label.SET_FORMAT, "version": 1, "cases": cases}), encoding="utf-8"
    )
    return d


def test_the_label_store_is_blind_and_writes_evalset_labels(tmp_path):
    d = make_set(tmp_path)
    store = LabelStore(d)
    public = store.public()["cases"]
    assert len(public) == 2
    for c in public:  # nothing that tells a candidate from a draft
        assert not {"case_id", "kind", "stream_id"} & set(c) and c["label"] is None
    k1, k2 = (case_key("297"), case_key("d1-1795445048"))
    got = store.set(k1, {"verdict": "clip", "in_s": -12.5, "out_s": 6.0, "note": "the fall"})
    assert got["verdict"] == "clip" and got["in_s"] == -12.5 and "at" in got
    store.set(k2, {"verdict": "reject"})
    labels = load_labels(d / "labels.json")  # what the eval reads
    assert labels["297"].in_s == -12.5 and labels["297"].note == "the fall"
    assert labels["d1-1795445048"].verdict == "reject"
    with pytest.raises(EvalSetError, match="in_s must be before out_s"):
        store.set(k1, {"verdict": "clip", "in_s": 5, "out_s": 1})
    with pytest.raises(EvalSetError, match="go together"):
        store.set(k1, {"verdict": "clip", "in_s": 5})
    with pytest.raises(LabelError, match="unknown case"):
        store.set("k00000000", {"verdict": "clip"})
    assert store.set(k2, {"verdict": None}) is None  # unlabelled again
    assert set(load_labels(d / "labels.json")) == {"297"}
    # a restarted page sees the labels so far
    again = LabelStore(d)
    assert [c["label"] and c["label"]["verdict"] for c in again.public()["cases"]].count(
        "clip"
    ) == 1
    with pytest.raises(LabelError, match="no label set"):
        LabelStore(tmp_path)


def test_the_page_server_serves_ranges_and_saves_labels(tmp_path):
    d = make_set(tmp_path)
    store = LabelStore(d)
    server = label.ThreadingHTTPServer(("127.0.0.1", 0), label.make_handler(store))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    key = case_key("297")
    try:
        with urllib.request.urlopen(base + "/") as r:
            assert b"Clipper labels" in r.read()
        with urllib.request.urlopen(base + "/api/cases") as r:
            assert [c["key"] for c in json.load(r)["cases"]] == [c["key"] for c in store.cases]
        req = urllib.request.Request(base + f"/video/{key}.mp4", headers={"Range": "bytes=10-19"})
        with urllib.request.urlopen(req) as r:
            assert r.status == 206 and r.headers["Content-Range"] == "bytes 10-19/1024"
            assert r.read() == bytes(range(10, 20))
        req = urllib.request.Request(base + f"/video/{key}.mp4", headers={"Range": "bytes=-4"})
        with urllib.request.urlopen(req) as r:
            assert r.read() == bytes(range(252, 256))
        with urllib.request.urlopen(base + f"/video/{key}.mp4") as r:
            assert r.status == 200 and r.headers["Accept-Ranges"] == "bytes"
            assert len(r.read()) == 1024
        for bad in ("/video/../cases.json", "/video/k00000000.mp4", "/labels.json"):
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + bad)
            assert e.value.code == 404
        post = urllib.request.Request(
            base + "/api/label",
            data=json.dumps({"key": key, "verdict": "clip", "in_s": -3, "out_s": 9}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(post) as r:
            assert json.load(r)["label"]["out_s"] == 9.0
        assert load_labels(d / "labels.json")["297"].out_s == 9.0
        post = urllib.request.Request(
            base + "/api/label",
            data=json.dumps({"key": key, "verdict": "maybe"}).encode(),
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(post)
        assert e.value.code == 400
    finally:
        server.shutdown()
        server.server_close()


# --------------------------------------------------------------------------- prepare + eval

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


def test_chunk_video(path: Path) -> None:
    """60 s of a tiny test picture with a tone, as MPEG-TS like a recorded chunk."""
    subprocess.run(
        [
            FFMPEG, "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=160x90:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
            "-t", "60", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
            "-f", "mpegts", str(path),
        ],
        check=True,
    )  # fmt: skip


test_chunk_video.__test__ = False  # a helper, not a test


def duration(path: Path) -> float:
    probe = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return float(probe.stdout)


def draft_doc(world, *peaks: float) -> dict:
    out = []
    for p in peaks:
        out.append(
            {
                "id": f"d{world.stream_id}-{int(world.at(p).timestamp())}",
                "stream_id": world.stream_id,
                "channel_id": world.channel_id,
                "source": "live",
                "status": "suppressed",
                "suppress_reason": "weak",
                "trigger": "chat",
                "peak_utc": world.at(p).isoformat(),
                "chat_peak_utc": world.at(p + 8).isoformat(),
                "chat_delay_s": 8.0,
                "delay_source": "default",
                "moment_from": world.at(p - 6).isoformat(),
                "moment_to": world.at(p + 4).isoformat(),
                "window_from": world.at(p - 90).isoformat(),
                "window_to": world.at(p + 30).isoformat(),
                "score": 3.1,
                "moment_type": "mixed",
                "features": {},
                "detector_version": "d1-test",
            }
        )
    return {"format": "clipper-detector-drafts", "version": 1, "drafts": out}


@pytest.fixture
async def read_only(settings):
    engines = []

    def make():
        engine = create_engine(settings, connect_args=evaluate.READ_ONLY)
        engines.append(engine)
        return session_factory(engine)

    yield make
    for engine in engines:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.skipif(FFMPEG is None or FFPROBE is None, reason="needs ffmpeg on PATH")
async def test_prepare_cuts_the_videos_and_the_eval_runs_the_drafts(
    world, read_only, tmp_path, monkeypatch, capsys
):
    await world.transcribe_range(-480, 180)
    first = await world.candidate()
    chunk = tmp_path / "chunk.ts"
    test_chunk_video(chunk)
    for c in world.chunks:
        world.store.objects[keys.chunk_video(world.stream_id, c.seq)] = chunk.read_bytes()
    drafts_file = tmp_path / "drafts.json"
    # -300: far from the candidate; +30: too close to it (left out)
    drafts_file.write_text(json.dumps(draft_doc(world, -300.0, 30.0)), encoding="utf-8")
    monkeypatch.setattr(label, "HEIGHT", 90)  # small and quick
    out = tmp_path / "set"
    args = parse_args(
        ["label", "prepare", "--out", str(out), "--candidate", str(first),
         "--drafts", str(drafts_file), "--encoder", "libx264", "--ffmpeg", FFMPEG]
    )  # fmt: skip
    assert await label.prepare(args, read_only(), world.store, now=world.clock.now) == 0
    doc = json.loads((out / "cases.json").read_text(encoding="utf-8"))
    by_id = {c["case_id"]: c for c in doc["cases"]}
    draft_id = f"d{world.stream_id}-{int(world.at(-300).timestamp())}"
    assert set(by_id) == {str(first), draft_id}
    one = by_id[str(first)]
    # the window [-90, +30] over the 60 s chunks: three pieces, 120 s
    assert [s["d"] for s in one["segments"]] == [30.0, 60.0, 30.0]
    assert one["duration_s"] == 120.0 and one["kind"] == "candidate"
    assert duration(out / one["video"]) == pytest.approx(120.0, abs=0.3)
    assert [d.id for d in load_drafts(out / "drafts.json")] == [draft_id]
    assert "2 cases (1 candidates, 1 drafts)" in capsys.readouterr().out
    # a rerun keeps the videos
    stamp = (out / one["video"]).stat().st_mtime_ns
    assert await label.prepare(args, read_only(), world.store, now=world.clock.now) == 0
    assert (out / one["video"]).stat().st_mtime_ns == stamp
    assert "0 videos cut, 2 kept" in capsys.readouterr().out

    # labels on both, then the eval over the labelled candidate and draft (read-only)
    store = LabelStore(out)
    store.set(case_key(str(first)), {"verdict": "clip"})
    store.set(case_key(draft_id), {"verdict": "reject"})
    report = tmp_path / "eval.json"
    eval_args = evaluate.parse_args(
        ["--labels", str(out / "labels.json"), "--drafts", str(out / "drafts.json"),
         "--report", str(report)]
    )  # fmt: skip
    code = await evaluate.run_eval(
        eval_args,
        settings=JudgeSettings(_env_file=None),
        model=FakeJudgeModel(),
        sessions=read_only(),
        store=evaluate._ReadOnlyStore(world.store),
        out=io.StringIO(),
        now=world.clock.now,
    )
    assert code == 0
    cases = {c["id"]: c for c in json.loads(report.read_text(encoding="utf-8"))["cases"]}
    assert set(cases) == {str(first), draft_id}
    assert cases[draft_id]["candidate_id"] is None and cases[draft_id]["outcome"] == "clip"
    assert cases[draft_id]["label"]["verdict"] == "reject"
    # a labelled draft needs its drafts file
    with pytest.raises(evaluate.UsageError, match="not in --drafts"):
        await evaluate.run_eval(
            evaluate.parse_args(["--labels", str(out / "labels.json")]),
            settings=JudgeSettings(_env_file=None),
            model=FakeJudgeModel(),
            sessions=read_only(),
            store=evaluate._ReadOnlyStore(world.store),
            now=world.clock.now,
        )


def test_piece_command_fades_only_when_asked():
    piece = Piece(0.0, datetime(2026, 11, 20, tzinfo=UTC), 4.0, "k", 12.0)
    plain = piece_command("ffmpeg", "src.ts", piece, "libx264", "out.mp4")
    assert "afade" not in plain[plain.index("-filter_complex") + 1]
    faded = piece_command(
        "ffmpeg", "src.ts", piece, "libx264", "out.mp4", fade_in_s=0.015, fade_out_s=0.015
    )
    graph = faded[faded.index("-filter_complex") + 1]
    assert graph.endswith(
        "asetpts=N/48000/TB,afade=t=in:st=0:d=0.015,afade=t=out:st=3.985:d=0.015[a]"
    )
