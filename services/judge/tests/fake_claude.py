"""A fake `claude` executable for the judge's tests (the real CLI is never run).

    python fake_claude.py <scenario.json> <claude arguments...>

The scenario (JSON, written by the test) says what to print:

    {"record_dir": "...",
     "auth": {...}, "auth_exit": 0,            # `claude auth status`
     "runs": [{"steps": [...], "exit": 0, "heartbeat": false}, ...]}

Call n (0-based, counted by the records in `record_dir`) plays `runs[min(n, len - 1)]`; its steps:
`{"line": obj}` (one stream-json line), `{"raw": text}` (a non-JSON line), `{"stderr": text}`,
`{"sleep": s}`. Every call is recorded in `record_dir/call-<n>.json` (argv, environment, cwd and
its entries, stdin, the `--system-prompt-file` text), `auth status` in `auth-<n>.json`.
`heartbeat`: the fake and a child it starts (a grandchild of the test) each rewrite a counter file
every 50 ms (`fake-<n>.beat`, `child-<n>.beat`) for up to 20 s, both running before the first
step, so a test can see that the whole process tree was killed.
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

BEAT_S = 0.05
BEAT_FOR_S = 20.0
CHILD = (
    "import sys, time\n"
    "path, end, n = sys.argv[1], time.monotonic() + float(sys.argv[2]), 0\n"
    "while time.monotonic() < end:\n"
    "    n += 1\n"
    "    open(path, 'w').write(str(n))\n"
    "    time.sleep(0.05)\n"
)


def beat(path: Path) -> None:
    end, n = time.monotonic() + BEAT_FOR_S, 0
    while time.monotonic() < end:
        n += 1
        path.write_text(str(n))
        time.sleep(BEAT_S)


def out(text: str) -> None:
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def main() -> int:
    scenario = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    args = sys.argv[2:]
    rec = Path(scenario["record_dir"])
    rec.mkdir(parents=True, exist_ok=True)
    record = {
        "argv": args,
        "env": dict(os.environ),
        "cwd": os.getcwd(),
        "cwd_entries": sorted(os.listdir(".")),
    }
    if args[:2] == ["auth", "status"]:
        n = len(list(rec.glob("auth-*.json")))
        (rec / f"auth-{n}.json").write_text(json.dumps(record), encoding="utf-8")
        out(json.dumps(scenario.get("auth", {})))
        return int(scenario.get("auth_exit", 0))
    n = len(list(rec.glob("call-*.json")))
    record["stdin"] = sys.stdin.buffer.read().decode("utf-8")
    if "--system-prompt-file" in args:
        path = Path(args[args.index("--system-prompt-file") + 1])
        record["system_prompt"] = path.read_bytes().decode("utf-8")
    (rec / f"call-{n}.json").write_text(json.dumps(record), encoding="utf-8")
    runs = scenario["runs"]
    run = runs[min(n, len(runs) - 1)]
    if run.get("heartbeat"):
        threading.Thread(target=beat, args=(rec / f"fake-{n}.beat",), daemon=True).start()
        subprocess.Popen(
            [sys.executable, "-c", CHILD, str(rec / f"child-{n}.beat"), str(BEAT_FOR_S)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        beats = [rec / f"fake-{n}.beat", rec / f"child-{n}.beat"]
        end = time.monotonic() + 10
        while not all(b.exists() for b in beats) and time.monotonic() < end:
            time.sleep(0.01)  # both run before the first step
    for step in run.get("steps", ()):
        if "line" in step:
            out(json.dumps(step["line"]))
        elif "raw" in step:
            out(step["raw"])
        elif "stderr" in step:
            sys.stderr.write(step["stderr"] + "\n")
            sys.stderr.flush()
        elif "sleep" in step:
            time.sleep(step["sleep"])
    return int(run.get("exit", 0))


if __name__ == "__main__":
    sys.exit(main())
