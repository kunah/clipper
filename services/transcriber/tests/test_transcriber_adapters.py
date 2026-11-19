"""The real model adapters (faster-whisper, CED) with their libraries faked: output mapping,
the options they pass, lazy imports. No models are loaded here (the e2e run in the CUDA image
covers the real ones)."""

from __future__ import annotations

import logging
import sys
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import ModuleType, SimpleNamespace
from typing import ClassVar

import numpy as np
import pytest

from clipper_core.contracts import SoundGroup
from clipper_core.timeline import Interval
from clipper_transcriber import ced, whisper
from clipper_transcriber.audio import SAMPLE_RATE
from clipper_transcriber.ced import (
    AUDIOSET_INDEX,
    CED_REVISION,
    CedTagger,
    LabelMismatch,
    check_audioset_labels,
    label_groups,
    per_second_max,
)
from clipper_transcriber.engines import (
    AUDIOSET_GROUPS,
    Engines,
    MissingDependency,
    RecognizedWord,
    SoundTagger,
    SpeechEngine,
    route_hf_logging,
)
from clipper_transcriber.fakes import silence, tone
from clipper_transcriber.gpu import is_out_of_memory, oom_delays, retry_on_oom
from clipper_transcriber.pipeline import Window, process_window_sync
from clipper_transcriber.service import build_engines, engine_device
from clipper_transcriber.settings import TranscriberSettings
from clipper_transcriber.vad import EnergyVad
from clipper_transcriber.whisper import WhisperEngine, WhisperOptions, result_from_segments

# --------------------------------------------------------------------------- faster-whisper fake


def fw_word(text, start, end, probability):
    return SimpleNamespace(word=text, start=start, end=end, probability=probability)


def fw_segment(start, end, text, words, *, avg_logprob=-0.2, no_speech=0.01, ratio=1.3):
    return SimpleNamespace(
        start=start,
        end=end,
        text=text,
        words=words,
        avg_logprob=avg_logprob,
        no_speech_prob=no_speech,
        compression_ratio=ratio,
    )


class FakeWhisperModel:
    instances: ClassVar[list[FakeWhisperModel]] = []

    def __init__(self, model, **kwargs):
        self.model, self.kwargs = model, kwargs
        self.calls: list[tuple[np.ndarray, dict]] = []
        self.consumed = 0  # generators run to their end
        self.fail_next: list[Exception] = []  # raised while decoding, one per call
        FakeWhisperModel.instances.append(self)

    def transcribe(self, audio, **kwargs):
        self.calls.append((audio, kwargs))
        n = len(audio) / SAMPLE_RATE
        error = self.fail_next.pop(0) if self.fail_next else None

        def segments():  # lazy, like faster-whisper: decoding happens while iterating
            if error is not None:
                raise error
            yield fw_segment(
                0.1,
                min(n, 0.5),
                " Hello there.",
                [fw_word(" Hello", 0.1, 0.25, 0.9), fw_word(" there.", 0.3, 0.45, 0.8)],
            )
            yield fw_segment(
                0.5,
                min(n, 1.0),
                " Bye now.",
                [fw_word(" Bye", 0.55, 0.7, 0.9), fw_word(" now.", 0.75, 0.95, 0.7)],
            )
            self.consumed += 1  # only after the last segment: the caller drained it

        return segments(), SimpleNamespace(language="en")


@pytest.fixture
def fake_faster_whisper(monkeypatch):
    FakeWhisperModel.instances = []
    module = ModuleType("faster_whisper")
    module.WhisperModel = FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    return module


def test_whisper_output_maps_to_words_and_scored_segments():
    result = result_from_segments(
        [
            fw_segment(
                0.0,
                2.0,
                " Hi, you.",
                [
                    fw_word(" Hi,", 0.0, 0.3, 0.95),
                    fw_word(" ", 0.3, 0.3, 0.1),  # empty after strip: dropped
                    fw_word(" you.", 0.4, 0.8, 0.5),
                ],
                avg_logprob=-0.4,
                no_speech=0.2,
                ratio=2.6,
            ),
            fw_segment(3.0, 4.0, " Ok", None),  # no word timestamps: segment kept, no words
        ]
    )
    assert result.words == [
        RecognizedWord("Hi,", 0.0, 0.3, 0.95),
        RecognizedWord("you.", 0.4, 0.8, 0.5),
    ]
    first, second = result.segments
    assert (first.start, first.end, first.text) == (0.0, 2.0, "Hi, you.")
    assert (first.avg_logprob, first.no_speech_prob, first.compression_ratio) == (-0.4, 0.2, 2.6)
    assert first.confidence is None  # whisper scores segments by avg_logprob, not 0..1
    assert (second.start, second.text) == (3.0, "Ok")


def test_whisper_engine_loads_once_and_decodes_arrays_with_the_decided_options(
    fake_faster_whisper,
):
    engine = WhisperEngine.load(WhisperOptions(vad_filter=True))
    assert isinstance(engine, SpeechEngine)
    # the pinned (benchmarked) Hub revision is part of the stored model id
    assert (engine.name, engine.device) == ("faster-whisper/large-v3-turbo@0a363e9", "cuda")
    (model,) = FakeWhisperModel.instances
    assert model.model == "large-v3-turbo"
    assert model.kwargs == {
        "device": "cuda",
        "compute_type": "float16",
        "cpu_threads": 0,
        "revision": "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf",
    }
    warm_audio, warm = model.calls[0]  # warm-up at load: fails fast on a broken CUDA setup
    assert warm["vad_filter"] is False and len(warm_audio) == SAMPLE_RATE
    assert model.consumed == 1

    pcm = np.zeros(2 * SAMPLE_RATE, dtype=np.float64)  # any dtype in, float32 to the model
    results = engine.transcribe_many([pcm, pcm[:SAMPLE_RATE], pcm[:0]])
    assert [len(r.words) for r in results] == [4, 4, 0]  # both segments of each call
    assert [len(r.segments) for r in results] == [2, 2, 0]
    assert len(model.calls) == 3  # the empty input never reaches the model
    for audio, kwargs in model.calls[1:]:
        assert isinstance(audio, np.ndarray) and audio.dtype == np.float32
        assert audio.flags["C_CONTIGUOUS"]
        assert kwargs == {
            "language": "en",
            "beam_size": 5,
            "word_timestamps": True,
            "vad_filter": True,
            "condition_on_previous_text": False,
        }
    assert model.consumed == 3  # every segment generator ran to the end


def test_whisper_revision_and_model_id():
    assert WhisperOptions().model_id == "faster-whisper/large-v3-turbo@0a363e9"
    pinned = WhisperOptions(revision="abcdef1234")
    assert pinned.model_id == "faster-whisper/large-v3-turbo@abcdef1"
    other = WhisperOptions(model="distil-large-v3")  # no pinned revision known: latest
    assert other.pinned_revision is None and other.model_id == "faster-whisper/distil-large-v3"
    settings = TranscriberSettings(_env_file=None, whisper_revision="", gpu_oom_wait_s=5)
    assert settings.whisper_options().pinned_revision == WhisperOptions().pinned_revision
    assert settings.whisper_options().oom_wait_s == 5


# --------------------------------------------------------------------------- GPU out of memory

OOM = RuntimeError("CUDA failed with error out of memory")


def raises(exc):
    def fn():
        raise exc

    return fn


def test_out_of_memory_errors_are_recognised():
    assert is_out_of_memory(OOM)
    assert is_out_of_memory(RuntimeError("cuBLAS failed with status CUBLAS_STATUS_ALLOC_FAILED"))
    torch_oom = type("OutOfMemoryError", (RuntimeError,), {})("CUDA out of memory. Tried ...")
    assert is_out_of_memory(torch_oom)
    assert not is_out_of_memory(RuntimeError("CUDA failed with error invalid device ordinal"))
    assert not is_out_of_memory(ValueError("out of memory"))


def test_oom_delays_double_up_to_the_total():
    assert oom_delays() == [30, 60, 120, 240, 150]  # 10 min in all
    assert oom_delays(100) == [30, 60, 10]
    assert oom_delays(0) == []


def test_retry_on_oom_waits_and_tries_again(caplog):
    calls = []

    def flaky():
        calls.append(time.monotonic())
        if len(calls) < 3:
            raise OOM
        return "ok"

    stopping = threading.Event()
    assert retry_on_oom(flaky, what="x", stopping=stopping, delays=[0.01, 0.02, 0.5]) == "ok"
    assert len(calls) == 3
    assert sum("GPU out of memory" in m for m in caplog.messages) == 2
    assert calls[2] - calls[1] >= 0.02  # waited in between
    with pytest.raises(RuntimeError, match="out of memory"):  # waits used up: the last error
        retry_on_oom(raises(OOM), what="x", stopping=stopping, delays=[0.01])
    with pytest.raises(ValueError):  # other errors are never retried
        retry_on_oom(raises(ValueError("no")), what="x", stopping=stopping, delays=[10])


def test_retry_on_oom_gives_up_at_once_on_shutdown():
    stopping = threading.Event()
    threading.Timer(0.05, stopping.set).start()
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="out of memory"):
        retry_on_oom(raises(OOM), what="x", stopping=stopping, delays=[30, 60])
    assert time.monotonic() - started < 5


def test_whisper_waits_out_a_full_gpu(fake_faster_whisper):
    engine = WhisperEngine.load(WhisperOptions(oom_wait_s=0.03), warm_up=False)
    (model,) = FakeWhisperModel.instances
    model.fail_next = [OOM]
    pcm = np.zeros(SAMPLE_RATE, dtype=np.float32)
    assert len(engine.transcribe(pcm).words) == 4
    assert len(model.calls) == 2  # an OOM, a wait (0.03 s here), then the decode went through
    model.fail_next = [OOM, OOM]
    with pytest.raises(RuntimeError, match="out of memory"):
        engine.transcribe(pcm)  # still full after all the waits: the job attempt fails
    assert len(model.calls) == 4

    model.fail_next = [RuntimeError("CUDA failed with error unspecified launch failure")]
    with pytest.raises(RuntimeError, match="launch failure"):
        engine.transcribe(pcm)  # not an OOM: no retry, the job runner handles it

    Engines(speech=engine, vad=EnergyVad()).request_stop()  # shutdown ends waits at once
    assert engine.stopping.is_set()


def test_whisper_words_land_on_window_offsets(fake_faster_whisper):
    """Through the pipeline: the fake 'speaks' at the start of each gated stretch."""
    engine = WhisperEngine.load(WhisperOptions(), warm_up=False)
    pcm = np.concatenate([silence(3.0), tone(440.0, 1.5), silence(3.0)])
    t0 = datetime(2026, 11, 19, 3, 0, tzinfo=UTC)
    window = Window(pcm, t0, Interval(t0, t0 + timedelta(seconds=len(pcm) / SAMPLE_RATE)))
    result = process_window_sync(window, Engines(speech=engine, vad=EnergyVad()))
    (stretch,) = result.speech_regions
    assert [w.text for w in result.words] == ["Hello", "there.", "Bye", "now."]
    assert result.words[0].start == pytest.approx(stretch[0] + 0.1, abs=1e-6)
    assert [s.text for s in result.segments] == ["Hello there.", "Bye now."]
    assert all(s.avg_logprob == -0.2 and s.no_speech_prob == 0.01 for s in result.segments)


def test_whisper_is_the_default_engine_built_from_settings(fake_faster_whisper):
    settings = TranscriberSettings(
        _env_file=None, vad="energy", tagger="none", whisper_beam_size=3, whisper_device="cpu"
    )
    assert settings.engine == "whisper"
    engines = build_engines(settings)
    assert isinstance(engines.speech, WhisperEngine) and engine_device(engines) == "cpu"
    assert engines.speech.options == WhisperOptions(beam_size=3, device="cpu", vad_filter=False)
    with pytest.raises(ValueError, match="whisper_device"):
        TranscriberSettings(_env_file=None, whisper_device="gpu")
    with pytest.raises(ValueError, match="tagger_device"):
        TranscriberSettings(_env_file=None, tagger_device="cuda:0")


def test_hf_libraries_log_through_our_handler(monkeypatch):
    hub = logging.getLogger("huggingface_hub")
    own = logging.StreamHandler()
    hub.addHandler(own)
    monkeypatch.setattr(hub, "propagate", False)
    calls = []
    tf_logging = SimpleNamespace(
        disable_default_handler=lambda: calls.append("no-handler"),
        enable_propagation=lambda: calls.append("propagate"),
        disable_progress_bar=lambda: calls.append("no-bars"),
    )
    fake_tf = ModuleType("transformers")
    fake_tf.utils = SimpleNamespace(logging=tf_logging)
    monkeypatch.setitem(sys.modules, "transformers", fake_tf)
    route_hf_logging()
    assert own not in hub.handlers and hub.propagate
    assert calls == ["no-handler", "propagate", "no-bars"]


def test_missing_extras_raise_a_clear_error(monkeypatch):
    for module in ("faster_whisper", "torch", "transformers"):
        monkeypatch.setitem(sys.modules, module, None)  # import raises ImportError
    with pytest.raises(MissingDependency, match="'whisper' extra"):
        WhisperEngine.load()
    with pytest.raises(MissingDependency, match="'ced' extra"):
        CedTagger.load()


def test_adapter_modules_import_without_their_libraries():
    # The package (and these modules) import on the dev env, where the extras are missing.
    assert whisper.WhisperOptions().model == "large-v3-turbo"
    assert ced.CED_MODEL == "mispeech/ced-base"


# --------------------------------------------------------------------------- CED

# AudioSet label names as CED's config builds them (full names from the ontology csv)
LABELS = ["Speech", "Laughter", "Giggle", "Music", "Screaming", "Dog", "Gunshot, gunfire"]

# The 23 labels the groups use and their index in AudioSet's class_labels_indices.csv (checked
# against the cached copy of topel/ConvNeXt-Tiny-AT, 2026-11-19)
PINNED_AUDIOSET = {
    0: "Speech",
    8: "Shout",
    11: "Yell",
    12: "Battle cry",
    13: "Children shouting",
    14: "Screaming",
    16: "Laughter",
    17: "Baby laughter",
    18: "Giggle",
    19: "Snicker",
    20: "Belly laugh",
    21: "Chuckle, chortle",
    22: "Crying, sobbing",
    24: "Whimper",
    27: "Singing",
    44: "Gasp",
    66: "Cheering",
    67: "Applause",
    69: "Crowd",
    137: "Music",
    426: "Explosion",
    427: "Gunshot, gunfire",
    434: "Burst, pop",
}


def audioset_labels():
    """527 labels with the pinned ones at their places (str keys, like a HF config)."""
    return {str(i): PINNED_AUDIOSET.get(i, f"label {i}") for i in range(527)}


def test_audioset_index_is_pinned():
    assert {i: label for label, i in AUDIOSET_INDEX.items()} == PINNED_AUDIOSET
    group_labels = sorted(label for ls in AUDIOSET_GROUPS.values() for label in ls)
    assert group_labels == sorted(PINNED_AUDIOSET.values())  # every group label is checked


def test_label_check_rejects_moved_or_missing_labels():
    check_audioset_labels(audioset_labels())
    short = dict(list(audioset_labels().items())[:526])
    with pytest.raises(LabelMismatch, match="527"):
        check_audioset_labels(short)
    with pytest.raises(LabelMismatch, match="Music"):
        check_audioset_labels(audioset_labels() | {"137": "Musical instrument"})
    with pytest.raises(LabelMismatch, match="Chuckle"):  # the short names of CED's config.json
        check_audioset_labels(audioset_labels() | {"21": "Chuckle"})


def test_label_groups_take_every_label_the_model_has():
    groups = label_groups(dict(enumerate(LABELS)))
    assert groups[SoundGroup.LAUGHTER] == [1, 2]
    assert groups[SoundGroup.SPEECH] == [0] and groups[SoundGroup.GUNFIRE_EXPLOSION] == [6]
    assert SoundGroup.CHEERING not in groups  # none of its labels: left out


def test_per_second_max_over_covering_windows():
    scores = np.array([[0.1], [0.5], [0.2]])  # windows starting at 0, 1, 2 s (2 s long)
    out = per_second_max(scores, [0, 1, 2], seconds=4, window_s=2)
    assert out[:, 0].tolist() == [0.1, 0.5, 0.5, 0.2]


def start_encoding_scorer(calls):
    """Scores each window with its own loudness: 'Laughter' = the window's max amplitude."""

    def scorer(windows):
        calls.append(windows.shape)
        probs = np.zeros((len(windows), len(LABELS)), dtype=np.float32)
        probs[:, 1] = np.abs(windows).max(axis=1)
        probs[:, 0] = 0.25
        return probs

    return scorer


@pytest.mark.parametrize("batch_size", [1, 2, 16])
def test_ced_scores_every_second_from_2s_windows(batch_size):
    calls = []
    tagger = CedTagger(start_encoding_scorer(calls), dict(enumerate(LABELS)), batch_size=batch_size)
    assert isinstance(tagger, SoundTagger)
    # 4.5 s: a loud burst in second 2 only
    pcm = np.zeros(int(4.5 * SAMPLE_RATE), dtype=np.float32)
    pcm[2 * SAMPLE_RATE : 2 * SAMPLE_RATE + 100] = 0.8
    rows = tagger.classify(pcm)
    assert len(rows) == 5  # ceil(4.5): the last second is partial
    # windows start at 0..3 s: the burst is in the windows starting at 1 and 2 s
    assert [round(r[SoundGroup.LAUGHTER], 3) for r in rows] == [0.0, 0.8, 0.8, 0.8, 0.0]
    assert all(r[SoundGroup.SPEECH] == 0.25 for r in rows)  # probabilities kept as they are
    assert set(rows[0]) == set(label_groups(dict(enumerate(LABELS))))
    assert all(shape[1] == 2 * SAMPLE_RATE for shape in calls)
    assert sum(shape[0] for shape in calls) == 4


@pytest.mark.parametrize(("seconds", "windows"), [(0.0, 0), (0.3, 1), (1.0, 1), (2.0, 1), (3.0, 2)])
def test_ced_short_inputs(seconds, windows):
    calls = []
    tagger = CedTagger(start_encoding_scorer(calls), dict(enumerate(LABELS)))
    rows = tagger.classify(np.zeros(round(seconds * SAMPLE_RATE), dtype=np.float32))
    assert len(rows) == int(np.ceil(seconds))
    assert sum(shape[0] for shape in calls) == windows


class FakeTensor:
    def __init__(self, array, device="cpu"):
        self.array, self.device = np.asarray(array), device
        self.halved = False

    def to(self, device):
        return FakeTensor(self.array, device)

    def half(self):
        t = FakeTensor(self.array.astype(np.float16), self.device)
        t.halved = True
        return t

    def float(self):
        return FakeTensor(self.array.astype(np.float32), self.device)

    def cpu(self):
        return FakeTensor(self.array, "cpu")

    def numpy(self):
        return self.array

    def is_floating_point(self):
        return np.issubdtype(self.array.dtype, np.floating)


@contextmanager
def _no_grad():
    yield


@pytest.fixture
def fake_ced_libs(monkeypatch):
    seen = SimpleNamespace(threads=[], loads=[], inputs=[], model=None)

    torch = ModuleType("torch")
    torch.__version__ = "2.15.0+cpu"
    torch.cuda = SimpleNamespace(is_available=lambda: seen.cuda)
    seen.cuda = True
    seen.labels = audioset_labels()
    torch.set_num_threads = seen.threads.append
    torch.inference_mode = _no_grad
    torch.from_numpy = FakeTensor

    class FeatureExtractor:
        def __call__(self, x, sampling_rate, return_tensors):
            assert sampling_rate == SAMPLE_RATE and return_tensors == "pt"
            return {"input_values": FakeTensor(x.array[:, ::160])}  # "mel frames"

    class Model:
        def __init__(self):
            self.device, self.halved, self.evaluated = None, False, False
            self.config = SimpleNamespace(id2label=seen.labels)

        def to(self, device):
            self.device = device
            return self

        def eval(self):
            self.evaluated = True
            return self

        def half(self):
            self.halved = True
            return self

        def __call__(self, input_values):
            seen.inputs.append(input_values)
            probs = np.full((len(input_values.array), 527), 0.3, dtype=np.float32)
            return SimpleNamespace(logits=FakeTensor(probs, input_values.device))

    def loader(kind, factory):
        def from_pretrained(repo, **kwargs):
            seen.loads.append((kind, repo, kwargs))
            obj = factory()
            if kind == "model":
                seen.model = obj
            return obj

        return SimpleNamespace(from_pretrained=from_pretrained)

    transformers = ModuleType("transformers")
    transformers.utils = SimpleNamespace(
        logging=SimpleNamespace(
            disable_default_handler=lambda: None,
            enable_propagation=lambda: None,
            disable_progress_bar=lambda: None,
        )
    )
    transformers.AutoFeatureExtractor = loader("fe", FeatureExtractor)
    transformers.AutoModelForAudioClassification = loader("model", Model)
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "transformers", transformers)
    return seen


def test_ced_load_pins_the_hub_code_and_keeps_its_sigmoid(fake_ced_libs):
    seen = fake_ced_libs
    tagger = CedTagger.load(threads=2, batch_size=8)
    assert (tagger.name, tagger.device) == ("ced-base", "cpu")
    assert [(kind, repo) for kind, repo, _ in seen.loads] == [
        ("fe", "mispeech/ced-base"),
        ("model", "mispeech/ced-base"),
    ]
    for _, _, kwargs in seen.loads:
        assert kwargs == {"trust_remote_code": True, "revision": CED_REVISION}
    assert seen.model.evaluated and seen.model.device == "cpu" and not seen.model.halved

    rows = tagger.classify(np.zeros(10 * SAMPLE_RATE, dtype=np.float32))
    assert len(rows) == 10
    # The model's output is already a probability: 0.3 stays 0.3 (no second sigmoid)
    assert {round(v, 6) for r in rows for v in r.values()} == {0.3}
    assert [len(x.array) for x in seen.inputs] == [8, 1]  # 9 windows in batches of 8
    assert seen.threads == [2, 2]  # set on every call (OpenMP: per calling thread)


def test_ced_load_refuses_labels_that_moved(fake_ced_libs):
    fake_ced_libs.labels = audioset_labels() | {"0": "Male speech, man speaking"}
    with pytest.raises(LabelMismatch, match="Speech"):
        CedTagger.load()


def test_ced_on_cuda_needs_a_cuda_torch(fake_ced_libs):
    fake_ced_libs.cuda = False  # the image's CPU build
    with pytest.raises(MissingDependency, match="TAGGER_DEVICE=cpu"):
        CedTagger.load(device="cuda")
    assert fake_ced_libs.loads == []  # refused before loading anything


def test_ced_waits_out_a_full_gpu():
    calls = []

    def scorer(windows):
        calls.append(len(windows))
        if len(calls) == 1:
            raise OOM
        return np.full((len(windows), len(LABELS)), 0.5, dtype=np.float32)

    tagger = CedTagger(scorer, dict(enumerate(LABELS)), oom_wait_s=0.01)
    assert len(tagger.classify(np.zeros(3 * SAMPLE_RATE, dtype=np.float32))) == 3
    assert calls == [2, 2]  # the batch was scored again


def test_ced_on_cuda_runs_in_half_precision(fake_ced_libs):
    seen = fake_ced_libs
    tagger = CedTagger.load(device="cuda", threads=0)
    tagger.classify(np.zeros(3 * SAMPLE_RATE, dtype=np.float32))
    assert seen.model.device == "cuda" and seen.model.halved
    assert all(x.device == "cuda" and x.halved for x in seen.inputs)
    assert seen.threads == []  # threads=0: torch's default


def test_ced_is_built_from_settings(fake_ced_libs, fake_faster_whisper):
    settings = TranscriberSettings(_env_file=None, vad="energy", tagger_threads=3)
    assert settings.tagger == "ced" and settings.tagger_device == "cpu"
    engines = build_engines(settings)
    assert isinstance(engines.tagger, CedTagger) and engines.tagger.device == "cpu"
    engines.tagger.classify(np.zeros(SAMPLE_RATE, dtype=np.float32))
    assert fake_ced_libs.threads == [3]


def test_every_group_has_audioset_labels():
    assert all(AUDIOSET_GROUPS[g] for g in SoundGroup)


def test_dockerfile_bakes_the_pinned_silero_model():
    """The image ADDs Silero by URL + checksum; they must be the ones vad.py pins."""
    from pathlib import Path

    from clipper_transcriber.vad import SILERO_SHA256, SILERO_URL

    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")
    assert f"--checksum=sha256:{SILERO_SHA256}" in dockerfile
    assert SILERO_URL in dockerfile
