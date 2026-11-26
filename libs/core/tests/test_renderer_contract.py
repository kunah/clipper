"""Renderer contract (DESIGN §20, build item 7a): render enums vs. the model's CHECKs, the render
manifest, the storage keys + server-side copy, the asset library (manifest, licences, the
deterministic picker), `renders` rows and the lifecycle read of the real table."""

import re
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from botocore.exceptions import ClientError
from pydantic import ValidationError
from sqlalchemy import CheckConstraint, select
from sqlalchemy.exc import IntegrityError

from clipper_core import assets
from clipper_core.config import Settings
from clipper_core.contracts import (
    RENDER_MANIFEST_VERSION,
    CandidateSource,
    CandidateStatus,
    ClipStatus,
    LifecycleState,
    MusicMood,
    Priority,
    RenderAsset,
    RenderManifest,
    RenderSource,
    RenderStatus,
    UnrenderableReason,
    Verdict,
)
from clipper_core.lifecycle import RENDER_STATES, lifecycle_states
from clipper_core.models import Candidate, Channel, Clip, ClipPlan, Render
from clipper_core.models import Stream as StreamRow
from clipper_core.storage import ObjectStore, keys
from clipper_core.timeline import utc_now


def check_values(table, name: str) -> set[str]:
    full = f"ck_{table.name}_{name}"
    (ck,) = [c for c in table.constraints if isinstance(c, CheckConstraint) and c.name == full]
    return set(re.findall(r"'([^']+)'", str(ck.sqltext)))


def test_render_enums_match_the_check_constraint_and_the_lifecycle():
    assert check_values(Render.__table__, "status") == {s.value for s in RenderStatus}
    assert Render.__table__.c.status.server_default.arg == RenderStatus.RENDERING
    assert {r.value for r in UnrenderableReason} == {"no_video", "video_expired"}
    assert set(RENDER_STATES) == {s.value for s in RenderStatus}
    assert RENDER_STATES[RenderStatus.DONE] == LifecycleState.IN_REVIEW


T = datetime(2026, 11, 25, 12, 0, 0, tzinfo=UTC)


def manifest(**kw) -> RenderManifest:
    values = {
        "render_id": 5,
        "clip_id": 7,
        "plan_id": 9,
        "profile": "short_1080x1920",
        "renderer_version": "0.1.0",
        "ffmpeg_version": "ffmpeg version 8.1.3",
        "encoder": "libx264",
        "graph_hash": "ab" * 32,
        "layout": {"kind": "blurred_fit", "content": [0, 520, 1080, 608]},
        "layout_config_sha256": None,
        "sources": [
            {
                "chunk_id": 1,
                "seq": 3,
                "key": keys.clip_pinned_video(7, 3),
                "start_utc": T,
                "trim_from_s": 12.5,
                "trim_to_s": 60.0,
                "out_at_s": 0.0,
                "pad_after_s": 0.3,
            }
        ],
        "assets": [{"id": "montserrat-black", "kind": "font", "sha256": "cd" * 32}],
        "width": 1080,
        "height": 1920,
        "fps": 60.0,
        "duration_s": 47.8,
        "loudness_lufs": -14.0,
        "true_peak_db": -1.2,
        "video_sha256": "ef" * 32,
    } | kw
    return RenderManifest.model_validate(values)


def test_render_manifest_round_trip_and_its_rules():
    m = manifest(warnings=["gap_cut"])
    assert m.version == RENDER_MANIFEST_VERSION == 1
    assert RenderManifest.model_validate_json(m.model_dump_json()) == m
    assert m.sources[0] == RenderSource(
        chunk_id=1,
        seq=3,
        key="clips/7/pinned/000003.ts",
        start_utc=T,
        trim_from_s=12.5,
        trim_to_s=60.0,
        out_at_s=0.0,
        pad_after_s=0.3,
    )
    assert not m.sources[0].teaser
    assert m.assets == [RenderAsset(id="montserrat-black", kind="font", sha256="cd" * 32)]
    # a newer renderer's extra fields are ignored; another version is refused
    data = m.model_dump(mode="json") | {"extra": 1}
    assert RenderManifest.model_validate(data) == m
    with pytest.raises(ValidationError):
        manifest(version=2)
    with pytest.raises(ValidationError, match="naive"):
        manifest(sources=[m.sources[0].model_dump() | {"start_utc": datetime(2026, 11, 25)}])
    with pytest.raises(ValidationError):
        RenderAsset(id="x", kind="video", sha256="00")


def test_renderer_storage_keys():
    assert keys.clip_pinned_video(7, 3) == "clips/7/pinned/000003.ts"
    assert keys.render_file(7, 12, "clip.mp4") == "clips/7/renders/12/clip.mp4"
    assert keys.render_file(7, 12, "render.json") == "clips/7/renders/12/render.json"
    for bad in ("", "../x", "a/b", ".hidden", "a..b", "a b", "x\\y"):
        with pytest.raises(ValueError):
            keys.render_file(7, 12, bad)


@pytest.mark.integration
def test_object_store_copies_server_side():
    store = ObjectStore(Settings(s3_bucket="clipper-test", _env_file=None))
    try:
        store.ensure_bucket(wait_s=3)
    except Exception as exc:
        pytest.skip(f"object storage not reachable, run `docker compose up -d objectstore` ({exc})")
    src, dst = f"tests/{uuid.uuid4()}.ts", f"tests/{uuid.uuid4()}/pinned.ts"
    store.put_bytes(src, b"\x47" * 188 * 1000, content_type="video/mp2t")
    try:
        store.copy(src, dst)
        assert store.get_bytes(dst) == store.get_bytes(src)
        store.delete(src)  # the copy is independent of the source
        assert store.exists(dst)
        with pytest.raises(ClientError):
            store.copy(src, f"tests/{uuid.uuid4()}.ts")
    finally:
        for key in (src, dst):
            store.delete(key)


# --------------------------------------------------------------------------- assets

MANIFEST = """
version = 1

[[assets]]
id = "boom-2"
file = "sfx/boom2.wav"
sha256 = "{sha}"
kind = "sfx"
tags = ["vine_boom"]
duration_s = 1.1
[assets.licence]
name = "Test pack"
source_url = "https://example.com/pack"
licensee = "tester"
proof = "licences/pack.txt"
platforms = ["youtube", "tiktok"]

[[assets]]
id = "boom-1"
file = "sfx/boom1.wav"
sha256 = "{sha}"
kind = "sfx"
tags = ["vine_boom", "impact"]
duration_s = 0.9
[assets.licence]
name = "Test pack"
source_url = "https://example.com/pack"
licensee = "tester"
proof = "licences/pack.txt"
platforms = ["youtube"]
expires = 2026-12-31

[[assets]]
id = "bed"
file = "music/bed.wav"
sha256 = "{sha}"
kind = "music"
moods = ["funny", "chill"]
duration_s = 30
[assets.licence]
name = "Test music"
source_url = "https://example.com/music"
licensee = "tester"
proof = "https://example.com/receipt"
platforms = ["youtube", "tiktok"]
attribution = "Music: Test"
"""


def write_manifest(tmp_path, text: str):
    path = tmp_path / "manifest.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_asset_manifest_loads_and_lists_tags_and_moods(tmp_path):
    m = assets.load_manifest(write_manifest(tmp_path, MANIFEST.format(sha="a" * 64)))
    assert [a.id for a in m.assets] == ["boom-2", "boom-1", "bed"]
    assert m.tags() == ["impact", "vine_boom"]
    assert m.moods() == [MusicMood.CHILL, MusicMood.FUNNY]
    assert [a.id for a in m.by_kind("music")] == ["bed"]
    bed = m.assets[2]
    assert bed.labels() == ("funny", "chill")
    assert (bed.licence.attribution, bed.licence.expires) == ("Music: Test", None)
    assert m.assets[1].licence.expires == date(2026, 12, 31)


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (lambda t: t.replace("duration_s = 1.1", 'duration_s = 1.1\ncolour = "red"'), "colour"),
        (lambda t: t.replace('id = "boom-1"', 'id = "boom-2"'), "more than once"),
        (lambda t: t.replace('tags = ["vine_boom"]\n', ""), "needs tags"),
        (lambda t: t.replace('moods = ["funny", "chill"]', 'tags = ["x"]'), "needs moods"),
        (lambda t: t.replace('moods = ["funny", "chill"]', 'moods = ["angry"]'), "moods"),
        (lambda t: t.replace('"sfx/boom2.wav"', '"../boom2.wav"'), "relative path"),
        (lambda t: t.replace('"sfx/boom2.wav"', '"C:/x.wav"'), "relative path"),
        (lambda t: t.replace('"sfx/boom2.wav"', '"/x.wav"'), "relative path"),
        (lambda t: t.replace('id = "boom-2"', 'id = "Boom 2"'), "asset ids"),
        (lambda t: t.replace('tags = ["vine_boom"]\n', 'tags = ["Vine Boom"]\n'), "tags are"),
        (lambda t: t.replace('platforms = ["youtube"]', "platforms = []"), "platforms"),
        (
            lambda t: t.replace(
                'licensee = "tester"\nproof = "licences/pack.txt"\n'
                'platforms = ["youtube", "tiktok"]',
                "",
            ),
            "licensee",
        ),
        (lambda t: t.replace("duration_s = 30", "duration_s = 0"), "duration_s"),
        (lambda t: t.replace("version = 1", "version = 2"), "version"),
        (lambda t: t + "\n[[assets]\n", "manifest.toml"),
    ],
)
def test_asset_manifest_rejects_bad_entries(tmp_path, change, error):
    with pytest.raises(assets.ManifestError, match=error):
        assets.load_manifest(write_manifest(tmp_path, change(MANIFEST.format(sha="a" * 64))))


def test_asset_manifest_rejects_a_bad_hash_and_a_missing_licence(tmp_path):
    with pytest.raises(assets.ManifestError, match="sha256"):
        assets.load_manifest(write_manifest(tmp_path, MANIFEST.format(sha="A" * 64)))
    no_licence = MANIFEST.format(sha="a" * 64).split("[[assets]]")
    no_licence[1] = no_licence[1].split("[assets.licence]")[0]
    with pytest.raises(assets.ManifestError, match="licence"):
        assets.load_manifest(write_manifest(tmp_path, "[[assets]]".join(no_licence)))
    with pytest.raises(assets.ManifestError, match=r"missing\.toml"):
        assets.load_manifest(tmp_path / "missing.toml")


def test_licence_problems_and_file_hash(tmp_path):
    m = assets.load_manifest(write_manifest(tmp_path, MANIFEST.format(sha="a" * 64)))
    boom2, boom1, _ = m.assets
    assert assets.licence_problems(boom1, today=date(2026, 12, 31)) == []
    assert assets.licence_problems(boom1, today=date(2027, 1, 1)) == [
        "licence expired on 2026-12-31"
    ]
    assert assets.licence_problems(
        boom1, today=date(2026, 12, 1), platforms=["youtube", "tiktok", "instagram"]
    ) == ["licence doesn't cover instagram, tiktok"]
    assert assets.licence_problems(boom2, today=date(2030, 1, 1), platforms=["tiktok"]) == []
    f = tmp_path / "x.bin"
    f.write_bytes(b"hello")
    assert assets.file_sha256(f) == (
        "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )


def test_pick_is_deterministic_spread_and_by_tag_or_mood(tmp_path):
    m = assets.load_manifest(write_manifest(tmp_path, MANIFEST.format(sha="a" * 64)))
    picks = [assets.pick(m.assets, "vine_boom", clip, 0).id for clip in range(40)]
    assert picks == [assets.pick(list(reversed(m.assets)), "vine_boom", c, 0).id for c in range(40)]
    assert set(picks) == {"boom-1", "boom-2"}  # spread over clips
    assert {assets.pick(m.assets, "vine_boom", 7, cue).id for cue in range(20)} == {
        "boom-1",
        "boom-2",
    }  # and over the cues of one clip
    assert assets.pick(m.assets, "impact", 7, 0).id == "boom-1"
    assert assets.pick(m.assets, "funny", 7, 0).id == "bed"  # a music mood
    assert assets.pick(m.assets, "whoosh", 7, 0) is None


# --------------------------------------------------------------------------- database

T0 = utc_now().replace(microsecond=0)


async def make_plan(sessions, *, clips: int = 1) -> list[tuple[int, int]]:
    """(clip id, plan id) per clip: a candidate, a planned clip and its `clip` plan row."""
    out = []
    async with sessions.begin() as s:
        channel = Channel(login="render-test")
        s.add(channel)
        await s.flush()
        stream = StreamRow(channel_id=channel.id, platform_stream_id="s1", started_at=T0)
        s.add(stream)
        await s.flush()
        for i in range(clips):
            peak = T0 + timedelta(seconds=600 + 120 * i)
            cand = Candidate(
                stream_id=stream.id,
                channel_id=channel.id,
                source=CandidateSource.LIVE,
                status=CandidateStatus.EMITTED,
                trigger="chat",
                peak_utc=peak,
                chat_delay_s=8.0,
                delay_source="default",
                moment_from=peak - timedelta(seconds=10),
                moment_to=peak + timedelta(seconds=5),
                window_from=peak - timedelta(seconds=90),
                window_to=peak + timedelta(seconds=30),
                score=6.5,
                detector_version="test",
            )
            s.add(cand)
            await s.flush()
            clip = Clip(
                candidate_id=cand.id,
                stream_id=stream.id,
                channel_id=channel.id,
                status=ClipStatus.PLANNED,
                anchor_utc=peak,
                context_from=cand.window_from,
                context_to=cand.window_to,
                priority=int(Priority.TRIGGER),
                waiting_since=utc_now(),
                plan_version=1,
            )
            s.add(clip)
            await s.flush()
            plan = ClipPlan(
                clip_id=clip.id,
                version=1,
                round=0,
                verdict=Verdict.CLIP,
                plan={"version": 1},
                model="fake",
                prompt_version="v1-00000000",
            )
            s.add(plan)
            await s.flush()
            out.append((clip.id, plan.id))
    return out


@pytest.mark.integration
async def test_render_row_defaults_and_one_row_per_job(sessions):
    ((clip_id, plan_id),) = await make_plan(sessions)
    async with sessions.begin() as s:
        s.add(Render(job_id=101, clip_id=clip_id, plan_id=plan_id, profile="short_1080x1920"))
    async with sessions() as s:
        row = (await s.execute(select(Render))).scalar_one()
    assert (row.status, row.pinned_keys, row.warnings, row.unrenderable_reason) == (
        "rendering",
        [],
        [],
        None,
    )
    assert row.created_at is not None and row.finished_at is None
    with pytest.raises(IntegrityError, match="uq_renders_job_id"):
        async with sessions.begin() as s:
            s.add(Render(job_id=101, clip_id=clip_id, plan_id=plan_id, profile="x"))


@pytest.mark.integration
@pytest.mark.parametrize(
    ("values", "check"),
    [
        ({"status": "queued"}, "ck_renders_status"),
        ({"status": "unrenderable"}, "ck_renders_unrenderable_reason"),
        ({"unrenderable_reason": "no_video"}, "ck_renders_unrenderable_reason"),
        ({"status": "done"}, "ck_renders_done_outputs"),
        ({"status": "done", "video_key": "k", "video_sha256": "s"}, "ck_renders_done_outputs"),
    ],
)
async def test_render_checks_reject_bad_rows(sessions, values, check):
    ((clip_id, plan_id),) = await make_plan(sessions)
    with pytest.raises(IntegrityError, match=check):
        async with sessions.begin() as s:
            s.add(Render(job_id=1, clip_id=clip_id, plan_id=plan_id, profile="p", **values))


@pytest.mark.integration
async def test_lifecycle_reads_the_latest_real_render(sessions):
    (a, a_plan), (b, b_plan), (c, _) = await make_plan(sessions, clips=3)
    done = {"video_key": "k", "video_sha256": "s", "finished_at": utc_now()}
    async with sessions.begin() as s:
        s.add(Render(job_id=1, clip_id=a, plan_id=a_plan, profile="p", status="failed"))
        s.add(
            Render(
                job_id=2,
                clip_id=a,
                plan_id=a_plan,
                profile="p",
                status="done",
                created_at=utc_now() + timedelta(seconds=5),
                **done,
            )
        )
        s.add(
            Render(
                job_id=3,
                clip_id=b,
                plan_id=b_plan,
                profile="p",
                status="unrenderable",
                unrenderable_reason=UnrenderableReason.NO_VIDEO,
            )
        )
    async with sessions() as s:
        states = await lifecycle_states(s, [a, b, c])
    assert states == {
        a: LifecycleState.IN_REVIEW,
        b: LifecycleState.UNRENDERABLE,
        c: LifecycleState.PLANNED,
    }
