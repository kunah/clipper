"""The ffmpeg command of a render (DESIGN §20, steps 3-6). Pure: paths are relative to the job's
work directory, so the same plan and inputs give the same command (the graph hash).

    measure = measure_command(spec)            # pass 1: the audio mix -> loudnorm's measurement
    render = render_command(spec, loudness)    # pass 2: clip.mp4 + preview.mp4 in one run

Pieces (the time map's, in output order) are one input each. The piece starts at X = its chunk
offset + the file's audio origin; the input is opened with `-ss` `SEEK_PREROLL_S` before X and the
graph trims exactly at X by timestamp. (ffmpeg's MPEG-TS seek lands on the keyframe *after* the
target, seen with ffmpeg 9.0.2 on 2026-11-25: a plain `-ss X` loses up to one GOP; timestamps stay
anchored to the `-ss` value wherever the demuxer lands.) Each piece's video becomes exactly its
frames on the output grid and its audio exactly its samples (cumulative rounding of the output
times, so nothing drifts):

    [k:v] trim=start=P, setpts=PTS-P/TB, fps=F:start_time=0, tpad=stop_mode=clone (the jitter
          fill), trim=end_frame=N, setpts=N/F/TB
    [k:a] atrim=start=P, asetpts=PTS-P/TB, aresample=48k:async=1:first_pts=0, apad,
          atrim=end_sample=M, asetpts=N/SR/TB

and the pieces are concatenated per kind (video and audio apart, so neither pads the other).
Then the layout (crop/scale per panel; a blurred cover copy behind `blurred_fit`), punch-ins (the
panel's source crop shrunk by the zoom scale, switched in by `overlay=enable='between(t,a,b)'`),
the ASS file (`ass`, bundled fonts only via `fontsdir`), the audio mix (stream audio, SFX by
`adelay` at their output sample, a music bed ducked by `sidechaincompress` keyed on the stream
audio, 1 s fade-out; `amix` without normalisation), two-pass `loudnorm` (pass 2 `linear=true`
with pass 1's values) and two encodes of the same frames: the clip and the preview.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction

from clipper_core.contracts import LayoutKind
from clipper_renderer.layout import BLUR_DARKEN, Panel, Rect, ResolvedLayout, _even, zoom_rect
from clipper_renderer.profiles import SAMPLE_RATE, Profile
from clipper_renderer.timemap import Piece

TARGET_LUFS = -14.0
# loudnorm's true-peak target sits 0.5 dB under the delivery limit (-1 dBTP): the AAC encode
# adds a little overshoot (measured on the synthetic tests)
TARGET_TP = -1.5
TARGET_LRA = 11.0
INPUT_MARGIN_S = 0.5  # each piece's input reads this much past its end (the trims cut exactly)
# seek this far before a piece and trim by timestamp (>= 2 GOPs: Twitch's keyframe interval is 2 s)
SEEK_PREROLL_S = 5.0
HOLD_MARGIN_S = 1.0  # the jitter fill may clone the last frame for pad + this
# PROPOSED mix defaults
DUCK = "threshold=0.05:ratio=8:attack=20:release=400"
MUSIC_FADE_S = 1.0
BLUR = "boxblur=luma_radius=12:luma_power=2:chroma_radius=6:chroma_power=2"


@dataclass(frozen=True, slots=True)
class SourceFile:
    """A pinned chunk on disk and what ffprobe said about it."""

    path: str  # relative to the work dir, e.g. "in/000003.ts"
    width: int
    height: int
    # where offsets count from: the audio stream's start minus the file's start (>= 0). The
    # transcriber's words (and so the plan's times) count from the chunk's first audio sample.
    origin_s: float = 0.0
    has_audio: bool = True


@dataclass(frozen=True, slots=True)
class SfxInput:
    path: str  # relative, e.g. "assets/vine_boom.wav"
    at_s: float  # output time
    gain_db: float  # the cue's gain plus the asset's peak normalisation


@dataclass(frozen=True, slots=True)
class MusicInput:
    path: str
    gain_db: float
    duck: bool = True


@dataclass(frozen=True, slots=True)
class ZoomOut:
    panel: str  # Panel.name
    start_s: float
    end_s: float
    scale: float


@dataclass(frozen=True, slots=True)
class Loudness:
    """loudnorm's pass-1 measurement, as printed (strings pass through unchanged)."""

    input_i: str
    input_tp: str
    input_lra: str
    input_thresh: str
    target_offset: str

    @property
    def silent(self) -> bool:
        return self.input_i in ("-inf", "inf", "nan") or float(self.input_i) < -70


@dataclass(frozen=True)
class GraphSpec:
    pieces: Sequence[Piece]
    sources: Mapping[int, SourceFile]  # by chunk id
    fps: Fraction
    profile: Profile
    layout: ResolvedLayout
    encoder: str  # libx264 | h264_nvenc
    zooms: Sequence[ZoomOut] = ()
    subtitles: str | None = "captions.ass"
    fonts_dir: str = "fonts"
    sfx: Sequence[SfxInput] = ()
    music: MusicInput | None = None


@dataclass(frozen=True)
class Command:
    args: list[str]  # after the ffmpeg executable
    graph: str
    outputs: list[str] = field(default_factory=list)


def output_fps(source: Fraction, profile: Profile) -> Fraction:
    cap = Fraction(profile.max_fps).limit_denominator(1001)
    return source if source <= cap else cap


def _frac(seconds: float) -> Fraction:
    return Fraction(f"{seconds:.6f}")


def _counts(piece: Piece, fps: Fraction) -> tuple[int, int]:
    """(frames, samples) of a piece: cumulative rounding of its output start and end."""
    a, b = _frac(piece.out_at_s), _frac(piece.out_to_s)
    frames = round(b * fps) - round(a * fps)
    samples = round(b * SAMPLE_RATE) - round(a * SAMPLE_RATE)
    return frames, samples


def total_frames(pieces: Sequence[Piece], fps: Fraction) -> int:
    return round(_frac(pieces[-1].out_to_s) * fps) if pieces else 0


def _seek(piece: Piece, src: SourceFile) -> tuple[float, float]:
    """(the `-ss` value, the preroll the graph trims after it) of a piece's input."""
    start = round(piece.trim_from_s + src.origin_s, 6)
    ss = round(max(start - SEEK_PREROLL_S, 0.0), 6)
    return ss, round(start - ss, 6)


def _input_args(spec: GraphSpec) -> list[str]:
    args: list[str] = []
    for p in spec.pieces:
        src = spec.sources[p.chunk.chunk_id]
        ss, preroll = _seek(p, src)
        length = preroll + p.duration_s + INPUT_MARGIN_S
        args += ["-ss", f"{ss:.6f}", "-t", f"{length:.6f}", "-i", src.path]
    for s in spec.sfx:
        args += ["-i", s.path]
    if spec.music is not None:
        args += ["-stream_loop", "-1", "-i", spec.music.path]
    return args


def _piece_filters(spec: GraphSpec, *, video: bool) -> tuple[list[str], list[str], list[str]]:
    """Per-piece chains; the labels of the video and audio parts to concatenate."""
    chains: list[str] = []
    vlabels: list[str] = []
    alabels: list[str] = []
    fps = spec.fps
    ref = spec.layout.source
    for k, p in enumerate(spec.pieces):
        frames, samples = _counts(p, fps)
        src = spec.sources[p.chunk.chunk_id]
        _, pre = _seek(p, src)
        if video and frames > 0:
            hold = p.pad_after_s + HOLD_MARGIN_S
            size = "" if (src.width, src.height) == ref else f",scale={ref[0]}:{ref[1]}"
            chains.append(
                f"[{k}:v:0]trim=start={pre:.6f},setpts=PTS-{pre:.6f}/TB,"
                f"fps=fps={fps}:start_time=0:round=near{size},"
                f"tpad=stop_mode=clone:stop_duration={hold:.3f},trim=end_frame={frames},"
                f"setpts=N*{fps.denominator}/{fps.numerator}/TB,setsar=1[v{k}]"
            )
            vlabels.append(f"[v{k}]")
        if samples > 0:
            if src.has_audio:
                chains.append(
                    f"[{k}:a:0]atrim=start={pre:.6f},asetpts=PTS-{pre:.6f}/TB,"
                    f"aresample={SAMPLE_RATE}:async=1:first_pts=0,"
                    f"aformat=sample_fmts=fltp:sample_rates={SAMPLE_RATE}:channel_layouts=stereo,"
                    f"apad=whole_len={samples},atrim=end_sample={samples},asetpts=N/SR/TB[a{k}]"
                )
            else:  # a chunk without an audio stream: silence of the same length
                chains.append(
                    f"anullsrc=r={SAMPLE_RATE}:cl=stereo,atrim=end_sample={samples},"
                    f"aformat=sample_fmts=fltp,asetpts=N/SR/TB[a{k}]"
                )
            alabels.append(f"[a{k}]")
    return chains, vlabels, alabels


def _concat(labels: list[str], out: str, *, video: bool) -> str:
    if len(labels) == 1:
        return f"{labels[0]}{'null' if video else 'anull'}{out}"
    v, a = (1, 0) if video else (0, 1)
    return f"{''.join(labels)}concat=n={len(labels)}:v={v}:a={a}{out}"


def _crop(r: Rect) -> str:
    return f"crop={r.w}:{r.h}:{r.x}:{r.y}"


def _layout_filters(spec: GraphSpec) -> list[str]:
    lay = spec.layout
    W, H = lay.canvas
    zooms_by_panel: dict[str, list[ZoomOut]] = {}
    for z in sorted(spec.zooms, key=lambda z: (z.start_s, z.panel)):
        zooms_by_panel.setdefault(z.panel, []).append(z)
    branches = len(lay.panels) + len(spec.zooms) + (1 if lay.background else 0)
    labels = [f"[s{i}]" for i in range(branches)]
    chains = [f"[vcat]split={branches}{''.join(labels)}" if branches > 1 else "[vcat]null[s0]"]
    nxt = iter(labels)
    panel_out: dict[str, str] = {}
    for panel in lay.panels:
        chains.append(
            f"{next(nxt)}{_crop(panel.src)},scale={panel.dst.w}:{panel.dst.h}:flags=bicubic,"
            f"setsar=1[p_{panel.name}]"
        )
        cur = f"[p_{panel.name}]"
        for i, z in enumerate(zooms_by_panel.get(panel.name, [])):
            zr = zoom_rect(panel.src, z.scale, panel.focus_x)
            chains.append(
                f"{next(nxt)}{_crop(zr)},scale={panel.dst.w}:{panel.dst.h}:flags=bicubic,"
                f"setsar=1[z_{panel.name}{i}]"
            )
            out = f"[p_{panel.name}{i}]"
            chains.append(
                f"{cur}[z_{panel.name}{i}]overlay=0:0:"
                f"enable='between(t,{z.start_s:.3f},{z.end_s:.3f})'{out}"
            )
            cur = out
        panel_out[panel.name] = cur
    if lay.kind == LayoutKind.FACECAM_GAMEPLAY:
        chains.append(f"{panel_out['face']}{panel_out['gameplay']}vstack=inputs=2[canvas]")
    elif lay.background:
        main: Panel = lay.panels[0]
        bw, bh = _even(W / 8), _even(H / 8)
        chains.append(
            f"{next(nxt)}scale={bw}:{bh}:force_original_aspect_ratio=increase:flags=bilinear,"
            f"crop={bw}:{bh},{BLUR},scale={W}:{H}:flags=bilinear,eq=brightness={BLUR_DARKEN:g},"
            "setsar=1[bg]"
        )
        chains.append(f"[bg]{panel_out[main.name]}overlay={main.dst.x}:{main.dst.y}[canvas]")
    else:
        chains.append(f"{panel_out[lay.panels[0].name]}null[canvas]")
    return chains


def _audio_mix(spec: GraphSpec, first_input: int) -> list[str]:
    """[speech] + SFX + music -> [mix]."""
    chains: list[str] = []
    music = spec.music
    mix_inputs = ["[sp]" if music and music.duck else "[speech]"]
    if music and music.duck:
        chains.append("[speech]asplit=2[sp][key]")
    fmt = f"aformat=sample_fmts=fltp:sample_rates={SAMPLE_RATE}:channel_layouts=stereo"
    idx = first_input
    for i, s in enumerate(spec.sfx):
        delay = round(_frac(s.at_s) * SAMPLE_RATE)
        chains.append(
            f"[{idx}:a:0]aresample={SAMPLE_RATE},{fmt},volume={s.gain_db:.2f}dB,"
            f"adelay=delays={delay}S:all=1[x{i}]"
        )
        mix_inputs.append(f"[x{i}]")
        idx += 1
    if music is not None:
        total = spec.pieces[-1].out_to_s
        fade_at = max(total - MUSIC_FADE_S, 0.0)
        chains.append(
            f"[{idx}:a:0]aresample={SAMPLE_RATE},{fmt},atrim=duration={total:.6f},"
            f"volume={music.gain_db:.2f}dB,afade=t=out:st={fade_at:.3f}:d={MUSIC_FADE_S:g}[mus]"
        )
        if music.duck:
            chains.append(f"[mus][key]sidechaincompress={DUCK}[duck]")
            mix_inputs.append("[duck]")
        else:
            mix_inputs.append("[mus]")
    if len(mix_inputs) == 1:
        chains.append(f"{mix_inputs[0]}anull[mix]")
    else:
        chains.append(
            f"{''.join(mix_inputs)}amix=inputs={len(mix_inputs)}:duration=first:"
            "dropout_transition=0:normalize=0[mix]"
        )
    return chains


def _loudnorm(measured: Loudness | None) -> str:
    base = f"loudnorm=I={TARGET_LUFS:g}:TP={TARGET_TP:g}:LRA={TARGET_LRA:g}"
    if measured is None:
        return f"{base}:print_format=json"
    return (
        f"{base}:measured_I={measured.input_i}:measured_TP={measured.input_tp}:"
        f"measured_LRA={measured.input_lra}:measured_thresh={measured.input_thresh}:"
        f"offset={measured.target_offset}:linear=true:print_format=json"
    )


GLOBAL = ["-nostdin", "-hide_banner", "-nostats", "-loglevel", "info", "-y"]
BITEXACT = [
    "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact",
    "-map_metadata", "-1", "-map_chapters", "-1",
]  # fmt: skip


def measure_command(spec: GraphSpec) -> Command:
    """Pass 1: the audio mix through loudnorm (measuring), to the null muxer."""
    chains, _, alabels = _piece_filters(spec, video=False)
    chains.append(_concat(alabels, "[speech]", video=False))
    chains += _audio_mix(spec, len(spec.pieces))
    chains.append(f"[mix]{_loudnorm(None)}[aout]")
    graph = ";\n".join(chains)
    args = [*GLOBAL, *_input_args(spec), "-filter_complex", graph]
    args += ["-map", "[aout]", "-f", "null", "-"]
    return Command(args, graph, [])


def _video_codec(spec: GraphSpec, *, preview: bool, duration_s: float) -> list[str]:
    p = spec.profile
    gop = str(max(1, round(spec.fps * Fraction(p.gop_s).limit_denominator(100))))
    if preview:
        kbps = p.preview_video_kbps(duration_s)
        rate = ["-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{2 * kbps}k"]
        if spec.encoder == "h264_nvenc":
            return ["-c:v", "h264_nvenc", "-preset", p.nvenc_preset, "-rc", "vbr", *rate,
                    "-profile:v", "high", "-g", gop, "-pix_fmt", "yuv420p"]  # fmt: skip
        return ["-c:v", "libx264", "-preset", p.x264_preset, *rate, "-profile:v", "high",
                "-g", gop, "-pix_fmt", "yuv420p"]  # fmt: skip
    if spec.encoder == "h264_nvenc":
        return [
            "-c:v", "h264_nvenc", "-preset", p.nvenc_preset, "-tune", "hq", "-rc", "vbr",
            "-b:v", f"{p.nvenc_kbps}k", "-maxrate", f"{p.nvenc_max_kbps}k",
            "-bufsize", f"{2 * p.nvenc_max_kbps}k", "-profile:v", "high", "-g", gop,
            "-pix_fmt", "yuv420p",
        ]  # fmt: skip
    return ["-c:v", "libx264", "-preset", p.x264_preset, "-crf", str(p.x264_crf),
            "-profile:v", "high", "-g", gop, "-pix_fmt", "yuv420p"]  # fmt: skip


def render_command(
    spec: GraphSpec,
    measured: Loudness | None,
    *,
    clip: str = "clip.mp4",
    preview: str = "preview.mp4",
) -> Command:
    """Pass 2: the whole graph, two outputs. `measured` None or silent: no loudness change."""
    pw, ph = spec.profile.preview_width, spec.profile.preview_height
    chains, vlabels, alabels = _piece_filters(spec, video=True)
    chains.append(_concat(vlabels, "[vcat]", video=True))
    chains.append(_concat(alabels, "[speech]", video=False))
    chains += _layout_filters(spec)
    if spec.subtitles:
        chains.append(f"[canvas]ass=filename={spec.subtitles}:fontsdir={spec.fonts_dir}[cap]")
    else:
        chains.append("[canvas]null[cap]")
    chains.append("[cap]format=yuv420p,setsar=1,split=2[vclip][vp0]")
    chains.append(f"[vp0]scale={pw}:{ph}:flags=bicubic,setsar=1[vprev]")
    chains += _audio_mix(spec, len(spec.pieces))
    norm = "anull" if measured is None or measured.silent else _loudnorm(measured)
    chains.append(f"[mix]{norm},aresample={SAMPLE_RATE},asplit=2[aclip][aprev]")
    graph = ";\n".join(chains)
    duration = spec.pieces[-1].out_to_s
    p = spec.profile
    audio = ["-ar", str(SAMPLE_RATE), "-ac", "2"]
    args = [*GLOBAL, *_input_args(spec), "-filter_complex", graph]
    args += ["-map", "[vclip]", "-map", "[aclip]", *_video_codec(spec, preview=False,
             duration_s=duration), "-c:a", "aac", "-b:a", f"{p.audio_kbps}k", *audio,
             "-movflags", "+faststart", *BITEXACT, clip]  # fmt: skip
    args += ["-map", "[vprev]", "-map", "[aprev]", *_video_codec(spec, preview=True,
             duration_s=duration), "-c:a", "aac", "-b:a", f"{p.preview_audio_kbps}k", *audio,
             "-movflags", "+faststart", *BITEXACT, preview]  # fmt: skip
    return Command(args, graph, [clip, preview])
