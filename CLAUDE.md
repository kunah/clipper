# Clipper

Automated Twitch clipper: chat spikes → record → transcribe → LLM judge/edit plan → render 9:16
→ review → publish. **Read `DESIGN.md` first**: it holds the full idea, workflow, decisions
and roadmap. When a decision changes or a roadmap step is finished, update `DESIGN.md`
(status line, roadmap, decision log).

## Layout

- `libs/core/` (`clipper_core`): the contract between services. Settings, `timeline` (UTC),
  `contracts` (priorities, job kinds, payloads), `models` (all tables), `jobs` (queue;
  `RetryLater` defers a job without using an attempt), `media` (time-range requests that raise
  priorities), `leases`, `heartbeat`, `storage`
  (S3 + key layout), `partitions` (daily chat partitions), `logs` (unified structured
  logging), `runtime` (entry helpers); shared by live services and backfill: `chunks`
  (`commit_chunk`: chunk + gaps + transcribe job), `chat` (chat rows, `SecondAggregator`,
  `ChatWriter`; chat filters shared by detector and judge: `normalised_text`, `is_command`,
  `is_mention`, `strip_links`, `user_key`, bots via `load_bots()` from the packaged
  `clipper_core/bots.toml`), `ffmpeg` (stream copies: audio out, fMP4 -> .ts).
- `libs/twitch/` (`clipper_twitch`): IRC parser + anonymous chat client, Helix API client
  (streams, users, `get_videos`, `get_video`, `get_clips`), third-party emote sets
  (7TV/BTTV/FFZ), live + VOD HLS playlist parsers (`hls`), VOD playlist URLs via streamlink and
  the CDN path's stream id (`vod`, extra `[vod]`), VOD chat replay + anonymous video metadata over
  the undocumented GQL API (`gql`), `TwitchSettings` (`CLIPPER_TWITCH_*`).
  Protocols + fakes for VOD/chat access; `tests/test_live_smoke.py` hits the real Twitch only with
  `CLIPPER_TWITCH_LIVE_TESTS=1` (marker `twitch_live`).
- `db/` (`clipper_db`): Alembic migrations + `clipper-migrate` one-off container.
- `services/controller/`: live status via Helix, streams, viewer snapshots, maintenance,
  and the `clipper-channels` CLI. `services/chat-ingest/`: chat logger + per-second stats;
  PRIVMSG tags -> `chat_messages.first_message`/`highlighted`/`reward_id`/`msg_type` (NULL =
  unknown, e.g. backfill; needs migration 0006, applied alone before redeploying chat-ingest; the
  backfill doesn't need it).
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
  `services/detector/` (`clipper_detector`, DESIGN §18). Pure part (no DB, no clock): `params`
  (`DetectorParams`, optional TOML, `detector_version`), `lexicon` + versioned
  `data/lexicon.toml` (emote/word categories, per-channel overrides; bots from core), `scorer` (stage 1:
  `ChannelScorer.step(second)` -> suspects/episodes), `stage2` (raw chat -> shares, clean uniques,
  moment type, fake-spike rules), `merge` (episode -> candidate draft: delay, window, cooldown;
  no hourly cap), `explicit` ("clip it" mentions, viewer clips -> signals), `pipeline` (scorer + stage 2
  + merge over in-memory rows), `synthetic` (seeded test chat). Live service `clipper-detector`:
  `service.LiveDetector` (loops with an injectable clock, leases `detect:channel:{id}`, restart
  replay), `reads`/`writes` (one transaction per candidate: row + media request + judge job),
  `settings` (`CLIPPER_DETECTOR_*`: `MODE` live|offline|all, offline not built yet; `JUDGE_JOBS`
  default 0 until the judge exists). Viewer clips only with `CLIPPER_TWITCH_*` credentials.
  Compose service `detector` (profile `detector`). Needs migration 0004. `pipeline.run_stream`
  mirrors the live write dedupe (± `dedupe_s`, dropped drafts in `StreamRun.duplicates`), so it
  equals an uninterrupted live run without explicit signals. Backtest (build item 4, read-only):
  `backtest` (pure: truth = top 10 ∪ ≥ p75 viewer clips, chat coverage cut at the broadcast's
  end, hit@k vs a chance level, clip and moment recall@k, delay check, false suppressions,
  `--grid` combos, table/JSON) + `backtest_cli` (`clipper-detector backtest`: the live read
  helpers -> `run_stream`; engine with `default_transaction_read_only`; `--export-drafts FILE`:
  every draft of the base params as JSON, no scoring, for the judge's eval labelling).
  `services/judge/` (`clipper_judge`, DESIGN §19). Pure part (6b): `params` (`JudgeParams`),
  `inputs` (`JudgeInputs`, one round in memory; `Frame` stills), `transcript` (sentences `S1…`, snapping
  lookups), `answer` (`JudgeAnswer`, the model's schema), `prompt` (`build_request` -> versioned,
  byte-stable `JudgeRequest` + `request_hash`, `retry_request`; prompt text in `prompts/v{N}/`;
  a `$frames` section in `user.md` (v2) adds the stills as `JudgeRequest.images`, v1 has none),
  `resolver` (answer or `PlanPatch` -> repaired `EditPlan`, reject, context request; repair
  codes), `samples` (fixture candidate). Goldens in `tests/fixtures/`
  (`CLIPPER_JUDGE_UPDATE_GOLDEN=1` rewrites them); tests pin the prompt files and the input format
  (a used `prompts/v{N}/` is frozen: add the next one). Service `clipper-judge` (6c): `model`
  (`JudgeModel` protocol, `JudgeResponse`, `ModelError`/`UsageLimitError`/`PermanentModelError`),
  `fakes` (`FakeJudgeModel` rules, `ScriptedJudgeModel`), `claude_cli` (6d: `ClaudeCliModel`,
  headless `claude -p` stream-json on the user's subscription; child env built from scratch, no
  `ANTHROPIC_*`/`CLAUDE_CODE_*` inherited; first event `system/init` with `apiKeySource` `none`,
  else (or on 401/403) the adapter switches itself off and jobs are held; overage (extra usage)
  kills the call and latches the guard unless `claude_allow_overage`; Popen +
  threads, not asyncio subprocesses (Windows selector loop); `check_auth` = `claude auth
  status`), `adapters`
  (`make_model`, `settings_for_model`: concurrency 1 for the CLI, `check_model`), `settings`
  (`CLIPPER_JUDGE_*`, `MODEL=fake|claude-cli`, `CLAUDE_*`; usage guard caps unset = off), `reads`/
  `writes`, `calls` (`judge_calls` cache by request hash, committed alone + S3 `judge_io`),
  `guard` (usage guard), `waiter` (LISTEN `clipper_transcripts` + 30 s sweep, pure `readiness`/
  deadlines), `rounds` (`JudgeHandler`: one judge job advances one clip), `revisions`
  (`ReviseHandler`: patches -> v+1 + render job; `RevisionSource`, `review_events` not built
  yet), `service` (`JudgeService`: N job runners on `judge` + `revise_plan`, waiter, heartbeat).
  `frames` (`frame_times` + `FrameGrabber`: JPEG stills from the chunks via ffmpeg; eval only so
  far, `clipper-judge eval --frames N`; the CLI adapter sends images as `--input-format
  stream-json`, `claude_cli.stdin_bytes`),
  `evalset` (eval fixtures: one round's `JudgeInputs` as JSON, labels TOML/JSON, detector drafts
  as cases) + `evaluate` (`clipper-judge eval`: read-only DB or fixtures, `--drafts` -> agreement,
  repairs, latency, usage), `label` + `label_page.html` (`clipper-judge label prepare|serve`: blind
  labelling set, videos cut from the chunks, a 127.0.0.1 page writing `labels.json`; sets live in
  the gitignored `data/labels/`). `ideas` (`clipper-judge ideas`, read-only trial of post-stream
  clips: one call over a whole stream's timeline, prompt `prompts/ideas-v{N}/` + its `PromptSpec`
  in `ideas.SPECS` (frozen once used; v2: editor requests from `--requests`, e.g.
  `data/labels/v1/requests.json` built from the label notes; pause markers; v3 default: `insert`
  beats, chat peak seconds, hooks optional), writes input.md, response.json, ideas.md and `--previews` cuts (`place_beats`: edges
  out to the words a beat's `what` quotes, a hook cut to its quote, else snapped to speech breaks;
  pauses cut out by `tighten` unless `--no-tighten`) into the gitignored `data/ideas/<stream>/<prompt>/`; a saved
  answer is reused, no call) + `ideas_rate` + `ideas_rate_page.html` (`clipper-judge rate-ideas`: a
  127.0.0.1 page to rate every run's ideas publish / with changes / no + a note, and what each
  run missed; writes `data/ideas/ratings.json`). Shadow runs (`CLIPPER_JUDGE_SHADOW`, `adapters.make_shadows`,
  `rounds`: same inputs, `shadow` rows, best effort); operator-instruction revisions
  (`prompts/v1/revise.md`, `prompt.build_revision_request`, `resolver.resolve_instruction`); the
  heartbeat queues `notify` jobs for `credential_expiring` (`CLIPPER_JUDGE_CLAUDE_TOKEN_EXPIRES`:
  a setup token reports no expiry) and `model_off`.
  Compose service `judge` (profile `judge`; the image pins the native Claude Code CLI 2.1.330 by
  sha256, runs as uid 10001, volume `judge-claude` = the CLI's own HOME). `render` jobs run on the
  renderer (profile `renderer`), `notify` jobs queue until review exists. Integration tests: `tests/conftest.py` (`world` fixture;
  `fake_claude` = `tests/fake_claude.py`, a fake executable: tests never run the real CLI).
  Depends on core only.
  `services/renderer/` (`clipper_renderer`, DESIGN §20). Pure: `profiles` (`short_1080x1920`,
  `test_270x480`), `timemap` (plan ranges -> per-chunk trims, jitter fill, gap cuts, teaser first;
  runs map plan times to output), `layout` (+ `config/layouts.toml`, read per render), `styles`
  (PROPOSED looks; bundled OFL Montserrat in `fonts/`, `fontset`), `captions` (ASS builder),
  `graph` (both ffmpeg commands; pieces seek 5 s early and trim by pts: ffmpeg's TS seek lands a
  GOP late; loudness measured with `ebur128`, applied by `loudnorm linear=true`), `samples`,
  `synthetic` (lavfi chunks: luma = frame index, 1 kHz beeps). `media` (ffmpeg/ffprobe runner),
  `library` (asset manifest + sha256 + licences, per-asset normalisation), `render`
  (`render_files`: one render from files on disk), `handler` (the `render` job: pin by S3 copy,
  download, words, render, upload, row `done` + `review` job; `unrenderable` no_video /
  video_expired; last attempt -> `failed` + `notify(render_failed)`), `service`, `settings`
  (`CLIPPER_RENDERER_*`), `main` (`clipper-renderer`, `layout-preview` (natively: this checkout's
  `config/layouts.toml` unless `--layouts`/`CLIPPER_RENDERER_LAYOUTS_PATH`), `assets check`). Goldens
  in `tests/fixtures/` (`CLIPPER_RENDERER_UPDATE_GOLDEN=1` rewrites them); tests need ffmpeg and
  ffprobe on PATH. Compose service `renderer` (profile `renderer`, GPU; image with jellyfin-ffmpeg
  8.1.3-2 static by sha256). Needs migration 0007 (`renders`). Depends on core only.
- `tools/backfill/` (`clipper_backfill`, `clipper-backfill` CLI): past VODs in the live format
  (DESIGN §17). A `vod` run: media + time (T0 estimator, chunk planner, `audio_only` by default,
  muted parts -> `muted` gaps, priority-0 transcribe jobs, resume from `vods.media_done_s`), then
  the chat replay into `chat_messages`/`chat_metrics_1s` (same `SecondAggregator` as live, resume
  from `vods.chat_done_s`/`chat_metrics_to`, skipped where the channel has live chat), then viewer
  clips into `viewer_clips` (Helix credentials, else skipped). `vod <id> --fill-gaps`: an ended
  stream WE recorded live instead gets its `not_recorded` gaps + unrecorded tail/head filled from
  the VOD (`planner.fill_ranges`/`plan_fill`, T0 measured on the live audio by `fill` +
  `audio_match`; chunks `source='backfill'` with seq >= `contracts.FILL_SEQ_BASE`, no gap rows,
  chat only in seconds without live chat; one `vods` row referencing the live stream; live rows
  never touched; not ours / backfilled / still live / an untrusted T0 -> exit 2, see `--dry-run`,
  then `--accept-t0 audio|pdt`). `clips`, `calibrate` (read-only:
  VOD vs our live recording), `status`, `channel` (list only: backfill runs only on a named VOD).
  Needs ffmpeg on PATH natively (`CLIPPER_BACKFILL_*`); compose service `backfill` (profile
  `tools`, `docker compose run --rm`).
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
uv run clipper-backfill vod <id> --max-minutes 5      # media + chat, resumable
uv run clipper-backfill channel <login>               # lists archives + what we have, no writes
uv run clipper-backfill vod <id> --no-media --chat-max-minutes 30   # chat only (no object storage)
uv run clipper-backfill clips <login|id>              # viewer clips (Helix credentials)
uv run clipper-backfill calibrate <id>                # read-only: VOD vs our live recording of it
uv run clipper-backfill vod <id> --fill-gaps   # fill a live stream's not_recorded gaps from its VOD
docker compose up -d --build controller chat-ingest recorder
docker compose up -d --build transcriber    # GPU (profile gpu, enabled by naming it); first start
                                            # downloads ~1.5 GB of weights into the `models` volume
uv run clipper-detector                     # live detector natively (CLIPPER_DETECTOR_JUDGE_JOBS=0)
uv run clipper-detector backtest --stream 803 [--json]   # read-only: candidates vs viewer clips
uv run clipper-detector backtest --channel x --since 2026-11-01 --grid z_open=3.5,4,5
docker compose up -d --build detector       # profile detector, enabled by naming it
docker compose up -d --build judge          # profile judge (CLIPPER_JUDGE_MODEL, fake by default);
                                            # claude-cli needs CLIPPER_JUDGE_CLAUDE_OAUTH_TOKEN in .env
uv run clipper-judge eval --labels labels.toml [--report r.json --csv r.csv]   # read-only, fake model
uv run clipper-judge eval --fixtures dir/ --labels labels.toml --model claude-cli --yes   # your plan
uv run clipper-detector backtest --stream 2415 --params loose.toml --export-drafts drafts.json
uv run clipper-judge label prepare --stream 2415 --drafts drafts.json --out data/labels/v1
uv run clipper-judge label serve data/labels/v1 --open      # http://127.0.0.1:8765, blind labels
uv run clipper-judge eval --labels data/labels/v1/labels.json --drafts data/labels/v1/drafts.json
uv run clipper-judge eval --labels … --drafts … --prompt v2 [--frames 8] --model claude-cli --yes  # stills, ffmpeg
uv run clipper-judge ideas --stream 1412 [--requests data/labels/v1/requests.json] [--model claude-cli --yes] [--previews]
uv run clipper-judge rate-ideas --open      # http://127.0.0.1:8766, rate the ideas -> data/ideas/ratings.json
docker compose up -d --build renderer       # profile renderer (GPU; needs migration 0007)
uv run clipper-renderer layout-preview ohnepixel --at 2026-11-23T20:15:00Z   # data/layout-preview/
uv run clipper-renderer assets check --dir data/assets   # the licensed SFX/music library
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
