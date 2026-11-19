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
- `libs/twitch/` (`clipper_twitch`): IRC parser + anonymous chat client, Helix API client,
  third-party emote sets (7TV/BTTV/FFZ), live HLS playlist parser (`hls`), `TwitchSettings`
  (`CLIPPER_TWITCH_*`).
- `db/` (`clipper_db`): Alembic migrations + `clipper-migrate` one-off container.
- `services/controller/`: live status via Helix, streams, viewer snapshots, maintenance,
  and the `clipper-channels` CLI. `services/chat-ingest/`: chat logger + per-second stats.
  `services/recorder/`: HLS -> chunks (local spool -> S3), gaps, transcription jobs; needs
  ffmpeg on PATH when run natively (`CLIPPER_RECORDER_*` settings).
  `services/transcriber/`: GPU worker for `transcribe` jobs (Silero VAD gate -> faster-whisper
  large-v3-turbo -> sentences + provisional tails, CED sound classes -> per-second audio metrics;
  `CLIPPER_TRANSCRIBER_*`). The real model libraries are optional extras (`whisper`, `ced`) that
  only the CUDA image installs; natively the service needs `CLIPPER_TRANSCRIBER_ENGINE=fake` and
  `CLIPPER_TRANSCRIBER_TAGGER=none` (or `fake`) set explicitly, and tests use the fakes.
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
uv run clipper-channels add xqc --mode audio_only      # list | mode <login> <mode> | enable | disable
docker compose up -d --build controller chat-ingest recorder
docker compose up -d --build transcriber    # GPU (profile gpu, enabled by naming it); first start
                                            # downloads ~2 GB of weights into the `models` volume
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
- Docker's disk image is on `E:\DockerDesktopWSL` (moved off the slow D: HDD on 2026-11-19). GPU works
  in containers (RTX 4060). The transcriber image is ~10 GB as Docker counts it (7.3 GB of CUDA base
  layers shared with the asr-bench image). Its torch is the CPU build (CUDA torch would add ~5 GB);
  CED speed is an open item (DESIGN §16).
- MinIO is not an option (no community images since Oct 2025); object storage is SeaweedFS.
