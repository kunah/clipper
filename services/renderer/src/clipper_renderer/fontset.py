"""The bundled fonts (`fonts/`, see its README): the only fonts the `ass` filter is given."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from clipper_core.assets import file_sha256

FONTS_DIR = Path(__file__).parent / "fonts"
FONT_LICENCE = "OFL-1.1"
SUFFIXES = (".ttf", ".otf")


@dataclass(frozen=True, slots=True)
class FontFile:
    name: str  # file name
    path: Path
    sha256: str

    @property
    def id(self) -> str:
        return self.path.stem.lower()


def bundled_fonts(directory: Path | str | None = None) -> list[FontFile]:
    """The font files (TTF/OTF) of `directory` (default: the package's), sorted by name.
    Other files (licences, READMEs) are left out: libass would try to load them."""
    d = Path(directory) if directory is not None else FONTS_DIR
    files = sorted(p for p in d.iterdir() if p.suffix.lower() in SUFFIXES)
    if not files:
        raise FileNotFoundError(f"no font files in {d}")
    return [FontFile(p.name, p, file_sha256(p)) for p in files]


def install(fonts: list[FontFile], dest: Path) -> None:
    """Copy the fonts into `dest` (the job's `fonts/`, the `ass` filter's `fontsdir`)."""
    dest.mkdir(parents=True, exist_ok=True)
    for f in fonts:
        shutil.copyfile(f.path, dest / f.name)
