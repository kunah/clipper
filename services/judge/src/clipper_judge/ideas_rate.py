"""A local page to rate the post-stream ideas (`clipper-judge ideas`, DESIGN §15 2026-11-26).

    clipper-judge rate-ideas [--dir data/ideas] [--port 8766] [--open]

Reads every `<dir>/<stream>/<prompt>/ideas.json` (written by `clipper-judge ideas`), serves a page
on 127.0.0.1 only (`ideas_rate_page.html`, package data): each idea's preview (byte ranges for
the player), the editor's request it answers, its angle, beats and checks, and three ratings:
publish, publish with changes, no, plus a note; per stream and prompt a "what did it miss?" note.
Every change rewrites `<dir>/ratings.json` atomically:

    {"format": "clipper-idea-ratings-v1",
     "ratings": {"1412/ideas-v2/3": {"rating": "publish", "note": "", "at": ..., "title": ...,
                                     "request": "R3", "request_sha": ...}},
     "missed": {"1412/ideas-v2": {"text": "...", "at": ...}}}

A rating keeps the request sha of the answer it rated: when a rerun replaces the answer, the
page shows the rating as made on an older answer instead of silently moving it to a new idea.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import threading
import webbrowser
from collections.abc import Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any

from clipper_core.logs import get_logger
from clipper_core.timeline import utc_now
from clipper_judge.ideas import IDEAS_FILE_FORMAT
from clipper_judge.label import send_video

log = get_logger(__name__)

RATINGS_FORMAT = "clipper-idea-ratings-v1"
RATINGS = ("publish", "changes", "no")
DEFAULT_PORT = 8766
MAX_BODY = 64 * 1024
NOTE_CHARS = 4000
_STREAM = re.compile(r"^\d+$")
_PROMPT = re.compile(r"^ideas-v\d+$")
_PREVIEW = re.compile(r"^previews/\d{2}-[a-z_]+\.mp4$")
_KEY = re.compile(r"^(\d+)/(ideas-v\d+)/(\d+)$")
_SET = re.compile(r"^(\d+)/(ideas-v\d+)$")


class RateError(ValueError):
    pass


class RatingStore:
    """The ideas under `directory` and `ratings.json`, rewritten atomically on every change."""

    def __init__(self, directory: Path) -> None:
        self.dir = directory
        self.path = directory / "ratings.json"
        self.lock = threading.Lock()
        self.sets: dict[str, dict[str, Any]] = {}  # "1412/ideas-v2" -> ideas.json
        for path in sorted(directory.glob("*/ideas-v*/ideas.json")):
            stream, prompt = path.parent.parent.name, path.parent.name
            if not (_STREAM.match(stream) and _PROMPT.match(prompt)):
                continue
            doc = _read_json(path)
            if (
                not isinstance(doc, Mapping)
                or not isinstance(doc.get("ideas"), list)
                or doc.get("format", IDEAS_FILE_FORMAT) != IDEAS_FILE_FORMAT  # none: first trial
            ):
                log.warning("not an ideas file: skipped", file=str(path))
                continue
            self.sets[f"{stream}/{prompt}"] = dict(doc)
        if not self.sets:
            raise RateError(
                f"{directory}: no */ideas-v*/ideas.json; run `clipper-judge ideas` first"
            )
        raw = _read_json(self.path) if self.path.exists() else None
        if raw is not None and not (
            isinstance(raw, Mapping) and raw.get("format") == RATINGS_FORMAT
        ):
            raise RateError(f"{self.path}: not a ratings file ({RATINGS_FORMAT})")
        raw = raw or {}
        self.ratings: dict[str, dict[str, Any]] = dict(raw.get("ratings") or {})
        self.missed: dict[str, dict[str, Any]] = dict(raw.get("missed") or {})

    # ------------------------------------------------------------------ reads

    def idea(self, key: str) -> tuple[dict[str, Any], dict[str, Any]]:
        m = _KEY.match(key)
        doc = self.sets.get(f"{m[1]}/{m[2]}") if m else None
        if m is None or doc is None:
            raise RateError(f"unknown idea {key!r}")
        for idea in doc["ideas"]:
            if isinstance(idea, Mapping) and idea.get("n") == int(m[3]):
                return doc, dict(idea)
        raise RateError(f"unknown idea {key!r}")

    def preview_path(self, stream: str, prompt: str, file: str) -> Path | None:
        """A preview file of a known set, by its relative name; None for anything else."""
        if f"{stream}/{prompt}" not in self.sets or not _PREVIEW.match(file):
            return None
        return self.dir / stream / prompt / file

    def state(self) -> dict[str, Any]:
        """Everything the page shows."""
        sets, ideas = [], []
        for set_key, doc in sorted(self.sets.items(), key=lambda kv: _set_order(kv[0])):
            stream, prompt = set_key.split("/")
            meta = doc.get("meta") or {}
            info = doc.get("stream") or {}
            sha = meta.get("request_sha")
            sets.append(
                {
                    "key": set_key,
                    "stream": int(stream),
                    "prompt": prompt,
                    "channel": info.get("channel"),
                    "title": info.get("title"),
                    "category": info.get("category"),
                    "started_at": info.get("started_at"),
                    "summary": doc.get("summary") or "",
                    "model": meta.get("model"),
                    "cost_usd": meta.get("cost_usd"),
                    "missed": (self.missed.get(set_key) or {}).get("text", ""),
                }
            )
            requests = doc.get("requests") or {}
            previews = doc.get("previews") or {}
            for idea in doc["ideas"]:
                if not isinstance(idea, Mapping):
                    continue
                n = idea.get("n")
                key = f"{set_key}/{n}"
                preview = previews.get(str(n)) or self._found_preview(stream, prompt, n)
                request = idea.get("request") or ""
                rating = self.ratings.get(key)
                ideas.append(
                    {
                        **{k: idea.get(k) for k in _IDEA_FIELDS},
                        "key": key,
                        "set": set_key,
                        "request_note": (requests.get(request) or {}).get("note")
                        if request
                        else None,
                        "request_case": (requests.get(request) or {}).get("case_id")
                        if request
                        else None,
                        "preview": None
                        if preview is None
                        else {
                            "url": f"/video/{stream}/{prompt}/{preview['file']}",
                            "seconds": preview.get("seconds"),
                            "planned_s": preview.get("planned_s"),
                        },
                        "rating": rating,
                        "stale": bool(rating and sha and rating.get("request_sha") != sha),
                    }
                )
        return {"sets": sets, "ideas": ideas, "ratings_file": str(self.path.resolve())}

    def _found_preview(self, stream: str, prompt: str, n: Any) -> dict[str, Any] | None:
        """An older ideas.json without previews: the file by its name."""
        if not isinstance(n, int):
            return None
        found = sorted((self.dir / stream / prompt / "previews").glob(f"{n:02d}-*.mp4"))
        if not found:
            return None
        return {"file": f"previews/{found[0].name}", "seconds": None, "planned_s": None}

    # ------------------------------------------------------------------ writes

    def rate(self, key: str, rating: Any, note: Any) -> dict[str, Any] | None:
        doc, idea = self.idea(key)
        note = str(note or "").strip()[:NOTE_CHARS]
        with self.lock:
            if rating in (None, "") and not note:
                self.ratings.pop(key, None)
                saved = None
            else:
                if rating not in (*RATINGS, None, ""):
                    raise RateError(f"rating must be one of {', '.join(RATINGS)}")
                saved = {
                    "rating": rating or None,
                    "note": note,
                    "at": utc_now().isoformat(),
                    "title": idea.get("title"),
                    "kind": idea.get("kind"),
                    "request": idea.get("request") or "",
                    "request_sha": (doc.get("meta") or {}).get("request_sha"),
                }
                self.ratings[key] = saved
            self._save()
        return saved

    def set_missed(self, set_key: str, text: Any) -> dict[str, Any] | None:
        if not _SET.match(set_key) or set_key not in self.sets:
            raise RateError(f"unknown stream/prompt {set_key!r}")
        text = str(text or "").strip()[:NOTE_CHARS]
        with self.lock:
            if text:
                self.missed[set_key] = {"text": text, "at": utc_now().isoformat()}
            else:
                self.missed.pop(set_key, None)
            self._save()
        return self.missed.get(set_key)

    def _save(self) -> None:
        doc = {
            "format": RATINGS_FORMAT,
            "ratings": dict(sorted(self.ratings.items(), key=lambda kv: _key_order(kv[0]))),
            "missed": dict(sorted(self.missed.items(), key=lambda kv: _set_order(kv[0]))),
        }
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def counts(self) -> dict[str, int]:
        keys = {f"{s}/{i.get('n')}" for s, d in self.sets.items() for i in d["ideas"]}
        rated = [r for k, r in self.ratings.items() if k in keys and r.get("rating")]
        return {"ideas": len(keys), "rated": len(rated)}


_IDEA_FIELDS = (
    "n",
    "request",
    "kind",
    "title",
    "angle",
    "why",
    "hook_text",
    "needs_visual",
    "visual_check",
    "censor",
    "confidence",
    "seconds",
    "beats",
    "issues",
)


def _set_order(set_key: str) -> tuple[int, int]:
    stream, prompt = set_key.split("/")
    return -int(prompt.rsplit("v", 1)[1]), int(stream)  # newest prompt first


def _key_order(key: str) -> tuple[int, int, int]:
    m = _KEY.match(key)
    return (int(m[1]), int(m[2].rsplit("v", 1)[1]), int(m[3])) if m else (0, 0, 0)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------- the server


def page_html() -> bytes:
    return resources.files("clipper_judge").joinpath("ideas_rate_page.html").read_bytes()


def make_handler(store: RatingStore) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "clipper-rate-ideas"

        def log_message(self, format: str, *args: Any) -> None:
            log.debug("http", line=format % args)

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, data: Any) -> None:
            self._send(status, json.dumps(data).encode("utf-8"), "application/json")

        def do_HEAD(self) -> None:
            self.do_GET()

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._send(HTTPStatus.OK, page_html(), "text/html; charset=utf-8")
            elif path == "/api/state":
                self._json(HTTPStatus.OK, store.state())
            elif path.startswith("/video/"):
                parts = path[len("/video/") :].split("/", 2)
                file = store.preview_path(*parts) if len(parts) == 3 else None
                if file is None:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "no such video"})
                    return
                send_video(self, file)
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path not in ("/api/rate", "/api/missed"):
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n > MAX_BODY:
                    raise RateError("request too large")
                body = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(body, Mapping):
                    raise RateError("expected a JSON object")
                if self.path == "/api/rate":
                    saved = store.rate(str(body.get("key")), body.get("rating"), body.get("note"))
                    self._json(HTTPStatus.OK, {"rating": saved, **store.counts()})
                else:
                    saved = store.set_missed(str(body.get("set")), body.get("text"))
                    self._json(HTTPStatus.OK, {"missed": saved})
            except (ValueError, RateError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    return Handler


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    p = sub.add_parser(
        "rate-ideas",
        help="a local page to rate the post-stream ideas (publish / with changes / no)",
        description="Serve a page on 127.0.0.1 to rate the ideas of `clipper-judge ideas`; "
        "ratings go to <dir>/ratings.json (DESIGN 15, 2026-11-26).",
    )
    p.add_argument("--dir", type=Path, default=Path("data") / "ideas", metavar="DIR")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--open", action="store_true", help="open the page in the browser")
    return p


def main(args: argparse.Namespace) -> int:
    try:
        store = RatingStore(args.dir)
    except RateError as exc:
        print(f"[error] {exc}")
        return 2
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(store))
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    c = store.counts()
    print(f"{c['ideas']} ideas in {len(store.sets)} runs, {c['rated']} rated; -> {store.path}")
    print(f"open {url}  (Ctrl+C stops)")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
