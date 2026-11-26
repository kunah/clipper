"""GPU helpers: memory/utilisation for the heartbeat and logs (NVML via nvidia-ml-py, part of the
`whisper` extra), and waiting out a full GPU.

`gpu_status` reports device-wide numbers: other processes (a game) count too, which is what the
operator wants to see when the transcriber slows down; None when NVML isn't available.

`retry_on_oom` keeps a job alive while something else holds the VRAM (the user gaming on this
PC): an engine call that runs out of GPU memory is retried in-process after 30 s, 60 s, ... (up to
~10 min in all) instead of failing the job attempt at once and burning the job's retries.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from clipper_core.logs import get_logger

log = get_logger(__name__)

_lock = threading.Lock()
_nvml: Any = None
_failed = False

OOM_WAIT_S = 600.0  # total time an engine call waits for GPU memory before giving up
OOM_FIRST_DELAY_S = 30.0
OOM_MAX_DELAY_S = 240.0


def _init() -> Any:
    global _nvml, _failed
    with _lock:
        if _nvml is None and not _failed:
            try:
                import pynvml

                pynvml.nvmlInit()
                _nvml = pynvml
            except Exception:  # not installed, no driver, no GPU in the container
                _failed = True
        return _nvml


def gpu_status(index: int = 0) -> dict[str, Any] | None:
    """{"name", "memory_used_mb", "memory_total_mb", "utilization_pct"} of GPU `index`."""
    nvml = _init()
    if nvml is None:
        return None
    try:
        handle = nvml.nvmlDeviceGetHandleByIndex(index)
        mem = nvml.nvmlDeviceGetMemoryInfo(handle)
        util = nvml.nvmlDeviceGetUtilizationRates(handle)
        name = nvml.nvmlDeviceGetName(handle)
    except Exception:
        return None
    return {
        "name": name.decode() if isinstance(name, bytes) else str(name),
        "memory_used_mb": int(mem.used) // 2**20,
        "memory_total_mb": int(mem.total) // 2**20,
        "utilization_pct": int(util.gpu),
    }


# GPU allocation failures by message (lower case):
# - CTranslate2: "CUDA failed with error out of memory"; cuBLAS / cuDNN: "..._ALLOC_FAILED"
# - onnxruntime's CUDA provider: its arena ("BFCArena::AllocateRawInternal Available memory of X
#   is smaller than requested bytes of Y", "Failed to allocate memory for requested buffer of
#   size N") or CUDA itself ("CUDA failure 2: out of memory")
OOM_MESSAGES = (
    "out of memory",
    "alloc_failed",
    "failed to allocate memory",
    "smaller than requested bytes",
)


def is_onnxruntime_error(exc: BaseException) -> bool:
    """onnxruntime's own exception types (`onnxruntime.capi.onnxruntime_pybind11_state.Fail`,
    `RuntimeException`, ...) derive from Exception only, not RuntimeError."""
    return type(exc).__module__.startswith("onnxruntime")


def is_out_of_memory(exc: BaseException) -> bool:
    """A GPU allocation failure of CTranslate2 (Whisper: RuntimeError) or onnxruntime (CED: its
    own exception types), told apart from other errors by `OOM_MESSAGES`."""
    if not (isinstance(exc, RuntimeError | MemoryError) or is_onnxruntime_error(exc)):
        return False
    if type(exc).__name__ == "OutOfMemoryError":
        return True
    message = str(exc).lower()
    return any(m in message for m in OOM_MESSAGES)


def oom_delays(
    total_s: float = OOM_WAIT_S,
    first_s: float = OOM_FIRST_DELAY_S,
    max_s: float = OOM_MAX_DELAY_S,
) -> list[float]:
    """Waits between attempts: doubling from `first_s`, at most `max_s` each, `total_s` in all
    (30, 60, 120, 240, 150 for the defaults)."""
    delays: list[float] = []
    delay, left = first_s, total_s
    while left > 0 and delay > 0:
        step = min(delay, max_s, left)
        delays.append(step)
        left -= step
        delay *= 2
    return delays


def retry_on_oom[T](
    fn: Callable[[], T],
    *,
    what: str,
    stopping: threading.Event,
    delays: list[float] | None = None,
) -> T:
    """`fn()`; on a GPU out-of-memory error wait and try again (`oom_delays`), logging a warning
    with the GPU's memory each time. Raises the last error when the waits are used up or
    `stopping` is set (shutdown). Blocking: engines run in a worker thread."""
    waits = oom_delays() if delays is None else delays
    for attempt in range(len(waits) + 1):
        try:
            result = fn()
        except Exception as exc:
            if not is_out_of_memory(exc) or attempt == len(waits) or stopping.is_set():
                raise
            log.warning(
                "GPU out of memory, waiting before trying again",
                engine=what,
                attempt=attempt + 1,
                wait_s=waits[attempt],
                gpu=gpu_status(),
                error=str(exc)[:300],
            )
            if stopping.wait(waits[attempt]):
                raise
        else:
            if attempt:
                log.info("GPU memory available again", engine=what, attempts=attempt + 1)
            return result
    raise AssertionError("unreachable")
