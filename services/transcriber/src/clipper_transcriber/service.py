"""The transcriber process: engines from settings, the job runner, the heartbeat.

    engines = build_engines(transcriber_settings)       # loads the models (blocking)
    service = TranscriberService(sessions, store, engines, transcriber_settings,
                                 worker_id=settings.worker_id, dsn=settings.database_dsn)
    await service.run(stop)                             # until `stop`; finishes the current job

Engines are picked by name from `SPEECH_ENGINES` / `SOUND_TAGGERS` (`CLIPPER_TRANSCRIBER_ENGINE`,
`CLIPPER_TRANSCRIBER_TAGGER`): "whisper" (default) and "ced" (default) are the real adapters,
whose libraries are the package's optional extras (`engines.MissingDependency` without them);
"fake" (the sine-tone test engines) is only used when configured explicitly.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from clipper_core import heartbeat
from clipper_core.contracts import JobKind
from clipper_core.jobs import JobRunner
from clipper_core.logs import get_logger
from clipper_core.timeline import utc_now
from clipper_transcriber.engines import Engines, SoundTagger, SpeechEngine, VoiceActivityDetector
from clipper_transcriber.gpu import gpu_status
from clipper_transcriber.handler import JobStats, Store, TranscribeHandler
from clipper_transcriber.settings import TranscriberSettings
from clipper_transcriber.vad import EnergyVad, SileroVad

log = get_logger(__name__)

SERVICE = "transcriber"


# --------------------------------------------------------------------------- engine registry


def _fake_speech(settings: TranscriberSettings) -> SpeechEngine:
    from clipper_transcriber.fakes import FakeSpeechEngine

    return FakeSpeechEngine(segments=True)


def _fake_tagger(settings: TranscriberSettings) -> SoundTagger:
    from clipper_transcriber.fakes import FakeSoundTagger

    return FakeSoundTagger()


def _whisper(settings: TranscriberSettings) -> SpeechEngine:
    from clipper_transcriber.whisper import WhisperEngine

    return WhisperEngine.load(settings.whisper_options())


def _ced(settings: TranscriberSettings) -> SoundTagger:
    from clipper_transcriber.ced import CedTagger

    return CedTagger.load(
        device=settings.tagger_device,
        threads=settings.tagger_threads,
        batch_size=settings.tagger_batch_size,
        oom_wait_s=settings.gpu_oom_wait_s,
    )


# Speech engine adapters by `CLIPPER_TRANSCRIBER_ENGINE`. A factory builds the engine and loads
# its model (blocking; called once at start in a worker thread).
SPEECH_ENGINES: dict[str, Callable[[TranscriberSettings], SpeechEngine]] = {
    "whisper": _whisper,  # faster-whisper large-v3-turbo on CUDA (extra "whisper")
    "fake": _fake_speech,  # sine-tone "speech" for tests; never a default
}

# Sound taggers by `CLIPPER_TRANSCRIBER_TAGGER`; "none" = no `classes` in audio_metrics_1s.
SOUND_TAGGERS: dict[str, Callable[[TranscriberSettings], SoundTagger | None]] = {
    "ced": _ced,  # CED-base AudioSet tagger, CPU by default (extra "ced"); provisional
    "none": lambda settings: None,
    "fake": _fake_tagger,  # loudness-based scores for tests
}


def _pick[T](registry: Mapping[str, T], name: str, what: str) -> T:
    try:
        return registry[name]
    except KeyError:
        known = ", ".join(sorted(registry))
        raise ValueError(f"unknown {what} {name!r} (known: {known})") from None


def build_vad(settings: TranscriberSettings) -> VoiceActivityDetector:
    params = settings.vad_params()
    if settings.vad == "energy":
        return EnergyVad(params)
    vad = SileroVad(settings.models_dir, model_path=settings.vad_model_path, params=params)
    vad.load()  # downloads the pinned model into models_dir on first start
    return vad


def build_engines(settings: TranscriberSettings) -> Engines:
    """Speech engine, voice-activity detector and sound tagger from the settings, models
    loaded (blocking: call it in a worker thread). Unknown names raise ValueError."""
    speech_factory = _pick(SPEECH_ENGINES, settings.engine, "speech engine")
    tagger_factory = _pick(SOUND_TAGGERS, settings.tagger, "sound tagger")
    return Engines(
        speech=speech_factory(settings),
        vad=build_vad(settings),
        tagger=tagger_factory(settings),
    )


def engine_device(engines: Engines) -> str:
    """Where the speech engine runs ("cuda", "cpu", ...): its `device` attribute, if any."""
    return str(getattr(engines.speech, "device", "cpu"))


# --------------------------------------------------------------------------- service


async def queue_depth(session: AsyncSession) -> dict[str, int]:
    """Queued transcription jobs by priority (JSON keys: the priority as text), highest
    first."""
    rows = (
        await session.execute(
            sql_text(
                """
                SELECT priority, count(*) AS n FROM jobs
                 WHERE kind = :kind AND status = 'queued'
                 GROUP BY priority ORDER BY priority DESC
                """
            ),
            {"kind": str(JobKind.TRANSCRIBE)},
        )
    ).all()
    return {str(r.priority): r.n for r in rows}


class TranscriberService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        store: Store,
        engines: Engines,
        settings: TranscriberSettings,
        *,
        worker_id: str,
        dsn: str,
        poll_interval: float = 10.0,
    ) -> None:
        self.sessions = sessions
        self.engines = engines
        self.settings = settings
        self.worker_id = worker_id
        self.device = engine_device(engines)
        self.started_at = utc_now()
        self.stats = JobStats()
        self.handler = TranscribeHandler(sessions, store, engines, settings, stats=self.stats)
        self.runner = JobRunner(
            sessions,
            dsn,
            worker_id,
            {JobKind.TRANSCRIBE: self.handler},
            poll_interval=poll_interval,
        )

    async def run(self, stop: asyncio.Event) -> None:
        """Claim and handle jobs until `stop` is set; the job in progress is finished first."""
        beats = asyncio.create_task(
            self._every(stop, self.settings.heartbeat_interval_s, self.beat), name="heartbeat"
        )
        stopper = asyncio.create_task(self._stop_engines_on(stop), name="stop-engines")
        try:
            await self.runner.run(stop)
        finally:
            for task in (beats, stopper):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def _stop_engines_on(self, stop: asyncio.Event) -> None:
        """On shutdown, an engine waiting for GPU memory gives up at once (its job attempt
        fails and is retried later) instead of holding the stop for minutes."""
        await stop.wait()
        self.engines.request_stop()

    def info(self, queue: Mapping[str, int]) -> dict[str, Any]:
        tagger = self.engines.tagger
        rtf = self.stats.realtime_factor
        return {
            "models": {
                "speech": self.engines.speech.name,
                "tagger": tagger.name if tagger is not None else None,
                "vad": self.engines.vad.name,
            },
            "device": self.device,
            "tagger_device": getattr(tagger, "device", None) if tagger is not None else None,
            "gpu": gpu_status() if self.device.startswith("cuda") else None,
            "realtime_factor": round(rtf, 1) if rtf is not None else None,
            "jobs_done": self.stats.done,
            "jobs_failed": self.stats.failed,
            "current_chunk": self.stats.current_chunk,
            "queue": dict(queue),
        }

    async def beat(self) -> None:
        async with self.sessions.begin() as session:
            queue = await queue_depth(session)
            await heartbeat.beat(
                session,
                worker_id=self.worker_id,
                service=SERVICE,
                started_at=self.started_at,
                info=self.info(queue),
            )

    @staticmethod
    async def _every(
        stop: asyncio.Event, interval_s: float, fn: Callable[[], Awaitable[None]]
    ) -> None:
        while not stop.is_set():
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("periodic task failed", task=getattr(fn, "__name__", "?"))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval_s)
