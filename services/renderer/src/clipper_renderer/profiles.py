"""Output profiles (`RenderPayload.profile`): sizes, frame-rate cap, encoder settings.

The layout and captions are designed on a 1080x1920 canvas (`DESIGN_W` x `DESIGN_H`, the ASS
script's PlayRes); a profile scales that to its own size. `test_270x480` keeps tests fast
(libx264 ultrafast, 30 fps cap).
"""

from __future__ import annotations

from dataclasses import dataclass

DESIGN_W, DESIGN_H = 1080, 1920
SAMPLE_RATE = 48_000
# Discord's default upload limit is 20 MiB per file (checked 2026-11-21, §21): the preview stays
# below it with room for the container.
PREVIEW_MAX_BYTES = 19_000_000


@dataclass(frozen=True, slots=True)
class Profile:
    name: str
    width: int
    height: int
    preview_width: int
    preview_height: int
    max_fps: float = 60.0
    x264_preset: str = "medium"
    x264_crf: int = 20
    nvenc: bool = True  # may use h264_nvenc (else always libx264)
    nvenc_preset: str = "p5"
    nvenc_kbps: int = 8000  # VBR target
    nvenc_max_kbps: int = 12000
    audio_kbps: int = 192
    preview_kbps: int = 1500  # video; lowered for long clips so the file fits PREVIEW_MAX_BYTES
    preview_audio_kbps: int = 96
    preview_max_bytes: int = PREVIEW_MAX_BYTES
    gop_s: float = 2.0

    @property
    def scale(self) -> float:
        """Profile pixels per design pixel."""
        return self.width / DESIGN_W

    def preview_video_kbps(self, duration_s: float) -> int:
        """The preview's video bitrate: `preview_kbps`, lower when the clip is so long that the
        file would pass `preview_max_bytes` (5 % of it left for the container and VBV slack)."""
        budget_kbps = self.preview_max_bytes * 8 * 0.95 / 1000 / max(duration_s, 1.0)
        return max(200, min(self.preview_kbps, int(budget_kbps - self.preview_audio_kbps)))


PROFILES = {
    p.name: p
    for p in (
        Profile("short_1080x1920", 1080, 1920, 540, 960),
        Profile(
            "test_270x480",
            270,
            480,
            136,
            240,
            max_fps=30.0,
            x264_preset="ultrafast",
            nvenc=False,
            audio_kbps=128,
            preview_kbps=300,
            preview_audio_kbps=64,
        ),
    )
}


def get_profile(name: str) -> Profile:
    try:
        return PROFILES[name]
    except KeyError:
        raise ValueError(f"unknown render profile {name!r} (known: {sorted(PROFILES)})") from None
