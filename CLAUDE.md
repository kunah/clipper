# Clipper

Automated Twitch clipper: chat spikes → record → transcribe → LLM judge/edit plan → render 9:16
→ review → publish. **Read `DESIGN.md` first**: it holds the full idea, workflow, decisions
and roadmap. When a decision changes or a roadmap step is finished, update `DESIGN.md`
(status line, roadmap, decision log).

## Layout

- `libs/core/` (`clipper_core`): the contract between services. Settings, `timeline` (UTC),
  `contracts` (priorities, job kinds, payloads), `models` (all tables), `jobs` (queue),
  `media` (time-range requests that raise priorities), `leases`, `heartbeat`, `storage`
  (S3 + key layout), `partitions` (daily chat partitions), `logs` (unified structured
  logging), `runtime` (entry helpers).
- `db/` (`clipper_db`): Alembic migrations + `clipper-migrate` one-off container.
- `services/<name>/`: one app per service (none yet; step 2 adds controller + chat-ingest).
- `tools/`: CLIs like backfill (later).
- `compose.yaml`: Postgres 18 + pgvector, SeaweedFS (S3), migrate, and services.

## Commands

```sh
uv sync --all-packages                      # dev env (Python 3.12, all workspace packages)
docker compose up -d postgres objectstore   # infra for dev/tests
docker compose up --build migrate           # migrations + partitions + bucket
uv run pytest                               # all tests (integration ones need compose infra)
uv run ruff check . --fix && uv run ruff format .
uv run alembic -c db/alembic.ini revision -m "add x"   # then write the migration by hand
```

uv was installed with winget; in an old shell it may not be on PATH yet (restart the shell).

## Rules

- Times are **timezone-aware UTC** only; build ranges with `timeline.Interval`.
- **Logging**: call `logs.setup_logging(settings)` once at process start, then
  `log = logs.get_logger(__name__)` and pass data as fields:
  `log.info("chunk saved", chunk_id=5)`, not f-strings and never `print`. Use
  `log_context(...)`/`bind_context(...)` for per-channel/per-job fields. Output is JSON lines in
  containers (CloudWatch/Loki-ready) and readable text in a terminal (`CLIPPER_LOG_FORMAT`).
- **One writer service per table**; contracts change only in `libs/core`.
- **Migrations are additive** and hand-written. `db/tests/test_migrations.py` fails if
  models and migrations differ.
- Enqueue jobs **in the same transaction** as the data they refer to; use
  `contracts.dedupe_key`. Raise priorities with `media.request_media`, never by deleting and
  re-adding jobs.
- Writing chat rows for a new day: call `partitions.ensure_chat_partitions` first.
- New service = `services/<name>/pyproject.toml` (depends on `clipper-core` with
  `{ workspace = true }`) + `Dockerfile` following `db/Dockerfile` + add to workspace
  `members` + add to `compose.yaml`. Async entry via `clipper_core.runtime.run`.

## This PC (Windows) gotchas

- Use `127.0.0.1`, **not `localhost`** (IPv6 attempt stalls connections).
- Clipper's Postgres is on host port **5433**. Port 5432 belongs to another project's
  container (`nut_be-db-1`). Don't touch it.
- psycopg async needs a selector event loop on Windows (`runtime.run` and conftest handle it).
- PowerShell 5.1 `Set-Content -Encoding utf8` writes a BOM that breaks TOML. Use the
  Edit/Write tools or bash for file edits.
- Docker's disk image is on `D:\DockerDesktopWSL`. GPU works in containers (RTX 4060).
- MinIO is not an option (no community images since Oct 2025); object storage is SeaweedFS.
