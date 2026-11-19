"""Sound tagger: CED-base (AudioSet, 527 labels) through transformers. PROVISIONAL choice (§15).

    tagger = CedTagger.load(device="cpu", threads=2)
    scores = tagger.classify(pcm)        # one {SoundGroup: 0..1} per (partial) second of pcm

2 s windows starting every second; a second's score is the max over the windows covering it,
and a group's score the max over its AudioSet labels (`engines.AUDIOSET_GROUPS`), as in
tools/asr-bench/run_tagger.py. The model's Hub code already ends in a sigmoid: its "logits" are
probabilities. That code runs with `trust_remote_code`, so the repo revision is pinned.

Its config reads the label names from `topel/ConvNeXt-Tiny-AT` (class_labels_indices.csv) on that
repo's main branch, which the pinned revision doesn't cover and the remote code can't pin, so
`load` checks the labels the groups use against the AudioSet ontology (`AUDIOSET_INDEX`) and
refuses a model whose labels moved.

torch/torchaudio/transformers are the optional `ced` extra, imported only in `load`. On the CPU
(default) torch comes from the PyTorch CPU index, which keeps the image ~5 GB smaller.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import numpy as np
import numpy.typing as npt

from clipper_core.contracts import SoundGroup
from clipper_core.logs import get_logger
from clipper_transcriber.audio import SAMPLE_RATE, Pcm
from clipper_transcriber.engines import (
    AUDIOSET_GROUPS,
    MissingDependency,
    SecondScores,
    require,
    route_hf_logging,
)
from clipper_transcriber.gpu import OOM_WAIT_S, oom_delays, retry_on_oom

log = get_logger(__name__)

CED_MODEL = "mispeech/ced-base"
CED_REVISION = "db3e14a8db4c21b56b165261c39649741a900e7f"  # the benchmarked one (2026-11-19)
WINDOW_S = 2
HOP_S = 1

# [n, WINDOW_S * 16000] float32 windows -> [n, labels] probabilities
Scorer = Callable[[npt.NDArray[np.float32]], npt.NDArray[np.floating]]

AUDIOSET_LABELS = 527
# Index of every label the groups use in AudioSet's class_labels_indices.csv (as cached from
# topel/ConvNeXt-Tiny-AT e3eaf4c, 2026-11-19).
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


class CedTagger:
    """CED as a `SoundTagger`. Build it with `load`; the constructor takes any scorer (tests)."""

    def __init__(
        self,
        scorer: Scorer,
        id2label: Mapping[Any, str],
        *,
        name: str = "ced-base",
        device: str = "cpu",
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
        *,
        model: str = CED_MODEL,
        revision: str | None = CED_REVISION,
        device: str = "cpu",
        threads: int = 2,
        batch_size: int = 16,
        oom_wait_s: float = OOM_WAIT_S,
    ) -> CedTagger:
        """Load the model (into the Hugging Face cache on first use). `threads` = torch CPU
        threads (0 = torch's default); on CUDA the model runs in float16. Raises
        MissingDependency for device "cuda" with a CPU-only torch, LabelMismatch for labels
        that aren't AudioSet's."""
        torch = require("torch", "ced")
        transformers = require("transformers", "ced")
        route_hf_logging()
        half = device.startswith("cuda")
        if half and not torch.cuda.is_available():
            raise MissingDependency(
                f"sound tagger device {device!r}, but torch {torch.__version__} has no CUDA"
                " (the image has the CPU build): set CLIPPER_TRANSCRIBER_TAGGER_DEVICE=cpu"
            )
        log.info("loading sound tagger", model=model, revision=revision, device=device)
        kwargs: dict[str, Any] = {"trust_remote_code": True, "revision": revision}
        fe = transformers.AutoFeatureExtractor.from_pretrained(model, **kwargs)
        net = transformers.AutoModelForAudioClassification.from_pretrained(model, **kwargs)
        check_audioset_labels(net.config.id2label)
        net = net.to(device).eval()
        if half:
            net = net.half()

        def scorer(windows: npt.NDArray[np.float32]) -> npt.NDArray[np.floating]:
            if threads > 0:
                # Per call: with OpenMP the setting belongs to the calling (worker) thread.
                torch.set_num_threads(threads)
            inputs = fe(torch.from_numpy(windows), sampling_rate=SAMPLE_RATE, return_tensors="pt")
            moved = {}
            for key, value in inputs.items():
                value = value.to(device)
                moved[key] = value.half() if half and value.is_floating_point() else value
            with torch.inference_mode():
                probs = net(**moved).logits  # already sigmoid-ed by the Hub model code
            return probs.float().cpu().numpy()

        return cls(
            scorer,
            net.config.id2label,
            name=model.rsplit("/", 1)[-1],
            device=device,
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
