"""Process entry helpers shared by every service."""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from collections.abc import Callable, Coroutine
from typing import Any


def run(main: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
    """asyncio.run that also works natively on Windows.

    psycopg's async driver can't use Windows' default Proactor loop, so use a selector loop there.
    """
    if sys.platform == "win32":
        return asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)
    return asyncio.run(main())


def stop_event_on_signals() -> asyncio.Event:
    """Event that is set on SIGTERM/SIGINT (docker stop / Ctrl+C). Call inside the running loop."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        # Windows has no add_signal_handler; there Ctrl+C raises KeyboardInterrupt instead.
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)
    return stop
