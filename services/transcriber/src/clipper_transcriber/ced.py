"""Sound tagger: CED-base (AudioSet, 527 labels) as an ONNX graph on onnxruntime's CUDA provider.
The model is a PROVISIONAL choice (§15); that it runs on the GPU without torch is decided
(2026-11-20).

    tagger = CedTagger.load()            # /opt/ced/ced-base-fp16.onnx (the image's), on CUDA
    scores = tagger.classify(pcm)        # one {SoundGroup: 0..1} per (partial) second of pcm

2 s windows starting every second; a second's score is the max over the windows covering it,
and a group's score the max over its AudioSet labels (`engines.AUDIOSET_GROUPS`), as in
tools/asr-bench/run_tagger.py.

The model file is exported at image build time from the pinned Hub weights of
`mispeech/ced-base` (services/transcriber/ced_export/export_onnx.py; the build checks
`CED_ONNX_SHA256`, and so does `load`). Graph: `waveform` float32 [batch, samples], 16 kHz mono,
at most `CED_MAX_SAMPLES` (the scorer refuses longer input: the graph itself only fails well
above the bound) -> `probs` float32 [batch, 527], already
probabilities (the model's sigmoid is in the graph), index i = AudioSet class i. The mel frontend
is in the graph too (float32; the transformer runs in float16); its -120 dB floor is relative to
the loudest frame of the whole batch, like torchaudio's AmplitudeToDB in the model's Hub code.
The graph has no label names: they come from the pinned AudioSet index table (`AUDIOSET_INDEX`),
which `load` checks against the model's output width and the groups.

On Linux the transcriber's onnxruntime is onnxruntime-gpu (the CUDA 12 build). `load` refuses a
session whose first provider isn't CUDA: when the CUDA provider can't load (wrong CUDA build,
missing libraries, no GPU), ORT only logs a warning and runs on the CPU, where the float16 graph
is ~6x realtime. A GPU out-of-memory error (ORT's arena or CUDA itself, e.g. a game holding the
VRAM) is waited out in-process (`gpu.retry_on_oom`).
"""

from __future__ import annotations

import hashlib
import math
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from clipper_core.contracts import SoundGroup
from clipper_core.logs import get_logger
from clipper_transcriber.audio import SAMPLE_RATE, Pcm
from clipper_transcriber.engines import AUDIOSET_GROUPS, MissingDependency, SecondScores
from clipper_transcriber.gpu import OOM_WAIT_S, oom_delays, retry_on_oom

log = get_logger(__name__)

# The weights the ONNX file is exported from (the benchmarked revision, 2026-11-19).
CED_MODEL = "mispeech/ced-base"
CED_REVISION = "db3e14a8db4c21b56b165261c39649741a900e7f"
# The exported file: where the image puts it, and its sha256 (the Dockerfile's build check uses
# the same value; a test keeps them equal).
CED_ONNX_PATH = Path("/opt/ced/ced-base-fp16.onnx")
CED_ONNX_SHA256 = "76cbe72ec8aa39d1291fcdb1bbd722cfa3be5b99a932ba7d85428478c46a6dc6"
CED_INPUT = "waveform"
CED_OUTPUT = "probs"
# Longest input the graph is exported for: the encoder splits longer inputs (> 1012 frames) on a
# data-dependent branch, so the export is bounded (10.11 s). The graph doesn't fail right above
# the bound (only far above it), so the scorer checks it.
CED_MAX_SAMPLES = 161_760
WINDOW_S = 2
HOP_S = 1

# CUDA provider options (measured in the evaluation, DESIGN.md §16): the default power-of-two
# arena growth peaked at 633 MB instead of 505; the cuDNN conv search modes made no difference;
# TF32 off keeps the float32 mel frontend exact at no measurable cost.
CUDA_PROVIDER_OPTIONS: Mapping[str, str] = {
    "device_id": "0",
    "arena_extend_strategy": "kSameAsRequested",
    "cudnn_conv_algo_search": "HEURISTIC",
    "use_tf32": "0",
}

# [n, WINDOW_S * 16000] float32 windows -> [n, labels] probabilities
Scorer = Callable[[npt.NDArray[np.float32]], npt.NDArray[np.floating]]

AUDIOSET_LABELS = 527
# Index of every label the groups use in AudioSet's class_labels_indices.csv (as cached from
# topel/ConvNeXt-Tiny-AT e3eaf4c, 2026-11-19). CED's output i is AudioSet class i.
AUDIOSET_INDEX: Mapping[str, int] = {
    "Speech": 0,
    "Shout": 8,
    "Yell": 11,
    "Battle cry": 12,
    "Children shouting": 13,
    "Screaming": 14,
    "Laughter": 16,
    "Baby laughter": 17,
    "Giggle": 18,
    "Snicker": 19,
    "Belly laugh": 20,
    "Chuckle, chortle": 21,
    "Crying, sobbing": 22,
    "Whimper": 24,
    "Singing": 27,
    "Gasp": 44,
    "Cheering": 66,
    "Applause": 67,
    "Crowd": 69,
    "Music": 137,
    "Explosion": 426,
    "Gunshot, gunfire": 427,
    "Burst, pop": 434,
}


class LabelMismatch(RuntimeError):
    """The model's label list isn't the AudioSet one the groups were built for."""


class ModelMismatch(RuntimeError):
    """The model file isn't the pinned export (other bytes, inputs or outputs)."""


def check_audioset_labels(id2label: Mapping[Any, str]) -> None:
    """527 labels, and every label of every group at its AudioSet index; else LabelMismatch."""
    labels = {int(i): str(label) for i, label in id2label.items()}
    if len(labels) != AUDIOSET_LABELS:
        raise LabelMismatch(f"expected {AUDIOSET_LABELS} AudioSet labels, got {len(labels)}")
    wrong = {
        label: labels.get(index)
        for label, index in AUDIOSET_INDEX.items()
        if labels.get(index) != label
    }
    unknown = [lbl for ls in AUDIOSET_GROUPS.values() for lbl in ls if lbl not in AUDIOSET_INDEX]
    if wrong or unknown:
        raise LabelMismatch(f"labels not at their AudioSet index: {wrong}; unchecked: {unknown}")


def audioset_id2label(outputs: int = AUDIOSET_LABELS) -> dict[int, str]:
    """Output index -> label for a model with `outputs` AudioSet outputs, from the pinned index
    table: the groups' labels by name, the others as "AudioSet class <i>" (unused)."""
    names = {index: label for label, index in AUDIOSET_INDEX.items()}
    return {i: names.get(i, f"AudioSet class {i}") for i in range(outputs)}


def label_groups(id2label: Mapping[Any, str]) -> dict[SoundGroup, list[int]]:
    """Model output index per AudioSet label -> the indices of each group's labels (groups
    without any of their labels are left out)."""
    index = {label: int(i) for i, label in id2label.items()}
    groups = {
        g: [index[label] for label in labels if label in index]
        for g, labels in AUDIOSET_GROUPS.items()
    }
    return {g: ids for g, ids in groups.items() if ids}


def windows_for(pcm: Pcm, window_s: int = WINDOW_S) -> tuple[npt.NDArray[np.float32], list[int]]:
    """Windows of `window_s` starting at every whole second, the last one ending in the last
    (partial) second; zero-padded to full length. Returns them and their start seconds."""
    seconds = math.ceil(len(pcm) / SAMPLE_RATE)
    starts = list(range(0, max(seconds - window_s + 1, 1), HOP_S))
    n = window_s * SAMPLE_RATE
    out = np.zeros((len(starts), n), dtype=np.float32)
    for row, s in enumerate(starts):
        piece = pcm[s * SAMPLE_RATE : s * SAMPLE_RATE + n]
        out[row, : len(piece)] = piece
    return out, starts


def per_second_max(
    window_scores: npt.NDArray[np.floating], starts: Sequence[int], seconds: int, window_s: int
) -> npt.NDArray[np.float64]:
    """[windows, k] scores -> [seconds, k]: each second the max over the windows covering it."""
    out = np.zeros((seconds, window_scores.shape[1]), dtype=np.float64)
    for row, s in enumerate(starts):
        a, b = s, min(s + window_s, seconds)
        out[a:b] = np.maximum(out[a:b], window_scores[row])
    return out


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def check_io(session: Any) -> int:
    """The pinned graph's interface: input `waveform` [batch, samples], output `probs`
    [batch, labels]. Returns the number of labels; ModelMismatch otherwise."""
    inputs, outputs = session.get_inputs(), session.get_outputs()
    names = ([i.name for i in inputs], [o.name for o in outputs])
    if names[0][:1] != [CED_INPUT] or names[1][:1] != [CED_OUTPUT]:
        raise ModelMismatch(f"expected input {CED_INPUT!r} and output {CED_OUTPUT!r}, got {names}")
    width = outputs[0].shape[-1]
    if not isinstance(width, int):
        raise ModelMismatch(f"output {CED_OUTPUT!r} has no fixed label count: {outputs[0].shape}")
    return width


class CedTagger:
    """CED as a `SoundTagger`. Build it with `load`; the constructor takes any scorer (tests)."""

    def __init__(
        self,
        scorer: Scorer,
        id2label: Mapping[Any, str],
        *,
        name: str = "ced-base",
        device: str = "cuda",
        window_s: int = WINDOW_S,
        batch_size: int = 16,
        oom_wait_s: float = OOM_WAIT_S,
    ) -> None:
        self._scorer = scorer
        self.name = name
        self.device = device
        self.window_s = window_s
        self.batch_size = max(1, batch_size)
        self.oom_wait_s = oom_wait_s
        self.stopping = threading.Event()  # set on shutdown: ends a wait for GPU memory
        self.groups = label_groups(id2label)
        missing = [g.value for g in AUDIOSET_GROUPS if g not in self.groups]
        if missing:
            log.warning("sound tagger has no labels for some groups", tagger=name, groups=missing)

    @classmethod
    def load(
        cls,
        model_path: Path | str = CED_ONNX_PATH,
        *,
        sha256: str | None = CED_ONNX_SHA256,
        threads: int = 2,
        batch_size: int = 16,
        oom_wait_s: float = OOM_WAIT_S,
        warm_up: bool = True,
    ) -> CedTagger:
        """An onnxruntime session on the CUDA provider for the exported model file, warmed up
        with one batch of silence (a CUDA setup that can't run the graph fails at start, not on
        the first job). `sha256` None skips the file check (another export); `threads` = ORT's
        intra-op CPU threads for the few shape ops it keeps on the CPU (0 = one per core).
        Raises MissingDependency when the file is missing or the CUDA provider isn't active,
        ModelMismatch for another file or interface, LabelMismatch for an output width that
        isn't AudioSet's."""
        path = Path(model_path)
        if not path.is_file():
            raise MissingDependency(
                f"CED model file {path} not found: the transcriber image has it at"
                f" {CED_ONNX_PATH} (exported at build time); natively export it with"
                " services/transcriber/ced_export/export_onnx.py and set"
                " CLIPPER_TRANSCRIBER_TAGGER_MODEL_PATH, or set CLIPPER_TRANSCRIBER_TAGGER=none"
            )
        if sha256 is not None and (digest := file_sha256(path)) != sha256:
            raise ModelMismatch(f"CED model file {path} has sha256 {digest}, expected {sha256}")

        import onnxruntime as ort

        log.info(
            "loading sound tagger",
            model=CED_MODEL,
            revision=CED_REVISION,
            path=str(path),
            onnxruntime=ort.__version__,
        )
        # ORT logs to stderr, not JSON: errors only, for the process ("No registered plugin EP
        # device found for 'CUDAExecutionProvider'" at every start is a harmless warning of 1.29)
        # and for the session (shape ops kept on the CPU, memcpy nodes). A CUDA provider that
        # can't load is still logged, as an error with the reason (a missing library).
        ort.set_default_logger_severity(3)
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.log_severity_level = 3
        providers = [("CUDAExecutionProvider", dict(CUDA_PROVIDER_OPTIONS)), "CPUExecutionProvider"]
        session = ort.InferenceSession(str(path), sess_options=options, providers=providers)
        # Not `session.disable_cpu_ep_fallback`: ORT keeps a dozen int64 shape ops on the CPU on
        # purpose, which trips it.
        active = list(session.get_providers())
        if active[:1] != ["CUDAExecutionProvider"]:
            raise MissingDependency(
                f"the sound tagger needs onnxruntime's CUDA provider, but the session runs on"
                f" {active} (onnxruntime {ort.__version__}, available:"
                f" {ort.get_available_providers()}): it needs onnxruntime-gpu's CUDA 12 build,"
                " the CUDA 12 + cuDNN 9 libraries of the image and a GPU (ORT logged the reason)"
            )
        width = check_io(session)
        if width != AUDIOSET_LABELS:
            raise LabelMismatch(f"model has {width} outputs, expected {AUDIOSET_LABELS} (AudioSet)")
        id2label = audioset_id2label(width)
        check_audioset_labels(id2label)

        def scorer(windows: npt.NDArray[np.float32]) -> npt.NDArray[np.floating]:
            batch = np.ascontiguousarray(windows, dtype=np.float32)
            if batch.ndim != 2 or batch.shape[1] > CED_MAX_SAMPLES:
                raise ValueError(
                    f"CED takes [batch, <= {CED_MAX_SAMPLES}] samples, got {batch.shape}"
                )
            return session.run([CED_OUTPUT], {CED_INPUT: batch})[0]

        if warm_up:
            scorer(np.zeros((max(1, batch_size), WINDOW_S * SAMPLE_RATE), dtype=np.float32))
        return cls(
            scorer,
            id2label,
            name=path.stem,
            device="cuda",
            batch_size=batch_size,
            oom_wait_s=oom_wait_s,
        )

    def classify(self, pcm: Pcm) -> list[SecondScores]:
        seconds = math.ceil(len(pcm) / SAMPLE_RATE)
        if seconds == 0:
            return []
        windows, starts = windows_for(pcm, self.window_s)
        groups = list(self.groups.items())
        scored = np.zeros((len(starts), len(groups)), dtype=np.float64)
        delays = oom_delays(self.oom_wait_s)
        for a in range(0, len(starts), self.batch_size):
            batch = windows[a : a + self.batch_size]
            probs = np.asarray(
                retry_on_oom(
                    lambda b=batch: self._scorer(b),
                    what=self.name,
                    stopping=self.stopping,
                    delays=delays,
                ),
                dtype=np.float64,
            )
            for col, (_, ids) in enumerate(groups):
                scored[a : a + len(probs), col] = probs[:, ids].max(axis=1)
        rows = per_second_max(scored, starts, seconds, self.window_s)
        return [
            {g: float(min(max(v, 0.0), 1.0)) for (g, _), v in zip(groups, row, strict=True)}
            for row in rows
        ]
