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
  logging), `runtime` (entry helpers); shared by live services and backfill: `chunks`
  (`commit_chunk`: chunk + gaps + transcribe job), `chat` (chat rows, `SecondAggregator`,
  `ChatWriter`), `ffmpeg` (stream copies: audio out, fMP4 -> .ts).
- `libs/twitch/` (`clipper_twitch`): IRC parser + anonymous chat client, Helix API client
  (streams, users, `get_videos`, `get_video`, `get_clips`), third-party emote sets
  (7TV/BTTV/FFZ), live + VOD HLS playlist parsers (`hls`), VOD playlist URLs via streamlink and
  the CDN path's stream id (`vod`, extra `[vod]`), VOD chat replay + anonymous video metadata over
  the undocumented GQL API (`gql`), `TwitchSettings` (`CLIPPER_TWITCH_*`).
  Protocols + fakes for VOD/chat access; `tests/test_live_smoke.py` hits the real Twitch only with
  `CLIPPER_TWITCH_LIVE_TESTS=1` (marker `twitch_live`).
- `db/` (`clipper_db`): Alembic migrations + `clipper-migrate` one-off container.
- `services/controller/`: live status via Helix, streams, viewer snapshots, maintenance,
  and the `clipper-channels` CLI. `services/chat-ingest/`: chat logger + per-second stats.
  `services/recorder/`: HLS -> chunks (local spool -> S3), gaps, transcription jobs; needs
  ffmpeg on PATH when run natively (`CLIPPER_RECORDER_*` settings).
  `services/transcriber/`: GPU worker for `transcribe` jobs (Silero VAD gate -> faster-whisper
  large-v3-turbo -> sentences + provisional tails, CED sound classes -> per-second audio metrics;
  `CLIPPER_TRANSCRIBER_*`). faster-whisper is an optional extra (`whisper`) that only the CUDA
  image installs; CED runs as an ONNX graph on onnxruntime-gpu (CUDA 12 build, the transcriber's
  onnxruntime on Linux; Windows gets the CPU package), exported at image build time from pinned
  weights by `services/transcriber/ced_export/export_onnx.py` (the build checks its sha256). No
  torch in the image (only the export build stage uses CPU torch). Natively the service needs
  `CLIPPER_TRANSCRIBER_ENGINE=fake` and `CLIPPER_TRANSCRIBER_TAGGER=none` (or `fake`) set
  explicitly, and tests use the fakes.
  `services/detector/` (`clipper_detector`, DESIGN §18): so far the pure part, no DB, no clock:
  `params` (`DetectorParams`, optional TOML, `detector_version`), `lexicon` + versioned
  `data/lexicon.toml` (emote/word categories, bots, per-channel overrides), `scorer` (stage 1:
  `ChannelScorer.step(second)` -> suspects/episodes), `stage2` (raw chat -> shares, clean uniques,
  moment type, fake-spike rules), `merge` (episode -> candidate draft: delay, window, cooldown,
  budget), `pipeline` (the three over in-memory rows), `synthetic` (seeded test chat). No
  Dockerfile/compose service yet (build item 3).
- `tools/backfill/` (`clipper_backfill`, `clipper-backfill` CLI): past VODs in the live format
  (DESIGN §17). A `vod` run: media + time (T0 estimator, chunk planner, `audio_only` by default,
  muted parts -> `muted` gaps, priority-0 transcribe jobs, resume from `vods.media_done_s`), then
  the chat replay into `chat_messages`/`chat_metrics_1s` (same `SecondAggregator` as live, resume
  from `vods.chat_done_s`/`chat_metrics_to`, skipped where the channel has live chat), then viewer
  clips into `viewer_clips` (Helix credentials, else skipped). `clips`, `calibrate` (read-only:
  VOD vs our live recording), `status`. Needs ffmpeg on PATH natively (`CLIPPER_BACKFILL_*`);
  compose service `backfill` (profile `tools`, `docker compose run --rm`).
  `tools/asr-bench/` is outside the workspace (own Dockerfile).
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
uv run clipper-backfill vod <id|url> --dry-run         # reads only; prints T0 + plan
uv run clipper-backfill vod <id> --max-minutes 5      # media + chat, resumable; status | channel <login>
uv run clipper-backfill vod <id> --no-media --chat-max-minutes 30   # chat only (no object storage)
uv run clipper-backfill clips <login|id>              # viewer clips (Helix credentials)
uv run clipper-backfill calibrate <id>                # read-only: VOD vs our live recording of it
docker compose up -d --build controller chat-ingest recorder
docker compose up -d --build transcriber    # GPU (profile gpu, enabled by naming it); first start
                                            # downloads ~1.5 GB of weights into the `models` volume
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
  in containers (RTX 4060). The transcriber image is ~10 GB as Docker counts it (CUDA base layers
  shared with the asr-bench image). Its build exports CED to ONNX in a throwaway stage with CPU
  torch (needs Hugging Face Hub access, ~345 MB; only the 173 MB .onnx stays in the image).
  `uv lock` and the transcriber's `docker build` need ORT's CUDA 12 package feed
  (aiinfra.pkgs.visualstudio.com, in the transcriber's pyproject and uv.lock) besides PyPI.
- MinIO is not an option (no community images since Oct 2025); object storage is SeaweedFS.
