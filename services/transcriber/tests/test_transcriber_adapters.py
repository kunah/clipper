"""The real model adapters (faster-whisper, CED on onnxruntime) with their libraries faked:
output mapping, the options they pass, lazy imports. No models are loaded here (the e2e run in
the CUDA image covers the real ones)."""

from __future__ import annotations

import hashlib
import logging
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
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
    CED_MAX_SAMPLES,
    CED_MODEL,
    CED_ONNX_PATH,
    CED_ONNX_SHA256,
    CED_REVISION,
    WINDOW_S,
    CedTagger,
    LabelMismatch,
    ModelMismatch,
    audioset_id2label,
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
from clipper_transcriber.fakes import FakeSpeechEngine, silence, tone
from clipper_transcriber.gpu import is_out_of_memory, oom_delays, retry_on_oom
from clipper_transcriber.pipeline import Window, process_window_sync
from clipper_transcriber.service import build_engines, engine_device
from clipper_transcriber.settings import TranscriberSettings
from clipper_transcriber.vad import EnergyVad, SileroVad
from clipper_transcriber.whisper import (
    FALLBACK_TEMPERATURES,
    WhisperEngine,
    WhisperOptions,
    result_from_segments,
)

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
            "temperature": 0.0,  # no fallback: deterministic (DESIGN.md §16)
        }
    assert model.consumed == 3  # every segment generator ran to the end
    assert list(FALLBACK_TEMPERATURES) == [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]


def test_whisper_temperature_fallback_is_a_setting(fake_faster_whisper):
    assert not TranscriberSettings(_env_file=None).whisper_temperature_fallback  # the default
    settings = TranscriberSettings(_env_file=None, whisper_temperature_fallback=True)
    engine = WhisperEngine.load(settings.whisper_options(), warm_up=False)
    engine.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32))
    ((_, kwargs),) = FakeWhisperModel.instances[0].calls
    assert kwargs["temperature"] == [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]  # faster-whisper's default


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
    named_oom = type("OutOfMemoryError", (RuntimeError,), {})("CUDA out of memory. Tried ...")
    assert is_out_of_memory(named_oom)
    assert not is_out_of_memory(RuntimeError("CUDA failed with error invalid device ordinal"))
    assert not is_out_of_memory(ValueError("out of memory"))


def test_oom_delays_double_up_to_the_total():
    assert oom_delays() == [30, 60, 120, 240, 150]  # 10 min in all
    assert oom_delays(100) == [30, 60, 10]
    assert oom_delays(0) == []


class RecordingEvent(threading.Event):
    """A stop event that is never set: its waits return at once and are recorded (no wall
    clock: Windows' monotonic clock ticks in 15.6 ms steps)."""

    def __init__(self):
        super().__init__()
        self.waits = []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        return False


def test_retry_on_oom_waits_and_tries_again(caplog):
    calls = []

    def flaky():
        calls.append(len(calls))
        if len(calls) < 3:
            raise OOM
        return "ok"

    stopping = RecordingEvent()
    assert retry_on_oom(flaky, what="x", stopping=stopping, delays=[0.01, 0.02, 0.5]) == "ok"
    assert len(calls) == 3
    assert sum("GPU out of memory" in m for m in caplog.messages) == 2
    assert stopping.waits == [0.01, 0.02]  # waited before each new attempt
    stopping = RecordingEvent()
    with pytest.raises(RuntimeError, match="out of memory"):  # waits used up: the last error
        retry_on_oom(raises(OOM), what="x", stopping=stopping, delays=[0.01])
    assert stopping.waits == [0.01]
    stopping = RecordingEvent()
    with pytest.raises(ValueError):  # other errors are never retried
        retry_on_oom(raises(ValueError("no")), what="x", stopping=stopping, delays=[10])
    assert stopping.waits == []


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
    with pytest.raises(ValueError, match="tagger_threads"):
        TranscriberSettings(_env_file=None, tagger_threads=-1)


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


def test_missing_libraries_and_files_raise_a_clear_error(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "faster_whisper", None)  # import raises ImportError
    with pytest.raises(MissingDependency, match="'whisper' extra"):
        WhisperEngine.load()
    with pytest.raises(MissingDependency, match="TAGGER=none"):  # no model file natively
        CedTagger.load(tmp_path / "ced-base-fp16.onnx")


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


# ORT's own exception type for a failed run (onnxruntime.capi.onnxruntime_pybind11_state.Fail):
# derives from Exception only, not RuntimeError.
OrtFail = type("Fail", (Exception,), {"__module__": "onnxruntime.capi.onnxruntime_pybind11_state"})
ORT_ARENA_OOM = OrtFail(
    "[ONNXRuntimeError] : 1 : FAIL : Non-zero status code returned while running MatMul node."
    " Name:'node_MatMul_42' Status Message: BFCArena::AllocateRawInternal Available memory of"
    " 4194304 is smaller than requested bytes of 25165824"
)


class FakeOrtSession:
    """onnxruntime.InferenceSession for the CED graph: output i of every window = i / 1000, so
    each group's score tells which output indices it read."""

    def __init__(self, seen, path, sess_options=None, providers=None):
        self.seen = seen
        seen.sessions.append(self)
        self.path, self.options, self.providers = path, sess_options, providers
        self.runs = []

    def get_providers(self):
        return list(self.seen.active)

    def get_inputs(self):
        return [SimpleNamespace(name=self.seen.input, shape=["batch", "samples"])]

    def get_outputs(self):
        return [SimpleNamespace(name="probs", shape=["batch", self.seen.width])]

    def run(self, names, feeds):
        self.runs.append((names, feeds))
        if self.seen.fail_next:
            raise self.seen.fail_next.pop(0)
        (batch,) = feeds.values()
        row = np.arange(self.seen.width, dtype=np.float32) / 1000
        return [np.tile(row, (len(batch), 1))]


@pytest.fixture
def fake_ort(monkeypatch, tmp_path):
    """A fake onnxruntime module and a model file (whose sha256 reads as `seen.sha`)."""
    seen = SimpleNamespace(
        sessions=[],
        active=["CUDAExecutionProvider", "CPUExecutionProvider"],
        width=527,
        input="waveform",
        fail_next=[],
        sha=CED_ONNX_SHA256,
        model=tmp_path / "ced-base-fp16.onnx",
        default_severity=[],
    )
    seen.model.write_bytes(b"not really onnx")
    module = ModuleType("onnxruntime")
    module.__version__ = "1.29.0"
    module.SessionOptions = lambda: SimpleNamespace(intra_op_num_threads=0, log_severity_level=2)
    module.InferenceSession = lambda path, **kw: FakeOrtSession(seen, path, **kw)
    module.get_available_providers = lambda: ["CUDAExecutionProvider", "CPUExecutionProvider"]
    module.set_default_logger_severity = seen.default_severity.append
    monkeypatch.setitem(sys.modules, "onnxruntime", module)
    monkeypatch.setattr(ced, "file_sha256", lambda path: seen.sha)
    return seen


def test_ced_runs_on_the_cuda_provider_with_the_measured_options(fake_ort):
    tagger = CedTagger.load(fake_ort.model, threads=2, batch_size=8)
    assert isinstance(tagger, SoundTagger)
    assert (tagger.name, tagger.device) == ("ced-base-fp16", "cuda")
    (session,) = fake_ort.sessions
    assert session.path == str(fake_ort.model)
    assert session.providers == [
        (
            "CUDAExecutionProvider",
            {
                "device_id": "0",
                "arena_extend_strategy": "kSameAsRequested",
                "cudnn_conv_algo_search": "HEURISTIC",
                "use_tf32": "0",
            },
        ),
        "CPUExecutionProvider",
    ]
    assert session.options.intra_op_num_threads == 2
    assert session.options.log_severity_level == 3  # ORT's warnings aren't JSON
    assert fake_ort.default_severity == [3]  # its process-wide logger too

    # warm-up at load: one full batch of silence (a broken CUDA setup fails at start)
    ((_, warm),) = session.runs
    assert warm["waveform"].shape == (8, 2 * SAMPLE_RATE) and not warm["waveform"].any()

    rows = tagger.classify(np.zeros(10 * SAMPLE_RATE, dtype=np.float64))
    assert len(rows) == 10
    assert [len(feeds["waveform"]) for _, feeds in session.runs] == [8, 8, 1]  # 9 windows
    for names, feeds in session.runs:
        assert names == ["probs"] and list(feeds) == ["waveform"]
        batch = feeds["waveform"]
        assert batch.dtype == np.float32 and batch.flags["C_CONTIGUOUS"]
        assert batch.shape[1] == 2 * SAMPLE_RATE <= CED_MAX_SAMPLES
    # Output i is AudioSet class i (= i / 1000 here), a group the max over its labels' outputs,
    # and the graph's outputs are already probabilities (no second sigmoid).
    expected = {
        SoundGroup.SPEECH: 0.0,  # Speech 0
        SoundGroup.SHOUTING: 0.013,  # Shout 8, Yell 11, Battle cry 12, Children shouting 13
        SoundGroup.SCREAMING: 0.014,
        SoundGroup.LAUGHTER: 0.021,  # Laughter 16 ... Chuckle, chortle 21
        SoundGroup.CRYING: 0.024,  # Crying, sobbing 22, Whimper 24
        SoundGroup.SINGING: 0.027,
        SoundGroup.GASP: 0.044,
        SoundGroup.CHEERING: 0.069,  # Cheering 66, Applause 67, Crowd 69
        SoundGroup.MUSIC: 0.137,
        SoundGroup.GUNFIRE_EXPLOSION: 0.434,  # Explosion 426, Gunshot 427, Burst, pop 434
    }
    for row in rows:
        assert {g: round(v, 6) for g, v in row.items()} == expected


def test_ced_refuses_input_longer_than_the_graph_takes(fake_ort):
    # Right above the bound the graph itself runs (it only fails far above it), so the scorer
    # checks the length.
    tagger = CedTagger.load(fake_ort.model, warm_up=False)
    (session,) = fake_ort.sessions
    assert tagger._scorer(np.zeros((2, CED_MAX_SAMPLES), dtype=np.float32)).shape == (2, 527)
    with pytest.raises(ValueError, match="161760"):
        tagger._scorer(np.zeros((1, CED_MAX_SAMPLES + 160), dtype=np.float32))
    with pytest.raises(ValueError, match="samples"):
        tagger._scorer(np.zeros(CED_MAX_SAMPLES, dtype=np.float32))  # not [batch, samples]
    assert len(session.runs) == 1  # only the valid batch reached the session


def test_ced_refuses_to_run_without_the_cuda_provider(fake_ort):
    # A CUDA provider that can't load (e.g. PyPI's CUDA 13 build on the CUDA 12 image): ORT
    # only warns and runs everything on the CPU.
    fake_ort.active = ["CPUExecutionProvider"]
    with pytest.raises(MissingDependency, match=r"CUDA provider.*\['CPUExecutionProvider'\]"):
        CedTagger.load(fake_ort.model)


def test_ced_checks_the_model_file(fake_ort, tmp_path):
    with pytest.raises(MissingDependency, match="not found"):
        CedTagger.load(tmp_path / "missing.onnx")  # natively: no file (the image has it)
    assert fake_ort.sessions == []
    fake_ort.sha = "0" * 64  # another export (or the Hub's int8 model.onnx)
    with pytest.raises(ModelMismatch, match="sha256"):
        CedTagger.load(fake_ort.model)
    assert fake_ort.sessions == []  # refused before loading it
    CedTagger.load(fake_ort.model, sha256=None)  # an explicitly unpinned file
    fake_ort.width = 521
    with pytest.raises(LabelMismatch, match="521 outputs"):
        CedTagger.load(fake_ort.model, sha256=None)
    fake_ort.width, fake_ort.input = 527, "input_values"  # e.g. a graph without the mel frontend
    with pytest.raises(ModelMismatch, match="waveform"):
        CedTagger.load(fake_ort.model, sha256=None)


def test_file_sha256(tmp_path):
    path = tmp_path / "f.bin"
    data = bytes(range(256)) * 9000  # > one 1 MiB block
    path.write_bytes(data)
    assert ced.file_sha256(path) == hashlib.sha256(data).hexdigest()


def test_out_of_memory_errors_of_onnxruntime_are_recognised():
    assert is_out_of_memory(ORT_ARENA_OOM)
    assert is_out_of_memory(OrtFail("Failed to allocate memory for requested buffer of size 1024"))
    assert is_out_of_memory(OrtFail("CUDA failure 2: out of memory ; GPU=0 ; expr=cudaMalloc"))
    assert is_out_of_memory(OrtFail("CUDNN failure 4: CUDNN_STATUS_ALLOC_FAILED ; GPU=0"))
    # The over-length input error and other failures are not retried
    assert not is_out_of_memory(
        OrtFail(
            "Non-zero status code returned while running Add node. Name:'node_add_3'"
            " Status Message: ... cannot broadcast"
        )
    )
    assert not is_out_of_memory(Exception("out of memory"))  # not ORT, not a RuntimeError


def test_ced_waits_out_a_full_gpu(fake_ort):
    tagger = CedTagger.load(fake_ort.model, oom_wait_s=0.01, warm_up=False)
    fake_ort.fail_next = [ORT_ARENA_OOM]
    assert len(tagger.classify(np.zeros(3 * SAMPLE_RATE, dtype=np.float32))) == 3
    (session,) = fake_ort.sessions
    assert [len(feeds["waveform"]) for _, feeds in session.runs] == [2, 2]  # scored again
    fake_ort.fail_next = [ORT_ARENA_OOM, ORT_ARENA_OOM]
    with pytest.raises(OrtFail, match="BFCArena"):  # still full after the waits
        tagger.classify(np.zeros(3 * SAMPLE_RATE, dtype=np.float32))
    fake_ort.fail_next = [OrtFail("cannot broadcast")]
    with pytest.raises(OrtFail, match="broadcast"):  # not an OOM: no retry
        tagger.classify(np.zeros(3 * SAMPLE_RATE, dtype=np.float32))
    Engines(speech=FakeSpeechEngine(), vad=EnergyVad(), tagger=tagger).request_stop()
    assert tagger.stopping.is_set()


def test_ced_is_built_from_settings(fake_ort, fake_faster_whisper):
    settings = TranscriberSettings(
        _env_file=None, vad="energy", tagger_model_path=fake_ort.model, tagger_threads=3
    )
    assert settings.tagger == "ced"
    assert TranscriberSettings(_env_file=None).tagger_model_path == CED_ONNX_PATH
    engines = build_engines(settings)
    assert isinstance(engines.tagger, CedTagger) and engines.tagger.device == "cuda"
    (session,) = fake_ort.sessions
    assert session.options.intra_op_num_threads == 3
    assert [len(feeds["waveform"]) for _, feeds in session.runs] == [16]  # warm-up batch


def test_silero_session_stays_on_the_cpu(fake_ort):
    """onnxruntime-gpu in the image: Silero must not pick the CUDA provider."""
    SileroVad(model_path=fake_ort.model).load()
    (session,) = fake_ort.sessions
    assert session.providers == ["CPUExecutionProvider"]


def test_every_group_has_audioset_labels():
    assert all(AUDIOSET_GROUPS[g] for g in SoundGroup)


def test_pinned_index_table_gives_the_tagger_its_labels():
    labels = audioset_id2label()
    check_audioset_labels(labels)  # what `load` checks against the model's output width
    assert len(labels) == 527 and labels[137] == "Music" and labels[1] == "AudioSet class 1"
    assert WINDOW_S * SAMPLE_RATE <= CED_MAX_SAMPLES  # the graph's longest input


def _package_file(*parts: str) -> str:
    return Path(__file__).resolve().parents[1].joinpath(*parts).read_text(encoding="utf-8")


def test_dockerfile_bakes_the_pinned_silero_model():
    """The image ADDs Silero by URL + checksum; they must be the ones vad.py pins."""
    from clipper_transcriber.vad import SILERO_SHA256, SILERO_URL

    dockerfile = _package_file("Dockerfile")
    assert f"--checksum=sha256:{SILERO_SHA256}" in dockerfile
    assert SILERO_URL in dockerfile


def test_root_override_keeps_the_transcribers_onnxruntime_pin():
    """The root pyproject's override replaces every `onnxruntime` requirement (the transcriber's
    too): its pin and marker must be the transcriber's own."""
    import tomllib

    root = tomllib.loads((Path(__file__).resolve().parents[3] / "pyproject.toml").read_text())
    package = tomllib.loads(_package_file("pyproject.toml"))
    (override,) = root["tool"]["uv"]["override-dependencies"]
    pins = [d for d in package["project"]["dependencies"] if d.startswith("onnxruntime")]
    assert override == "onnxruntime==1.31.1; sys_platform != 'linux'"
    assert pins == [override, "onnxruntime-gpu==1.29.0; sys_platform == 'linux'"]


def test_dockerfile_exports_the_pinned_ced_model():
    """The image's build stage exports CED and checks the sha256 that `load` checks, and puts
    the file where the settings look for it; the export script uses the pinned revision."""
    dockerfile = _package_file("Dockerfile")
    assert f'echo "{CED_ONNX_SHA256}  /out/ced-base-fp16.onnx"' in dockerfile
    assert f"/out/ced-base-fp16.onnx {CED_ONNX_PATH.as_posix()}" in dockerfile
    export = _package_file("ced_export", "export_onnx.py")
    assert f'REPO = "{CED_MODEL}"' in export and f'REVISION = "{CED_REVISION}"' in export
    requirements = set(_package_file("ced_export", "requirements.txt").splitlines())
    assert {"torch==2.15.0+cpu", "torchaudio==2.11.0+cpu", "transformers==5.23.0"} <= requirements
    assert "/x/bin/python /ced_export/check_labels.py /out/id2label.json" in dockerfile


def test_export_label_check_uses_the_pinned_group_labels(tmp_path):
    """The build stage's label check compares the export's 527 names (from the unpinned label
    CSV) with group_labels.json, which must be the 23 pinned AudioSet rows."""
    import json
    import subprocess

    rows = json.loads(_package_file("ced_export", "group_labels.json"))
    assert rows == {str(i): label for i, label in PINNED_AUDIOSET.items()}
    assert rows == {str(i): label for label, i in AUDIOSET_INDEX.items()}

    script = Path(__file__).resolve().parents[1] / "ced_export" / "check_labels.py"
    exported = tmp_path / "id2label.json"

    def run(labels):
        exported.write_text(json.dumps(labels), encoding="utf-8")
        return subprocess.run(
            [sys.executable, "-I", str(script), str(exported)], capture_output=True, text=True
        )

    assert run(audioset_labels()).returncode == 0
    moved = run(audioset_labels() | {"21": "Chuckle"})  # config.json's short name
    assert moved.returncode != 0 and "index 21" in moved.stderr
    assert run(dict(list(audioset_labels().items())[:526])).returncode != 0
