"""The licensed SFX/music library (DESIGN §20 "Assets"), shared by the renderer and the judge.

The library is files, no table: an `assets/` directory with a `manifest.toml` the user writes;
nothing of it ships in git (§2: licensed audio only, provable per render).

    version = 1

    [[assets]]
    id = "vine_boom_1"                 # lowercase letters, digits, '_', '-'
    file = "sfx/vine_boom.wav"         # relative to the manifest's directory
    sha256 = "3f1c..."                 # of the file (lowercase hex)
    kind = "sfx"                       # sfx (with tags) | music (with moods)
    tags = ["vine_boom"]
    duration_s = 1.2

    [assets.licence]                   # required for every asset
    name = "Example SFX Pack licence"
    source_url = "https://example.com/pack"
    licensee = "the account the licence was bought for"
    proof = "licences/pack-receipt.pdf"   # a file next to the manifest or a URL
    platforms = ["youtube", "tiktok", "instagram"]
    attribution = ""                   # optional; what a post must credit
    expires = 2027-12-31               # optional

`load_manifest(path)` validates it (unknown keys are errors: it's config a person writes);
`licence_problems()` says why an entry can't be used (expired, a target platform missing);
`pick()` chooses deterministically among the entries with a tag or mood. The renderer only uses
entries whose file matches its sha256 and whose licence has no problems.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from collections.abc import Iterable, Sequence
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from clipper_core.contracts import MusicMood

MANIFEST_VERSION = 1
_ID = re.compile(r"[a-z0-9][a-z0-9_-]*")
_TAG = re.compile(r"[a-z0-9][a-z0-9_]*")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class _Config(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AssetLicence(_Config):
    name: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    licensee: str = Field(min_length=1)
    proof: str = Field(min_length=1)  # a file next to the manifest or a URL
    platforms: list[str] = Field(min_length=1)  # where the licence allows use (lowercase)
    attribution: str | None = None
    expires: date | None = None

    @field_validator("platforms")
    @classmethod
    def _lower(cls, platforms: list[str]) -> list[str]:
        for p in platforms:
            if not _TAG.fullmatch(p):
                raise ValueError(f"platform names are lowercase words: {p!r}")
        return platforms


class AssetEntry(_Config):
    id: str
    file: str
    sha256: str
    kind: Literal["sfx", "music"]
    tags: list[str] = Field(default_factory=list)  # sfx: vine_boom, whoosh, record_scratch, ...
    moods: list[MusicMood] = Field(default_factory=list)  # music
    duration_s: float = Field(gt=0)
    licence: AssetLicence

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError(f"asset ids are lowercase letters, digits, '_' and '-': {value!r}")
        return value

    @field_validator("file")
    @classmethod
    def _relative(cls, value: str) -> str:
        p = PurePosixPath(value.replace("\\", "/"))
        if not value or p.is_absolute() or ".." in p.parts or ":" in value:
            raise ValueError(f"file must be a relative path inside the library: {value!r}")
        return p.as_posix()

    @field_validator("sha256")
    @classmethod
    def _sha(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("sha256 must be 64 lowercase hex characters")
        return value

    @field_validator("tags")
    @classmethod
    def _tags(cls, tags: list[str]) -> list[str]:
        for t in tags:
            if not _TAG.fullmatch(t):
                raise ValueError(f"tags are lowercase letters, digits and '_': {t!r}")
        return tags

    @model_validator(mode="after")
    def _kind_fields(self) -> AssetEntry:
        if self.kind == "sfx" and (not self.tags or self.moods):
            raise ValueError(f"sfx {self.id!r} needs tags and no moods")
        if self.kind == "music" and (not self.moods or self.tags):
            raise ValueError(f"music {self.id!r} needs moods and no tags")
        return self

    def labels(self) -> tuple[str, ...]:
        """What `pick()` matches: the tags of an sfx, the moods of a music track."""
        return tuple(self.tags) if self.kind == "sfx" else tuple(m.value for m in self.moods)


class AssetManifest(_Config):
    version: Literal[1] = MANIFEST_VERSION
    assets: list[AssetEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> AssetManifest:
        seen: set[str] = set()
        for a in self.assets:
            if a.id in seen:
                raise ValueError(f"asset id {a.id!r} appears more than once")
            seen.add(a.id)
        return self

    def tags(self) -> list[str]:
        """Every sfx tag (sorted): the list the judge offers the model for `sfx` cues."""
        return sorted({t for a in self.assets if a.kind == "sfx" for t in a.tags})

    def moods(self) -> list[MusicMood]:
        """Every music mood (sorted by value)."""
        return sorted({m for a in self.assets if a.kind == "music" for m in a.moods})

    def by_kind(self, kind: Literal["sfx", "music"]) -> list[AssetEntry]:
        return [a for a in self.assets if a.kind == kind]


class ManifestError(ValueError):
    """The manifest can't be read or is invalid (the message says where)."""


def load_manifest(path: Path | str) -> AssetManifest:
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ManifestError(f"{path}: {exc}") from exc
    try:
        return AssetManifest.model_validate(data)
    except ValidationError as exc:
        raise ManifestError(f"{path}: {exc}") from exc


def file_sha256(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def licence_problems(entry: AssetEntry, *, today: date, platforms: Iterable[str] = ()) -> list[str]:
    """Why the entry's licence doesn't allow a render now: expired (`expires` before `today`),
    or a target platform it doesn't list. Empty = usable."""
    problems = []
    lic = entry.licence
    if lic.expires is not None and lic.expires < today:
        problems.append(f"licence expired on {lic.expires.isoformat()}")
    missing = sorted(set(platforms) - set(lic.platforms))
    if missing:
        problems.append(f"licence doesn't cover {', '.join(missing)}")
    return problems


def pick(
    entries: Sequence[AssetEntry], tag: str, clip_id: int, cue_index: int
) -> AssetEntry | None:
    """The entry for cue `cue_index` of clip `clip_id` among those with `tag` (an sfx tag or a
    music mood): sorted by id, index = sha256(clip id, cue) mod n. Deterministic (re-renders pick
    the same file) and spread over clips. None when no entry has the tag."""
    matching = sorted((e for e in entries if tag in e.labels()), key=lambda e: e.id)
    if not matching:
        return None
    digest = hashlib.sha256(f"{clip_id}:{cue_index}".encode()).digest()
    return matching[int.from_bytes(digest[:8], "big") % len(matching)]
