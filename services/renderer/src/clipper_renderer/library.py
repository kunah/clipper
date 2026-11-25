"""The SFX/music library as the renderer uses it (DESIGN §20 "Assets"): the user's `assets/`
directory with `manifest.toml` (`clipper_core.assets`), checked before use.

An entry is usable when its file exists, matches its sha256 and its licence has no problems
(not expired, covers every target platform: `CLIPPER_RENDERER_ASSET_PLATFORMS`). Usable entries
are measured once (`ebur128`: true peak and integrated loudness) for the normalisation: an SFX
to a -6 dBFS true peak (most are shorter than the 400 ms loudness block), a music track to
-14 LUFS, before the plan's own gain (PROPOSED). `clipper-renderer assets check` prints the same
verdicts.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from clipper_core import assets
from clipper_core.assets import AssetEntry, ManifestError
from clipper_core.logs import get_logger
from clipper_core.timeline import utc_now
from clipper_renderer.graph import MEASURE
from clipper_renderer.media import FfmpegError, Tools, number, parse_ebur128

log = get_logger(__name__)

MANIFEST = "manifest.toml"
SFX_PEAK_DB = -6.0  # PROPOSED: an SFX's true peak before the cue's gain
MUSIC_LUFS = -14.0  # PROPOSED: a music track's loudness before the plan's bed gain (-20 dB)


@dataclass(frozen=True, slots=True)
class Verdict:
    entry: AssetEntry
    path: Path
    problems: tuple[str, ...]

    @property
    def usable(self) -> bool:
        return not self.problems


@dataclass(frozen=True, slots=True)
class Measured:
    loudness_lufs: float | None
    true_peak_db: float | None


class AssetLibrary:
    """Loaded per render (the manifest may change between renders); file hashes are cached by
    (path, size, mtime) and measurements by sha256 for the process's life."""

    def __init__(
        self,
        directory: Path | str | None,
        tools: Tools,
        *,
        platforms: Iterable[str] = (),
        today: Callable[[], date] = lambda: utc_now().date(),
    ) -> None:
        self.directory = Path(directory) if directory else None
        self.tools = tools
        self.platforms = tuple(platforms)
        self.today = today
        self._hashes: dict[tuple[str, int, int], str] = {}
        self._measured: dict[str, Measured] = {}

    def check(self) -> list[Verdict]:
        """Every manifest entry with its problems (none = usable). No directory or no manifest:
        an empty library. An invalid manifest raises `ManifestError`."""
        if self.directory is None or not (self.directory / MANIFEST).exists():
            return []
        manifest = assets.load_manifest(self.directory / MANIFEST)
        out = []
        for entry in manifest.assets:
            path = self.directory / entry.file
            problems = list(
                assets.licence_problems(entry, today=self.today(), platforms=self.platforms)
            )
            if not path.is_file():
                problems.insert(0, f"file missing: {entry.file}")
            elif self._sha256(path) != entry.sha256:
                problems.insert(0, "sha256 mismatch")
            out.append(Verdict(entry, path, tuple(problems)))
        return out

    def usable(self) -> list[Verdict]:
        try:
            verdicts = self.check()
        except ManifestError as exc:
            log.error("asset manifest invalid: no SFX or music", error=str(exc))
            return []
        for v in verdicts:
            if not v.usable:
                log.warning("asset not used", asset_id=v.entry.id, problems=list(v.problems))
        return [v for v in verdicts if v.usable]

    def _sha256(self, path: Path) -> str:
        st = path.stat()
        key = (str(path), st.st_size, st.st_mtime_ns)
        if key not in self._hashes:
            self._hashes[key] = assets.file_sha256(path)
        return self._hashes[key]

    def measure(self, verdict: Verdict) -> Measured:
        sha = verdict.entry.sha256
        if sha not in self._measured:
            try:
                result = self.tools.run(
                    ["-nostdin", "-hide_banner", "-nostats", "-i", str(verdict.path), "-vn",
                     "-af", MEASURE, "-f", "null", "-"],
                    timeout_s=120,
                )  # fmt: skip
                loud = parse_ebur128(result.stderr)
                integrated = None if float(loud.i) <= -69.9 else number(loud.i)
                self._measured[sha] = Measured(integrated, number(loud.tp))
            except FfmpegError as exc:
                log.warning("asset not measured", asset_id=verdict.entry.id, error=str(exc))
                self._measured[sha] = Measured(None, None)
        return self._measured[sha]

    def normalisation_db(self, verdict: Verdict) -> float:
        m = self.measure(verdict)
        if verdict.entry.kind == "sfx":
            return 0.0 if m.true_peak_db is None else round(SFX_PEAK_DB - m.true_peak_db, 2)
        return 0.0 if m.loudness_lufs is None else round(MUSIC_LUFS - m.loudness_lufs, 2)


def pick(verdicts: list[Verdict], label: str, clip_id: int, cue_index: int) -> Verdict | None:
    entry = assets.pick([v.entry for v in verdicts], label, clip_id, cue_index)
    return next((v for v in verdicts if v.entry is entry), None) if entry else None
