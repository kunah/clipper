"""`ClaudeCliModel`: the judge's model through the Claude Code CLI on the user's subscription
(DESIGN §19 "Claude Code CLI adapter", build item 6d; decided by the user 2026-11-21).

One headless `claude -p` process per call, no shell:

    claude -p --output-format stream-json --verbose --model <id> --tools "" --strict-mcp-config
           --no-session-persistence --max-turns 3 --system-prompt-file <file> --json-schema <json>
           [--effort <level>] [--max-budget-usd <x>]

with the user content on stdin (closed after writing) and an empty temp dir as the working dir.
`--json-schema` works through the CLI's built-in StructuredOutput tool (2 turns, hence
`--max-turns 3`). Never `--bare` (it can't read `CLAUDE_CODE_OAUTH_TOKEN`) and never
`--fallback-model` (a silent model switch would change verdicts). The CLI has no max-tokens
flag: `JudgeRequest.max_tokens` is unused here.

**Environment, built from scratch** (`child_env`): PATH, locale, temp and proxy/CA variables
(+ the Windows system variables Python and the CLI need), HOME/USERPROFILE = the judge's own
`claude_config_dir` (`settings.check_config_dir`: never the user's home or `~/.claude`, never in
a git work tree), CLAUDE_CONFIG_DIR = `<that>/.claude`, CLAUDE_CODE_OAUTH_TOKEN (the setting),
CLAUDE_CODE_MAX_RETRIES (low: the job queue does the waiting) and fixed switches: no
auto-updates, no CLAUDE.md or auto memory, no prompt history, no automatic model switch on a
safety flag. Nothing else of this process is inherited: no ANTHROPIC_* (an API key would be used
instead of the plan in `-p`), no CLAUDE_CODE_* (a run started from a Claude Code terminal would
pass that session's variables on), never CLAUDE_CODE_RETRY_WATCHDOG.

**Output** (stream-json, one JSON object per line):
- The first JSON event must be `system/init` with `apiKeySource` `"none"`. Anything else (another
  first event, another source) kills the process tree and **switches the adapter off**
  (`ModelUnavailableError`: no further process is started until the service restarts, jobs are
  held, a heartbeat warning `model_off`). The init event comes before the model request, but the
  CLI may already have sent it: the check bounds the spend to at most one request, it can't
  guarantee none. `model` and `claude_code_version` are recorded.
- `rate_limit_event` -> the usage reading (`RateLimitReading`). Extra usage beyond the plan
  (`isUsingOverage`, or the plan refusing while `overageStatus` allows) kills the call with
  `UsageLimitError` (the window's reset) unless `allow_overage`; the guard then holds every call
  until that reset (`guard.UsageGuard.hold`).
- `system/api_retry` -> logged.
- the final `result`: a `structured_output` is the answer whatever the subtype (a non-success
  subtype is logged). Without one: `error_max_turns` / `error_max_structured_output_retries` and
  a successful run return the `result` text (an invalid answer: the resolver's one retry, then
  `failed`); a "usage limit reached|<epoch>" text is a usage limit. Usage: `input_tokens` (+ cache
  writes) / `cache_read_input_tokens` / `output_tokens`; latency `duration_ms`; request id
  `session_id`; cost `total_cost_usd` (a list-price estimate, not a charge); `extras` = init event,
  CLI version, result line, rate-limit info, retries (the call's S3 record, never the hash). The
  token is removed from every text kept (stderr, result, messages).

**Errors** (every one carries the usage and cost of the result line when there was one: a failed
run can be paid): rejected credentials (`api_error_status` 401/403, an authentication error) ->
the adapter switches off (`ModelUnavailableError`); a usage or rate limit (`api_error_status` 429,
a reading whose status isn't `allowed`/`allowed_warning`, `rate_limit` retries, a limit message)
-> `UsageLimitError(resets_at)`; `error_max_budget_usd` -> `PermanentModelError` (the clip fails,
the request isn't sent again); another failed run (`is_error`, a subtype other than `success`, no
result line) -> `ModelError` (stderr tail included; the queue retries); past `claude_timeout_s`
the whole process tree is killed (Linux: the process group of a new session; Windows:
`taskkill /T /F`) -> `CallTimeoutError` (two per request at most, `calls.TIMEOUT_ATTEMPTS`). A
successful result with a non-zero exit is kept (the answer was paid; logged).

The process runs with `subprocess.Popen` and reader threads, not `asyncio.create_subprocess_exec`:
Windows' selector event loop (psycopg needs it, `clipper_core.runtime.run`) has no subprocess
support. The executable is resolved on the absolute entries of PATH only (never the working dir);
an absolute `claude_path` is best. The startup check `check_auth` runs `claude auth status` with
the same environment and requires `authMethod` `oauth_token` or `claude.ai` (and `apiProvider`
`firstParty` when it is reported).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from clipper_core.logs import get_logger
from clipper_core.timeline import utc_now
from clipper_judge.model import (
    ALLOWED_STATUSES,
    CallTimeoutError,
    JudgeResponse,
    ModelError,
    ModelUnavailableError,
    PermanentModelError,
    RateLimitReading,
    Usage,
    UsageLimitError,
    UsageWindow,
)
from clipper_judge.prompt import JudgeRequest
from clipper_judge.settings import (
    CLAUDE_MODEL,
    DEFAULT_CLAUDE_CONFIG_DIR,
    JudgeSettings,
    check_config_dir,
)

log = get_logger(__name__)

WINDOWS = sys.platform == "win32"
ADAPTER = "claude-cli"
MAX_TURNS = 3  # the StructuredOutput tool takes 2 turns
NO_API_KEY = "none"  # init.apiKeySource on a subscription login or an OAuth token
ACCEPTED_AUTH = frozenset({"oauth_token", "claude.ai"})  # `claude auth status` authMethod
FIRST_PARTY = "firstParty"  # `claude auth status` apiProvider (when reported)
CREDENTIAL_WARN = timedelta(days=14)  # heartbeat warning before the token/login expires
AUTH_TIMEOUT_S = 30.0
EXIT_GRACE_S = 10.0  # after the result line the process gets this long to exit, then is killed
KILL_WAIT_S = 10.0
OFF_RETRY_S = 600.0  # a held job (the adapter switched off) comes back after this long
STDERR_TAIL_CHARS = 4000
STDERR_LINE_CHARS = 500  # each stderr line is cut to this
NON_JSON_KEPT = 5  # non-JSON stdout lines kept for the record
TMP_PREFIX = "clipper-judge-"  # the per-call temp dir (system temp: never inside a checkout)
ANSWER_SUBTYPES = frozenset({"error_max_turns", "error_max_structured_output_retries"})
BUDGET_SUBTYPE = "error_max_budget_usd"
AUTH_STATUSES = frozenset({401, 403})
AUTH_RETRY_ERRORS = frozenset({"authentication_failed", "oauth_org_not_allowed", "account_on_hold"})
_LIMIT_TEXT = re.compile(
    r"usage limit|rate limit|limit reached|hit your .{0,20}limit|out of (?:extra )?usage",
    re.IGNORECASE,
)
_LIMIT_EPOCH = re.compile(r"usage limit reached\|(\d{9,13})", re.IGNORECASE)
_AUTH_TEXT = re.compile(
    r"authentication|invalid (?:api key|bearer|token|x-api-key)|oauth token|token (?:has )?"
    r"(?:expired|been revoked)|please run /login|unauthori[sz]ed|\b40[13]\b",
    re.IGNORECASE,
)
_TOKENISH = re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}")
REDACTED = "[redacted]"

# The environment of the CLI: these variables of this process are passed on (system, locale,
# temp dirs, network); everything else is set by `child_env` or left out.
PASS_THROUGH = (
    "PATH",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TMPDIR",
    "TEMP",
    "TMP",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS",
)
WINDOWS_PASS_THROUGH = ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT")
FIXED_ENV = {
    "DISABLE_AUTOUPDATER": "1",  # the image's binary is pinned; natively: not the user's install
    "DISABLE_UPDATES": "1",
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",  # no CLAUDE.md of any parent directory
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_SKIP_PROMPT_HISTORY": "1",  # with --no-session-persistence: nothing kept
    "CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK": "1",  # no silent model switch on a safety flag
}
# What the child environment may hold of CLAUDE_CODE_* (tests assert it)
ALLOWED_CLAUDE_CODE = frozenset(
    {"CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_MAX_RETRIES"}
    | {k for k in FIXED_ENV if k.startswith("CLAUDE_CODE_")}
)

Clock = Callable[[], datetime]


class ClaudeAuthError(RuntimeError):
    """`claude auth status` doesn't show a subscription login: the judge must not run."""


# --------------------------------------------------------------------------- environment


def config_paths(config_dir: Path) -> tuple[Path, Path]:
    """(HOME of the CLI, its CLAUDE_CONFIG_DIR) inside the judge's own directory."""
    home = Path(os.path.expanduser(str(config_dir))).resolve()
    return home, home / ".claude"


def child_env(
    config_dir: Path,
    *,
    oauth_token: str | None,
    max_retries: int,
    parent: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The CLI's whole environment (see the module docstring)."""
    parent = os.environ if parent is None else parent
    home, claude_dir = config_paths(config_dir)
    names = PASS_THROUGH + (WINDOWS_PASS_THROUGH if WINDOWS else ())
    env: dict[str, str] = {}
    for name in names:
        value = parent.get(name)
        if value is None and WINDOWS:  # os.environ is case-insensitive there, a Mapping isn't
            value = next((v for k, v in parent.items() if k.upper() == name.upper()), None)
        if value is not None:
            env[name] = value
    env["HOME"] = str(home)
    env["CLAUDE_CONFIG_DIR"] = str(claude_dir)
    if WINDOWS:
        env["USERPROFILE"] = str(home)
        env["APPDATA"] = str(home / "AppData" / "Roaming")
        env["LOCALAPPDATA"] = str(home / "AppData" / "Local")
    env.update(FIXED_ENV)
    env["CLAUDE_CODE_MAX_RETRIES"] = str(max_retries)
    if oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
    return env


def prepare_config_dir(config_dir: Path) -> Path:
    """Create the judge's CLI directories; `PermanentModelError` if they can't be written."""
    home, claude_dir = config_paths(config_dir)
    try:
        claude_dir.mkdir(parents=True, exist_ok=True)
        if WINDOWS:
            (home / "AppData" / "Roaming").mkdir(parents=True, exist_ok=True)
            (home / "AppData" / "Local").mkdir(parents=True, exist_ok=True)
        probe = claude_dir / ".clipper-write-test"
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError as exc:
        raise PermanentModelError(f"claude_config_dir {home} is not writable: {exc}") from exc
    return home


def find_executable(name: str, path: str | None) -> str | None:
    """`name` on the absolute entries of `path` (never the working directory, unlike
    `shutil.which` on Windows); on Windows `name.exe` first, then `name` as given."""
    names = [name + ".exe", name] if WINDOWS and not name.lower().endswith(".exe") else [name]
    for entry in (path or "").split(os.pathsep):
        entry = entry.strip().strip('"')
        if not entry or not os.path.isabs(entry):
            continue
        for n in names:
            candidate = os.path.join(entry, n)
            if os.path.isfile(candidate) and (WINDOWS or os.access(candidate, os.X_OK)):
                return candidate
    return None


def resolve_command(command: Sequence[str], env: Mapping[str, str]) -> list[str]:
    """The argv prefix with an absolute executable: `claude_path` as given when absolute (best),
    else looked up on the child's PATH (absolute entries only). A Windows `.cmd`/`.bat` shim
    (npm) is refused: it would pass the JSON schema through cmd.exe's parsing."""
    if not command:
        raise PermanentModelError("claude_path is empty")
    exe, *rest = command
    if os.path.isabs(exe):
        found: str | None = exe if os.path.isfile(exe) else None
    elif os.path.dirname(exe):
        raise PermanentModelError(f"claude_path {exe!r}: give an absolute path or a bare name")
    else:
        found = find_executable(exe, env.get("PATH"))
    if found is None:
        raise PermanentModelError(
            f"claude executable not found: {exe!r} (CLIPPER_JUDGE_CLAUDE_PATH)"
        )
    if WINDOWS and found.lower().endswith((".cmd", ".bat")):
        raise PermanentModelError(
            f"{found}: use the native claude.exe, not a .cmd shim (arguments would go through "
            "cmd.exe)"
        )
    return [found, *rest]


# --------------------------------------------------------------------------- the process


class CliProcess:
    """One child process (no shell) in its own process group / session: stdin written and
    closed by a thread, stdout lines handed to the event loop by a reader thread, the stderr
    tail kept by another (each line cut to `STDERR_LINE_CHARS`). Works on every event loop (no
    asyncio subprocesses). `kill` kills the tree once, in a worker thread."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: Path,
        stdin: bytes = b"",
    ) -> None:
        self.argv = list(argv)
        self._loop = asyncio.get_running_loop()
        self._lines: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._stderr: deque[str] = deque()
        self._stderr_chars = 0
        self._kill_lock = threading.Lock()
        kwargs: dict[str, Any] = {}
        if WINDOWS:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True  # its own process group: killpg reaches the tree
        self.proc = subprocess.Popen(
            self.argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(env),
            cwd=str(cwd),
            **kwargs,
        )
        self.killed = False
        self._threads = [
            threading.Thread(target=self._write_stdin, args=(stdin,), daemon=True),
            threading.Thread(target=self._read_stdout, daemon=True),
            threading.Thread(target=self._read_stderr, daemon=True),
        ]
        for t in self._threads:
            t.start()

    @property
    def pid(self) -> int:
        return self.proc.pid

    def _write_stdin(self, data: bytes) -> None:
        stream = self.proc.stdin
        assert stream is not None
        try:
            if data:
                stream.write(data)
        except OSError:  # the process ended before reading it all
            pass
        finally:
            with contextlib.suppress(OSError):
                stream.close()

    def _put(self, item: bytes | None) -> bool:
        try:
            self._loop.call_soon_threadsafe(self._lines.put_nowait, item)
        except RuntimeError:  # the loop is closed
            return False
        return True

    def _read_stdout(self) -> None:
        stream = self.proc.stdout
        assert stream is not None
        try:
            for line in iter(stream.readline, b""):
                if not self._put(line):
                    return
        except (OSError, ValueError):
            pass
        self._put(None)

    def _read_stderr(self) -> None:
        stream = self.proc.stderr
        assert stream is not None
        try:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if len(line) > STDERR_LINE_CHARS:
                    line = line[:STDERR_LINE_CHARS] + "…"
                line += "\n"
                self._stderr.append(line)
                self._stderr_chars += len(line)
                while self._stderr_chars > STDERR_TAIL_CHARS and len(self._stderr) > 1:
                    self._stderr_chars -= len(self._stderr.popleft())
        except (OSError, ValueError):
            pass

    async def next_line(self, within_s: float) -> bytes | None:
        """The next stdout line; None at the end of output. `TimeoutError` after `within_s`."""
        if within_s <= 0:
            raise TimeoutError
        return await asyncio.wait_for(self._lines.get(), within_s)

    async def wait(self, within_s: float) -> int | None:
        """The exit code, or None if the process is still running after `within_s`."""
        if self.proc.poll() is not None:
            return self.proc.returncode
        if within_s <= 0:
            return None
        try:
            return await asyncio.to_thread(self.proc.wait, within_s)
        except subprocess.TimeoutExpired:
            return None

    def kill_tree(self) -> None:
        """Kill the process and everything it started, once (blocking, short). Linux: SIGKILL to
        the whole process group (also after the leader exited; ESRCH ignored)."""
        with self._kill_lock:
            if self.killed:
                return
            self.killed = True
        if WINDOWS:
            if self.proc.poll() is None:  # the tree is found through the live parent only
                root = os.environ.get("SYSTEMROOT", r"C:\Windows")
                taskkill = os.path.join(root, "System32", "taskkill.exe")
                with contextlib.suppress(OSError, subprocess.SubprocessError):
                    subprocess.run(
                        [taskkill, "/T", "/F", "/PID", str(self.proc.pid)],
                        capture_output=True,
                        timeout=KILL_WAIT_S,
                        check=False,
                    )
        else:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self.proc.pid, signal.SIGKILL)
        with contextlib.suppress(OSError):
            self.proc.kill()

    async def kill(self) -> None:
        """`kill_tree` in a worker thread (it finishes even if this task is cancelled)."""
        if not self.killed:
            await asyncio.shield(asyncio.to_thread(self.kill_tree))

    def stderr_tail(self) -> str:
        return "".join(self._stderr)[-STDERR_TAIL_CHARS:].strip()

    async def close(self) -> None:
        """Kill what is left (Linux: always the process group; Windows: a process still
        running), reap it, let the reader threads end."""
        if not WINDOWS or self.proc.poll() is None:
            await self.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            await asyncio.to_thread(self.proc.wait, KILL_WAIT_S)
        for t in self._threads:
            await asyncio.to_thread(t.join, 2.0)
        for stream in (self.proc.stdout, self.proc.stderr):
            with contextlib.suppress(OSError):
                if stream is not None:
                    stream.close()


# --------------------------------------------------------------------------- parsing helpers


def parse_time(value: Any) -> datetime | None:
    """A CLI timestamp: epoch seconds or milliseconds (number or digits), or ISO 8601 (no offset:
    UTC). None when missing or unreadable."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if re.fullmatch(r"\d+(?:\.\d+)?", text):
            value = float(text)
        else:
            try:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            return (dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)).astimezone(UTC)
    if isinstance(value, int | float):
        seconds = float(value) / 1000 if float(value) > 1e11 else float(value)
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    return max(int(value), 0)


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def parse_reading(info: Mapping[str, Any], observed_at: datetime) -> RateLimitReading:
    """`rate_limit_event.rate_limit_info` -> the usage reading of the guard. Overage: the call is
    billed as extra usage (`isUsingOverage`), or the plan refuses while `overageStatus` allows
    (so the call goes through on extra usage)."""
    windows = info.get("unifiedWindows")
    windows = windows if isinstance(windows, Mapping) else {}

    def window(name: str) -> UsageWindow | None:
        w = windows.get(name)
        if not isinstance(w, Mapping):
            if info.get("rateLimitType") == name and "utilization" in info:
                w = info  # an older shape: the one window at the top level
            else:
                return None
        return UsageWindow(
            utilization=_float(w.get("utilization")),
            resets_at=parse_time(w.get("resetsAt")),
            status=_str(w.get("status")),
        )

    status = _str(info.get("status")) or "unknown"
    overage_status = _str(info.get("overageStatus"))
    refusing = status not in ALLOWED_STATUSES | {"unknown"}
    overage = info.get("isUsingOverage") is True or (
        refusing and overage_status in ALLOWED_STATUSES
    )
    return RateLimitReading(
        observed_at=observed_at,
        status=status,
        resets_at=parse_time(info.get("resetsAt")),
        five_hour=window("five_hour"),
        seven_day=window("seven_day"),
        overage=overage,
        overage_status=overage_status,
        raw=dict(info),
    )


def limit_resets_at(reading: RateLimitReading | None) -> datetime | None:
    """When a refused call may go again (`RateLimitReading.refusal_resets_at`); None: the
    guard's `limit_retry_s` (a short 429 while the plan's windows still allow)."""
    return None if reading is None else reading.refusal_resets_at()


def _limit_epoch(text: str) -> datetime | None:
    m = _LIMIT_EPOCH.search(text)
    return None if m is None else parse_time(m.group(1))


# --------------------------------------------------------------------------- one run


@dataclass
class _Run:
    """What one CLI run printed."""

    init: dict[str, Any] | None = None
    first_event: str | None = None  # the type/subtype of the first JSON event
    rate_limit_info: dict[str, Any] | None = None
    reading: RateLimitReading | None = None
    retries: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] | None = None
    events: Counter[str] = field(default_factory=Counter)
    non_json: list[str] = field(default_factory=list)
    exit_code: int | None = None
    stderr: str = ""

    @property
    def version(self) -> str | None:
        return None if self.init is None else _str(self.init.get("claude_code_version"))

    @property
    def session_id(self) -> str | None:
        for src in (self.result, self.init):
            if src is not None and _str(src.get("session_id")):
                return src["session_id"]
        return None


@dataclass(frozen=True, slots=True)
class AuthStatus:
    """`claude auth status` (JSON) as the judge saw it at start."""

    auth_method: str | None
    logged_in: bool | None
    login_expires_at: datetime | None
    config_directory: str | None
    raw: Mapping[str, Any]


class ClaudeCliModel:
    """`JudgeModel` over the Claude Code CLI (see the module docstring). `command` is the argv
    prefix of the executable (default `[claude_path]`; tests pass a fake: `[python, script]`).
    `off`: why the adapter switched itself off (None: on); it then starts nothing."""

    def __init__(
        self,
        *,
        command: Sequence[str] = ("claude",),
        model: str = CLAUDE_MODEL,
        effort: str | None = None,
        oauth_token: str | None = None,
        config_dir: Path = DEFAULT_CLAUDE_CONFIG_DIR,
        timeout_s: float = 180.0,
        max_retries: int = 2,
        max_budget_usd: float | None = 0.50,
        system_prompt_file: bool = True,
        allow_overage: bool = False,
        clock: Clock = utc_now,
    ) -> None:
        self.command = list(command)
        self._model = model
        self.effort = effort
        self.timeout_s = timeout_s
        self.max_budget_usd = max_budget_usd
        self.system_prompt_file = system_prompt_file
        self.allow_overage = allow_overage
        self.config_dir = check_config_dir(config_dir)  # ValueError: never the user's config
        self.clock = clock
        self._oauth_token = oauth_token or None
        self.env = child_env(self.config_dir, oauth_token=oauth_token, max_retries=max_retries)
        self.auth: AuthStatus | None = None
        self.cli_version: str | None = None
        self.credential_expires_at: datetime | None = None
        self.calls = 0
        self.off: str | None = None
        self.off_since: datetime | None = None
        self._warned_expiry: datetime | None = None

    @classmethod
    def from_settings(
        cls, settings: JudgeSettings, *, command: Sequence[str] | None = None
    ) -> ClaudeCliModel:
        return cls(
            command=command if command is not None else (settings.claude_path,),
            model=settings.claude_model,
            effort=settings.claude_effort,
            oauth_token=settings.oauth_token,
            config_dir=settings.claude_config_dir,
            timeout_s=settings.claude_timeout_s,
            max_retries=settings.claude_max_retries,
            max_budget_usd=settings.claude_max_budget_usd,
            system_prompt_file=settings.claude_system_prompt_file,
            allow_overage=settings.claude_allow_overage,
        )

    # ------------------------------------------------------------------ JudgeModel

    @property
    def name(self) -> str:
        return self._model

    @property
    def options(self) -> Mapping[str, Any]:
        # what changes the answer (hashed with the request); not the budget cap, timeout,
        # retries or how the system prompt is passed
        return {"adapter": ADAPTER, "effort": self.effort, "max_turns": MAX_TURNS}

    def argv(self, request: JudgeRequest, system_file: Path | None) -> list[str]:
        """The CLI's arguments after the executable."""
        args = [
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            self._model,
            "--tools",
            "",
            "--strict-mcp-config",
            "--no-session-persistence",
            "--max-turns",
            str(MAX_TURNS),
        ]
        if system_file is not None:
            args += ["--system-prompt-file", str(system_file)]
        else:
            args += ["--system-prompt", request.system]
        # property order kept: the model fills `reasons` before it decides
        args += ["--json-schema", json.dumps(request.schema, separators=(",", ":"))]
        if self.effort is not None:
            args += ["--effort", self.effort]
        if self.max_budget_usd is not None:
            args += ["--max-budget-usd", f"{self.max_budget_usd:g}"]
        return args

    async def judge(self, request: JudgeRequest) -> JudgeResponse:
        if self.off is not None:
            raise ModelUnavailableError(
                f"the claude adapter is switched off: {self.off}", retry_after_s=OFF_RETRY_S
            )
        prepare_config_dir(self.config_dir)
        command = resolve_command(self.command, self.env)
        self.calls += 1
        started = time.monotonic()
        run = _Run()
        with tempfile.TemporaryDirectory(prefix=TMP_PREFIX, ignore_cleanup_errors=True) as tmp:
            base = Path(tmp)
            cwd = base / "cwd"  # empty: no project CLAUDE.md, settings or .mcp.json
            cwd.mkdir()
            system_file = None
            if self.system_prompt_file:
                system_file = base / "system.md"
                system_file.write_bytes(request.system.encode("utf-8"))
            argv = [*command, *self.argv(request, system_file)]
            try:
                proc = CliProcess(argv, env=self.env, cwd=cwd, stdin=request.user.encode("utf-8"))
            except OSError as exc:
                raise PermanentModelError(f"claude could not be started: {exc}") from exc
            try:
                await self._read(proc, run, started)
            except BaseException:
                await proc.kill()
                raise
            finally:
                await proc.close()
                run.stderr = self._redact(proc.stderr_tail())
        return self._response(run, time.monotonic() - started, argv)

    # ------------------------------------------------------------------ reading the stream

    async def _read(self, proc: CliProcess, run: _Run, started: float) -> None:
        """Reads until the result line or the end of output; an error raised here kills the tree
        (`judge`)."""
        deadline = started + self.timeout_s
        timed_out = f"claude timed out after {self.timeout_s:g} s: process tree killed"
        while True:
            try:
                line = await proc.next_line(deadline - time.monotonic())
            except TimeoutError:
                raise self._error(CallTimeoutError, timed_out, run, proc, started) from None
            if line is None:
                break
            self._event(line, run, proc, started)
            if run.result is not None:
                break
        # the result is in (or the output ended): the process should exit now
        grace = deadline - time.monotonic()
        if run.result is not None:
            grace = max(min(grace, EXIT_GRACE_S), 1.0)
        code = await proc.wait(grace)
        if code is None:
            if run.result is None:
                raise self._error(CallTimeoutError, timed_out, run, proc, started)
            log.warning("claude did not exit after its result: process tree killed")
            await proc.kill()
            code = await proc.wait(KILL_WAIT_S)
        run.exit_code = code

    def _error(
        self,
        cls: type[ModelError],
        message: str,
        run: _Run,
        proc: CliProcess,
        started: float,
        **kw: Any,
    ) -> ModelError:
        run.stderr = self._redact(proc.stderr_tail())
        return cls(
            self._redact(message),
            latency_s=time.monotonic() - started,
            request_id=run.session_id,
            rate_limit=run.reading,
            extras=self._extras(run, None, proc.argv),
            **kw,
        )

    def _switch_off(self, reason: str) -> None:
        if self.off is None:
            self.off, self.off_since = reason, self.clock()
            log.error(
                "claude adapter switched off: no further calls until the service restarts",
                reason=reason,
            )

    def _event(self, raw: bytes, run: _Run, proc: CliProcess, started: float) -> None:
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            return
        try:
            event = json.loads(text)
        except ValueError:
            if len(run.non_json) < NON_JSON_KEPT:
                run.non_json.append(self._redact(text[:500]))
            return
        if not isinstance(event, dict):
            return
        kind = str(event.get("type") or "?")
        subtype = event.get("subtype")
        name = f"{kind}/{subtype}" if isinstance(subtype, str) else kind
        run.events[name] += 1
        if run.first_event is None:
            run.first_event = name
            if name != "system/init":
                reason = (
                    f"claude's first event was {name!r}, not system/init: its auth source "
                    "(apiKeySource) can't be checked"
                )
                self._switch_off(reason)
                raise self._error(
                    ModelUnavailableError,
                    reason + " (process tree killed)",
                    run,
                    proc,
                    started,
                    called=True,
                    retry_after_s=OFF_RETRY_S,
                )
        if name == "system/init":
            run.init = event
            self._check_init(event, run, proc, started)
        elif name == "system/api_retry":
            retry = {
                k: event.get(k)
                for k in ("attempt", "max_retries", "retry_delay_ms", "error_status", "error")
            }
            run.retries.append(retry)
            log.warning("claude api retry", **retry)
        elif kind == "rate_limit_event":
            info = event.get("rate_limit_info")
            if isinstance(info, Mapping):
                run.rate_limit_info = dict(info)
                run.reading = parse_reading(info, self.clock())
                if run.reading.overage and not self.allow_overage:
                    resets = run.reading.refusal_resets_at() or run.reading.resets_at
                    log.error(
                        "claude reports extra usage (overage): call killed",
                        overage_status=run.reading.overage_status,
                        resets_at=_iso(resets),
                    )
                    raise self._error(
                        UsageLimitError,
                        "extra usage beyond the plan (overage) is not allowed "
                        f"(isUsingOverage {info.get('isUsingOverage')!r}, overageStatus "
                        f"{run.reading.overage_status!r}): process tree killed",
                        run,
                        proc,
                        started,
                        resets_at=resets,
                    )
        elif kind == "result":
            run.result = event

    def _check_init(
        self, init: Mapping[str, Any], run: _Run, proc: CliProcess, started: float
    ) -> None:
        source = init.get("apiKeySource")
        if source != NO_API_KEY:
            reason = (
                f"claude's apiKeySource is {source!r}, not {NO_API_KEY!r}: it would bill an API "
                "key instead of the plan"
            )
            self._switch_off(reason)
            raise self._error(
                ModelUnavailableError,
                reason + " (process tree killed before its answer)",
                run,
                proc,
                started,
                called=True,
                retry_after_s=OFF_RETRY_S,
            )
        version = _str(init.get("claude_code_version"))
        if version is not None and version != self.cli_version:
            if self.cli_version is not None:
                log.warning("claude version changed", old=self.cli_version, new=version)
            self.cli_version = version
        reported = init.get("model")
        if isinstance(reported, str) and reported and reported != self._model:
            log.warning("claude reports another model", requested=self._model, reported=reported)

    # ------------------------------------------------------------------ the outcome

    def _response(self, run: _Run, wall_s: float, argv: Sequence[str]) -> JudgeResponse:
        res = run.result
        extras = self._extras(run, wall_s, argv)
        base: dict[str, Any] = {
            "latency_s": wall_s,
            "request_id": run.session_id,
            "rate_limit": run.reading,
            "extras": extras,
        }
        if res is None:
            raise ModelError(
                f"claude ended without a result (exit {run.exit_code})"
                + (f": {run.stderr[-600:]}" if run.stderr else ""),
                **base,
            )
        self._note_credential(res.get("credential_expires_at"))
        subtype = res.get("subtype")
        latency = _float(res.get("duration_ms"))
        u = res.get("usage") if isinstance(res.get("usage"), Mapping) else {}
        usage = Usage(
            input_tokens=_int(u.get("input_tokens")) + _int(u.get("cache_creation_input_tokens")),
            cached_input_tokens=_int(u.get("cache_read_input_tokens")),
            output_tokens=_int(u.get("output_tokens")),
        )
        base |= {
            "latency_s": latency / 1000 if latency is not None else wall_s,
            "usage": usage,
            "cost_usd": _float(res.get("total_cost_usd")),
        }
        text = self._redact(res.get("result") if isinstance(res.get("result"), str) else "")
        failed = bool(res.get("is_error")) or subtype != "success"
        structured = res.get("structured_output")
        if structured is not None:  # the answer, whatever the run's subtype
            if failed:
                log.warning("claude's result has an answer but a failure subtype", subtype=subtype)
            return self._answer(json.dumps(structured, ensure_ascii=False), run, base)
        if not failed:
            resets = _limit_epoch(text)
            if resets is not None:
                raise UsageLimitError(f"claude: {text[:200]}", resets_at=resets, **base)
            log.warning("claude's result has no structured_output", result=text[:200])
            return self._answer(text, run, base)
        detail = f"{subtype or 'error'}: {text[:300]}".strip()
        if run.stderr:
            detail += f" | stderr: {run.stderr[-400:]}"
        if self._auth_failed(res, run, text):
            reason = f"claude's credentials were rejected ({detail[:300]})"
            self._switch_off(reason)
            raise ModelUnavailableError(reason, called=True, retry_after_s=OFF_RETRY_S, **base)
        if self._usage_limited(res, run, text):
            raise UsageLimitError(
                f"claude refused for a usage or rate limit ({detail})",
                resets_at=_limit_epoch(text) or limit_resets_at(run.reading),
                **base,
            )
        if subtype in ANSWER_SUBTYPES:  # an invalid answer: the resolver's one retry
            log.warning("claude ended without an answer", subtype=subtype)
            return self._answer(text, run, base)
        if subtype == BUDGET_SUBTYPE:
            raise PermanentModelError(
                f"claude stopped at its budget cap (--max-budget-usd {self.max_budget_usd:g}): "
                f"{detail}",
                **base,
            )
        raise ModelError(f"claude run failed (exit {run.exit_code}, {detail})", **base)

    def _answer(self, text: str, run: _Run, base: Mapping[str, Any]) -> JudgeResponse:
        if run.exit_code not in (0, None):
            log.warning("claude exited non-zero after its result", exit_code=run.exit_code)
        res = run.result or {}
        models = res.get("modelUsage")
        if isinstance(models, Mapping):
            other = sorted(str(m) for m in models if not str(m).startswith(self._model))
            if other:
                log.warning("claude used other models too", requested=self._model, other=other)
        return JudgeResponse(
            text=text,
            usage=base["usage"],
            latency_s=base["latency_s"],
            request_id=base["request_id"],
            cost_usd=base["cost_usd"],
            rate_limit=base["rate_limit"],
            extras=base["extras"],
        )

    @staticmethod
    def _auth_failed(res: Mapping[str, Any], run: _Run, text: str) -> bool:
        if res.get("api_error_status") in AUTH_STATUSES:
            return True
        if any(r.get("error") in AUTH_RETRY_ERRORS for r in run.retries):
            return True
        return bool(_AUTH_TEXT.search(text))

    @staticmethod
    def _usage_limited(res: Mapping[str, Any], run: _Run, text: str) -> bool:
        if res.get("api_error_status") == 429:
            return True
        if run.reading is not None and run.reading.status not in ALLOWED_STATUSES | {"unknown"}:
            return True
        if any(r.get("error") == "rate_limit" for r in run.retries):
            return True
        return bool(_LIMIT_TEXT.search(text))

    def _redact(self, value: Any) -> Any:
        """The token (and anything shaped like one) removed from texts kept or recorded."""
        if isinstance(value, str):
            if self._oauth_token and self._oauth_token in value:
                value = value.replace(self._oauth_token, REDACTED)
            return _TOKENISH.sub(REDACTED, value)
        if isinstance(value, Mapping):
            return {k: self._redact(v) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [self._redact(v) for v in value]
        return value

    def _extras(self, run: _Run, wall_s: float | None, argv: Sequence[str]) -> dict[str, Any]:
        shown = list(argv)
        if "--json-schema" in shown:  # the schema is in the request already
            i = shown.index("--json-schema")
            if i + 1 < len(shown):
                shown[i + 1] = "<request schema>"
        if "--system-prompt" in shown:
            i = shown.index("--system-prompt")
            if i + 1 < len(shown):
                shown[i + 1] = "<request system prompt>"
        return self._redact(
            {
                "claude_cli": {
                    "version": run.version,
                    "argv": shown[len(self.command) :],
                    "first_event": run.first_event,
                    "init": run.init,
                    "rate_limit_info": run.rate_limit_info,
                    "api_retries": run.retries,
                    "result": run.result,
                    "events": dict(run.events),
                    "non_json_stdout": run.non_json,
                    "exit_code": run.exit_code,
                    "wall_s": None if wall_s is None else round(wall_s, 3),
                    "stderr_tail": run.stderr or None,
                }
            }
        )

    # ------------------------------------------------------------------ auth + status

    async def check_auth(self) -> AuthStatus:
        """`claude auth status` with the call's environment: `authMethod` must be `oauth_token`
        or `claude.ai`, `apiProvider` `firstParty` when reported (`ClaudeAuthError` otherwise).
        No model request is made."""
        prepare_config_dir(self.config_dir)
        try:
            command = resolve_command(self.command, self.env)
        except PermanentModelError as exc:
            raise ClaudeAuthError(str(exc)) from exc
        out: list[bytes] = []
        with tempfile.TemporaryDirectory(prefix=TMP_PREFIX, ignore_cleanup_errors=True) as tmp:
            try:
                proc = CliProcess([*command, "auth", "status"], env=self.env, cwd=Path(tmp))
            except OSError as exc:
                raise ClaudeAuthError(f"claude could not be started: {exc}") from exc
            deadline = time.monotonic() + AUTH_TIMEOUT_S
            try:
                while (line := await proc.next_line(deadline - time.monotonic())) is not None:
                    out.append(line)
                code = await proc.wait(deadline - time.monotonic())
            except TimeoutError:
                raise ClaudeAuthError(
                    f"claude auth status timed out ({AUTH_TIMEOUT_S:g} s)"
                ) from None
            finally:
                await proc.close()
            stderr = self._redact(proc.stderr_tail())
        text = self._redact(b"".join(out).decode("utf-8", errors="replace").strip())
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise ClaudeAuthError(
                f"claude auth status printed no JSON (exit {code}): {(text or stderr)[:300]}"
            )
        method = _str(data.get("authMethod"))
        logged_in = data.get("loggedIn") if isinstance(data.get("loggedIn"), bool) else None
        status = AuthStatus(
            auth_method=method,
            logged_in=logged_in,
            login_expires_at=parse_time(data.get("loginExpiresAt")),
            config_directory=_str(data.get("configDirectory")),
            raw=data,
        )
        self.auth = status
        if method not in ACCEPTED_AUTH:
            raise ClaudeAuthError(
                f"claude auth status: authMethod {method!r} (exit {code}); the judge needs a "
                "subscription login: CLIPPER_JUDGE_CLAUDE_OAUTH_TOKEN from `claude setup-token` "
                "(authMethod oauth_token) or a login stored in claude_config_dir (claude.ai)"
            )
        provider = data.get("apiProvider")
        if provider is not None and provider != FIRST_PARTY:
            raise ClaudeAuthError(
                f"claude auth status: apiProvider {provider!r}, not {FIRST_PARTY!r} (a cloud "
                "provider would bill its account, not the plan)"
            )
        _, claude_dir = config_paths(self.config_dir)
        if status.config_directory is not None and not _same_path(
            status.config_directory, claude_dir
        ):
            log.warning(
                "claude uses another config directory than the judge's",
                expected=str(claude_dir),
                reported=status.config_directory,
            )
        log.info(
            "claude auth ok",
            auth_method=method,
            login_expires_at=_iso(status.login_expires_at),
            token=self._oauth_token is not None,
        )
        self._warn_expiry()
        return status

    def _note_credential(self, value: Any) -> None:
        expires = parse_time(value)
        if expires is not None:
            self.credential_expires_at = expires
            self._warn_expiry()

    def expires_at(self) -> datetime | None:
        """The credential's end: the token's (`credential_expires_at` of the last result), else
        the stored login's (`loginExpiresAt`)."""
        if self.credential_expires_at is not None:
            return self.credential_expires_at
        return None if self.auth is None else self.auth.login_expires_at

    def warnings(self) -> dict[str, str]:
        out: dict[str, str] = {}
        if self.off is not None:
            out["model_off"] = self.off
        expires = self.expires_at()
        if expires is not None and expires - self.clock() < CREDENTIAL_WARN:
            out["credential_expiring"] = expires.isoformat()
        return out

    def _warn_expiry(self) -> None:
        expires = self.expires_at()
        if expires is None or expires == self._warned_expiry:
            return
        if expires - self.clock() >= CREDENTIAL_WARN:
            return
        self._warned_expiry = expires
        days = (expires - self.clock()).total_seconds() / 86400
        log.warning(
            "claude credential expires soon: run `claude setup-token` and update "
            "CLIPPER_JUDGE_CLAUDE_OAUTH_TOKEN",
            expires_at=expires.isoformat(),
            days_left=round(days, 1),
        )

    def status(self) -> dict[str, Any]:
        """For the heartbeat (`ReportsStatus`)."""
        return {
            "adapter": ADAPTER,
            "model": self._model,
            "effort": self.effort,
            "cli_version": self.cli_version,
            "auth_method": None if self.auth is None else self.auth.auth_method,
            "login_expires_at": None if self.auth is None else _iso(self.auth.login_expires_at),
            "credential_expires_at": _iso(self.credential_expires_at),
            "calls": self.calls,
            "off": self.off,
            "off_since": _iso(self.off_since),
            "warnings": self.warnings(),
        }


def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.isoformat()


def _same_path(a: str, b: Path) -> bool:
    try:
        pa = Path(os.path.expanduser(a)).resolve()
    except OSError:
        return False
    if WINDOWS:
        return str(pa).lower() == str(b).lower()
    return pa == b
