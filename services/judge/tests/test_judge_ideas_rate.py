"""The ideas rating page (`clipper-judge rate-ideas`): loading the runs, ratings with the answer's
request sha, the per-run "missed" note, and the local server (page, state, video ranges, posts)."""

import json
import re
import threading
import urllib.error
import urllib.request

import pytest

from clipper_judge import ideas_rate
from clipper_judge.ideas_rate import RateError, RatingStore


def idea(n: int, request: str = "") -> dict:
    return {
        "n": n,
        "request": request,
        "kind": "moment",
        "title": f"idea {n}",
        "angle": "a",
        "why": "w",
        "hook_text": "",
        "needs_visual": False,
        "visual_check": "",
        "censor": "",
        "confidence": 0.5,
        "seconds": 20.0,
        "beats": [{"role": "hook", "from": "0:01:00", "to": "0:01:02", "what": "x", "seconds": 2}],
        "issues": [],
    }


def make_runs(tmp_path, sha: str = "sha-1"):
    v2 = tmp_path / "7" / "ideas-v2"
    (v2 / "previews").mkdir(parents=True)
    (v2 / "previews" / "01-moment.mp4").write_bytes(bytes(range(256)) * 4)
    (v2 / "ideas.json").write_text(
        json.dumps(
            {
                "format": "clipper-ideas-v1",
                "prompt": "ideas-v2",
                "meta": {"request_sha": sha, "model": "m", "cost_usd": 1.0},
                "stream": {"id": 7, "channel": "c", "started_at": "2026-11-01T12:00:00+00:00"},
                "requests": {"R1": {"case_id": "42", "note": "make fun of it"}},
                "previews": {
                    "1": {"file": "previews/01-moment.mp4", "seconds": 18.0, "planned_s": 20.0}
                },
                "summary": "s",
                "ideas": [idea(1, "R1"), idea(2)],
            }
        ),
        encoding="utf-8",
    )
    v1 = tmp_path / "7" / "ideas-v1"
    (v1 / "previews").mkdir(parents=True)
    (v1 / "previews" / "01-story.mp4").write_bytes(b"x")
    # the first trial's file: no format, no previews (found by name)
    (v1 / "ideas.json").write_text(json.dumps({"meta": {}, "ideas": [idea(1)]}), encoding="utf-8")
    (tmp_path / "notes").mkdir()  # not a stream: ignored
    return tmp_path


def test_store_state_and_ratings(tmp_path):
    store = RatingStore(make_runs(tmp_path))
    state = store.state()
    assert [s["key"] for s in state["sets"]] == ["7/ideas-v2", "7/ideas-v1"]  # newest first
    first, second, old = state["ideas"]
    assert first["key"] == "7/ideas-v2/1" and first["request_note"] == "make fun of it"
    assert first["request_case"] == "42"
    assert first["preview"] == {
        "url": "/video/7/ideas-v2/previews/01-moment.mp4",
        "seconds": 18.0,
        "planned_s": 20.0,
    }
    assert second["preview"] is None and second["request_note"] is None
    assert old["preview"]["url"] == "/video/7/ideas-v1/previews/01-story.mp4"
    saved = store.rate("7/ideas-v2/1", "publish", " good ")
    assert saved["rating"] == "publish" and saved["note"] == "good"
    assert saved["request_sha"] == "sha-1" and saved["request"] == "R1"
    assert store.counts() == {"ideas": 3, "rated": 1}
    doc = json.loads((tmp_path / "ratings.json").read_text(encoding="utf-8"))
    assert doc["format"] == "clipper-idea-ratings-v1" and "7/ideas-v2/1" in doc["ratings"]
    assert store.rate("7/ideas-v2/2", "", "only a note")["rating"] is None
    assert store.rate("7/ideas-v2/2", "", "") is None  # cleared
    with pytest.raises(RateError, match="rating must be"):
        store.rate("7/ideas-v2/1", "maybe", "")
    for bad in ("7/ideas-v2/9", "8/ideas-v2/1", "7/v2/1", "../x"):
        with pytest.raises(RateError, match="unknown idea"):
            store.rate(bad, "no", "")
    assert store.set_missed("7/ideas-v2", "the bridge jump")["text"] == "the bridge jump"
    with pytest.raises(RateError, match="unknown"):
        store.set_missed("7/ideas-v9", "x")
    # a restarted page keeps them; a rerun (new request sha) marks the rating as older
    again = RatingStore(make_runs(tmp_path / "copy", sha="sha-2"))
    (tmp_path / "copy" / "ratings.json").write_text(
        (tmp_path / "ratings.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    again = RatingStore(tmp_path / "copy")
    rated = again.state()["ideas"][0]
    assert rated["rating"]["rating"] == "publish" and rated["stale"]
    assert again.state()["sets"][0]["missed"] == "the bridge jump"


def test_store_needs_runs(tmp_path):
    with pytest.raises(RateError, match=re.escape("no */ideas-v*/ideas.json")):
        RatingStore(tmp_path)
    make_runs(tmp_path)
    (tmp_path / "ratings.json").write_text('{"labels": {}}', encoding="utf-8")
    with pytest.raises(RateError, match="not a ratings file"):
        RatingStore(tmp_path)


def test_the_server(tmp_path):
    store = RatingStore(make_runs(tmp_path))
    server = ideas_rate.ThreadingHTTPServer(("127.0.0.1", 0), ideas_rate.make_handler(store))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def post(path, body):
        req = urllib.request.Request(
            base + path, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json"},
        )  # fmt: skip
        with urllib.request.urlopen(req) as r:
            return json.load(r)

    try:
        with urllib.request.urlopen(base + "/") as r:
            assert b"Clipper ideas" in r.read()
        with urllib.request.urlopen(base + "/api/state") as r:
            assert len(json.load(r)["ideas"]) == 3
        video = base + "/video/7/ideas-v2/previews/01-moment.mp4"
        req = urllib.request.Request(video, headers={"Range": "bytes=10-19"})
        with urllib.request.urlopen(req) as r:
            assert r.status == 206 and r.read() == bytes(range(10, 20))
        for bad in (
            "/video/7/ideas-v2/../ideas.json",
            "/video/7/ideas-v2/previews/../../ideas.json",
            "/video/8/ideas-v2/previews/01-moment.mp4",
            "/video/7/ideas-v2/previews/02-moment.mp4",  # a valid name, no file
            "/ratings.json",
        ):
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + bad)
            assert e.value.code == 404, bad
        got = post("/api/rate", {"key": "7/ideas-v2/1", "rating": "changes", "note": "shorter"})
        assert got["rating"]["rating"] == "changes" and got["rated"] == 1
        assert post("/api/missed", {"set": "7/ideas-v2", "text": "x"})["missed"]["text"] == "x"
        with pytest.raises(urllib.error.HTTPError) as e:
            post("/api/rate", {"key": "7/ideas-v2/1", "rating": "maybe"})
        assert e.value.code == 400
        with pytest.raises(urllib.error.HTTPError) as e:
            post("/api/rate", {"key": "7/ideas-v2/1", "note": "x" * 70000})
        assert e.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
