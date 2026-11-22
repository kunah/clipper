# Overnight run (2026-11-19), branch `transcriber`

Orchestrator session in the worktree `C:\Users\hkuna\developer\clips-transcriber`. Every task is
done by an Opus subagent, checked (ruff + full pytest), reviewed by a second agent, then committed.
Nothing is pushed or merged. This file is the resume point and the morning summary.

## Done

- Baseline: ruff clean, 107 tests passing.
- Step 4a (commit "Transcriber contract in core"): migration 0002 (`chunk_transcripts` ownership
  columns + CHECK, `transcript_segments.provisional` + index, new `audio_metrics_1s` + CHECK),
  contracts (`TRANSCRIPTS_NOTIFY_CHANNEL`, `Word`/`WordsFile` v1, `CHUNK_JITTER`), new
  `clipper_core.transcripts` (`words()`, `coverage()` with transcribed / provisional /
  untranscribed / gap / unrecorded). DESIGN.md §16 updated with the details. 123 tests.
- Step 4b-1 (commit "Transcriber pipeline"): `services/transcriber` package: ffmpeg decode to
  16 kHz mono f32, engine interfaces (`SpeechEngine` incl. batched `transcribe_many`,
  `SoundTagger`, `VoiceActivityDetector`), Silero VAD v6.2 via onnxruntime (no torch; bit-exact
  vs the reference), VAD gating, sentence split, junk filter, provisional tail, per-second
  metrics (a second belongs to the chunk containing ts + 0.5 s), fakes. `contracts.SoundGroup`;
  `transcript_segments.confidence` added to (undeployed) migration 0002. 221 tests.
- Step 4b-2 (commit "Transcriber job handler"): `plan_window` (takeover, capped takeover,
  re-take on retry, out-of-order, re-runs), window audio assembly, one locked transaction per
  stream (advisory lock, re-validation, words-file upload under the lock, segments, P update,
  audio metrics, NOTIFY), re-plan on HandoverConflict, `jobs.PermanentJobError` in core (broken
  files fail at once), service + heartbeat (queue depth by priority), `clipper-transcriber`
  entry. Contiguity tolerance = `CHUNK_JITTER` (0.5 s). A randomised handover model test
  (300 seeds) + DB tests for every ordering. 279 tests. Root ruff config excludes `*.md`.
- Step 4b-3 (commit "Transcriber: faster-whisper + CED adapters, CUDA image, compose"):
  `WhisperEngine` (large-v3-turbo, float16, beam 5, word timestamps), `CedTagger` (provisional,
  CPU torch), CUDA Dockerfile (`clipper/transcriber:dev`, Silero baked in), compose service
  `transcriber` (GPU reservation, `models` volume) — **built, NOT started**.
  **End-to-end run** (real models on GPU, throwaway DB + bucket, all 20 bench chunks laid out as
  5 contiguous streams; cleaned up afterwards): 20/20 jobs done first try, coverage complete,
  8 sentence takeovers at chunk cuts with no lost/duplicated words, 93.7 % word agreement with the
  benchmark's turbo output, 2.35 GB GPU peak. Speed: 13× realtime overall (Whisper 31× per gated
  stretch, CED on CPU 24× on 2 threads = the slowest part, VAD ~260×).
  `vad_filter=False` chosen (on: identical words, 3 % slower). Batched faster-whisper pipeline
  rejected (1.2× faster, changed 1–2 % of words). After review: model revisions pinned
  (turbo @0a363e9; CED label table checked at load, since its label CSV can't be pinned), GPU
  out-of-memory waits in-process up to 10 min (a game holding VRAM doesn't burn job attempts),
  all logs JSON (NVIDIA banner off, HF/transformers routed), Silero via `ADD --checksum`,
  compose `profiles: [gpu]` (a bare `docker compose up -d` does NOT start it). A cold start on a
  throwaway volume downloaded ~1.8 GB and worked online. 313 tests.

- Step 5a (commit "Backfill groundwork"): migration 0003 (`vods`, `viewer_clips`; CHECKs on
  `streams`: source in live/backfill, a backfilled stream is always `ended`, so recorder and
  chat-ingest can never pick one up), contracts (`GapReason.MUTED`, `T0Method`, VOD statuses,
  `backfill_lease`), shared `clipper_core.chunks.commit_chunk` / `clipper_core.chat` /
  `clipper_core.ffmpeg` (recorder + chat-ingest switched; behaviour reviewed line by line as
  identical), controller never updates/ends a backfilled stream, `hls.parse_vod_playlist`.
  337 tests. The live recorder/chat-ingest/controller containers still run the old code until
  you rebuild them (no need to hurry; nothing changes for them).

- Step 5b (commit "Twitch VOD access"): Helix `get_videos`/`get_clips`, `clipper_twitch.vod`
  (streamlink, optional extra `vod`; restricted vs gone told apart), `clipper_twitch.gql` (chat
  replay: anonymous, ≤ 2 req/s, retries on Twitch's transient errors) with fakes and fixtures.
  Smoke test (opt-in, `CLIPPER_TWITCH_LIVE_TESTS=1`) ran once on an xqc archive (~24 MB, deleted):
  VOD segments are 10 s MPEG-TS; **the `audio_only` playlist carries PDT** on every segment
  (= T0_pdt + offset, T0_pdt = createdAt − 3.2 s); muted = `N-muted.ts`; chat `createdAt` is UTC
  with ms, `contentOffsetSeconds` whole seconds, no drift over 11.6 h; the chat anchor sits ~4 s
  after T0_pdt (Twitch's replay compensates latency). Facts in DESIGN §3. 421 tests.

- Step 5c (commit "Backfill CLI: VOD media + time"): `tools/backfill` (`clipper-backfill vod
  <id> [--dry-run] [--max-minutes N] [--video [Q]] ...`, `status`, `channel` with Helix
  credentials). **T0 from the `audio_only` PDT** (`T0Method.PDT`; deviation from the §17 draft,
  which assumed no PDT: the chat anchor sits ~4 s later because Twitch's replay compensates
  latency, and using it would cancel the chat delay), chat anchor + GQL/Helix created_at as
  cross-checks with flags. ~60 s chunks exactly T0 + Σ EXTINF, `muted` gaps, priority-0
  transcribe jobs, lease + resume from `vods.media_done_s` (same transaction as each chunk),
  refuses a rendition switch that would overlap committed media. Real run on xqc archive
  2932113307 (5 min, throwaway DB/bucket, deleted): T0 by PDT (8343 points, 0 deviation), 7
  chunks, muted gaps, hard kill + resume OK, ~16 MB downloaded. compose `backfill` service
  (profile `tools`, not built). 503 tests.

- Step 5d (commit "Backfill: chat replay, viewer clips, calibrate"): chat replay →
  `chat_messages` / `chat_metrics_1s` through the same `SecondAggregator` as live (chat-ingest
  reviewed unchanged), watermark resume (no double counting), skipped where ≥ 60 s of live chat
  exists in the range; failed third-party emote sets recorded in `vods.flags` (FFZ is unreachable
  from this PC); viewer clips via Helix → `viewer_clips` (needs Twitch app credentials in this
  checkout's `.env`; tested with fakes only); `calibrate <video>` (read-only, audio
  cross-correlation vs our live recording) — **run it once yourself** on a stream we recorded
  live, to confirm the PDT T0. Real chat run: 2426 messages / 905 chatters / 215 metric seconds
  from 4 min of an xqc VOD, hard kill + resume without duplicates. **Step 5 done.** 531 tests.

- Transcriber: **CED on the GPU** (commit "Transcriber: CED on the GPU via ONNX fp16"; your
  decision, recipe from the main session): CED-base exported to ONNX fp16 in a Docker build
  stage from pinned weights (sha256 of the output checked; reproduced byte-identically twice;
  label table checked in the build), onnxruntime-gpu 1.29 (CUDA-12 build) in the image, CPU
  onnxruntime natively for Silero/tests, no torch in the image. Rerun of the GPU e2e (20 chunks):
  identical 1747 words, 93.7 % agreement, per-second classes within 0.003 of the CPU run; CED
  48 s → 0.84 s; **overall 13× → 29× realtime**; 2.60 GB GPU peak (Whisper + CED). Whisper now
  defaults to **temperature 0** (deterministic: 20/20 chunks identical across passes vs 2/20
  differing with the fallback, which once invented "we'll be right back"; same agreement,
  3–5 % faster; the fallback stays a setting). Image still 10.3 GB (venv smaller, but ORT's CUDA
  provider ≈ CPU torch in size). 539 tests.

- Detector (§18, proposed) unit 1 (commit "Detector contract: migration 0004"): tables
  `candidates` (UNIQUE stream/peak/source; CHECKs: source, status, one-way suppress reason,
  moment type, moment ⊂ window ∋ peak) and `chat_delay_estimates` (nullable delay unless valid);
  contracts `JobKind.JUDGE` / `DETECT_OFFLINE`, candidate enums, payloads, `dedupe_key(kind, id,
  *parts)` (backwards compatible), `detect_lease`; controller stale limits for the new job kinds.
  560 tests. Needs migration 0004 on the live DB before a detector runs.

- Detector unit 2 (commit "Detector scorer"): new `services/detector` package, pure code:
  `ChannelScorer` (10 s unique-chatter activity vs a 20-min rolling median, robust z + ratio,
  viewer normalisation, episodes; 16 µs per channel-second), stage 2 on raw chat (clean
  uniques, reaction/greeting/command/bot/vote/dup/deleted shares, fake-spike rules: raid, subs,
  bits, mode toggle, spam wave, commands, votes, copypasta, bots, greetings, `weak`), merge
  (chat delay, windows, cooldown fold-in, budget), versioned emote lexicon v1
  (`data/lexicon.toml`, to be tuned). Decisions beyond the §18 draft are listed as proposed rows
  in §18 (e.g. no scoring before ~6 min of own baseline). 717 tests.

- Detector unit 3 (commit "Detector live service"): `clipper-detector` follows
  chat_metrics_1s every second (final cutoff 10 s behind + data watermark, late rows re-checked),
  per-channel leases, stage 2 on raw chat, one transaction per candidate (row + TRIGGER media
  request + judge job when `CLIPPER_DETECTOR_JUDGE_JOBS=1`, off by default until the judge
  exists), explicit signals ("clip it", clip links by ≥ 2 posters, Helix clips with
  credentials), restart replay without double emits (3 review rounds; a seeded property test
  compares restarts/handover with an uninterrupted run: identical; a 240 s outage may differ
  in the outage window only). Dockerfile + compose service `detector` (profile `detector`, not
  built, not started). 752 tests.

- Judge (§19, proposed) unit 6a (commit "Judge contract"): migration 0005 (`clips`,
  `clip_plans`, `judge_calls`, `media_requests.clip_id` FK), the versioned `EditPlan` contract
  (UTC, ordered non-touching segments, hook/teaser/zooms inside segments, hashtags, word fixes),
  `PlanPatch` for review edits, job kinds RENDER/REVIEW/REVISE_PLAN/NOTIFY + payloads,
  `clipper_core.lifecycle` (derived §7 clip state), storage key for judge I/O. 880 tests.

- Judge unit 6b (commit "Judge input builder + resolver"): new `services/judge` package (pure):
  versioned, byte-stable LLM input (transcript with timing, chat summary, audio events, media
  ranges; chat/titles fenced and length-capped against prompt injection; prompt text in
  `prompts/v1/`, pinned by sha256) + request hash; `JudgeAnswer` schema; resolver: model answer
  or operator `PlanPatch` → repaired, valid `EditPlan` (never cuts inside a word, splits at video
  holes, durations, hashtags, hook/teaser/zooms…) or reject / context request / retry; context
  caps (≤ 2 extensions, ≤ 3 calls). Property test over words on holes, across video edges and
  overlapping words: 0 mid-word cuts (the old code had 43/277). Shared chat filters (bots,
  normalised text, "clip it", links, commands) moved from the detector into
  `clipper_core.chat` + `bots.toml` (detector re-exports them; lexicon v2). 1021 tests.

## Your answers (2026-11-21, relayed by the main session)

Recorded as decision rows in DESIGN §15 (dated 2026-11-21) and in the sections named:
1. CED-base: stays as is, provisional; experiments later. No action (§15).
2. Resolver, tiny video islands: keep spending the one retry instead of `no_video` (§15).
3. Backfill is never automatic: only a VOD/stream you name (`vod <id>`); `channel` becomes
   list-only (§15, §17). Checked §17: `channel <login>` is the only path that backfills without
   an explicit id (today it backfills the newest 5). `clips <login>` reads clip metadata only,
   `calibrate`/`status` are read-only, and nothing in §18/§19 starts a backfill. The code change
   to `channel` was made after you said resume (2026-11-21, see "Resumed").
4. Publishing: you decide what goes where and provide credentials + upload method per platform;
   no rights model beyond a per-route config you fill in; adapters wait (§2, §15, §22: the
   `RightsBasis` enum/CHECK is dropped from the draft, routes get an optional `note`, accounts an
   `upload_method`, e.g. `api` or `manual`).
5. Review on Discord instead of Telegram: §21 reworked (bot with buttons/selects/modals over the
   Gateway, no public endpoint; web UI unchanged). Discord's upload limit, checked 2026-11-21:
   **20 MiB per file by default** (bots too; raised from 10 MB in August 2026), more only with
   server boosts. §20's preview (~12 MB for 60 s, capped at 19 MB) fits, so there's no extra
   preview render; larger previews fall back to the thumbnail + web link.
6. Judge 6d: the Claude Code CLI on your subscription (§19 "Claude Code CLI adapter" + "Usage
   guard"). Verified on this PC (CLI 2.1.330) with two tiny Haiku calls in `-p` mode: the flags,
   the `result` fields, `--json-schema` → `structured_output` (takes 2 turns, so `--max-turns` ≥ 2), and
   a `rate_limit_event` in `stream-json` with your five-hour/seven-day utilization (the usage
   guard reads it). Auth: `claude setup-token` (one-year token, `CLAUDE_CODE_OAUTH_TOKEN`) for
   the container; `--bare` can't use it; an `ANTHROPIC_API_KEY` in the environment would take
   precedence and bill the API, so the adapter strips it and checks `apiKeySource`. Note for you:
   Claude Code's legal page says subscription OAuth is for "ordinary use of Claude Code" and that
   products serving other users must use API keys; the judge serves only you.
7. Deploy for testing: done by the main session (Environment events). Redeploys are fine.
8. Twitch app credentials: in the worktree's `.env` now. The detector polls viewer clips live; the
   backtest's credential blocker is gone.

## Your answers, second batch (2026-11-21, relayed by the main session)

Where you didn't answer, your rule "use the recommendation" applies, so those count as decided.
All recorded in DESIGN §15 (rows dated 2026-11-21) and in §17–§19, §22:
1. Default chat delay 8 s until learned: yes (§18).
2. The detector holds the Twitch app credentials and polls Get Clips: yes (§18).
3. ≤ 8 chat candidates per stream-hour to the judge: yes (§18).
4. Raid / gifted-sub spikes: suppressed, as designed (§18).
5. Extra IRC tags (first message, channel-point reward, highlighted message): proper columns on
   `chat_messages` (proposed `first_message`, `highlighted`, `reward_id`, all nullable: null =
   unknown, as in backfill), not `_`-keys; needs a migration and a chat-ingest redeploy (§18).
6. Judge model: Opus through the Claude Code CLI, a pinned full id (§19).
7. Usage limits: none for now ("let it run"). The guard logs the five-hour/seven-day
   utilization from `rate_limit_event`, and all its caps are unset by default, so nothing blocks.
   A call the platform itself refuses for a limit still waits for `resetsAt` (§19). Heads-up: Opus
   with no caps can use a large share of your plan's five-hour window, which your own Claude use
   shares (it read 0.8 during my test, mostly this orchestrator session). The heartbeat will show it, and the caps
   take effect once you set them.
8. audio_only channels aren't judged; channels you want clips from switch to `full` (§19).
9. The judge runs in Docker; you create the token with `claude setup-token` and put it in `.env`
   as `CLIPPER_JUDGE_CLAUDE_OAUTH_TOKEN` (§19).
10. Twitch's GQL API (VOD playback token + chat replay): yes (§17).
11. Backfill fetches audio by default; `--video` per run when renders are wanted (§17).
12. Clips from backfilled VODs may be published if they're worth it, per route (§17, §22).
13. Accepted: `muted` gaps, no `chat_events` from replay, "one writer per table *and source*" +
    the controller guard (§17).
14. `calibrate` and the first backtest data: yesterday's ohnepixel VOD, if still available. The
    main session runs `calibrate` and `clips` on it once the stack is up; detector unit 4 uses it
    later (§17, §18).
15–20. Taste, layouts, SFX/music, retention, Discord and publisher credentials: answered when
    needed, still open.
21. §17–§22 stay "proposed": you'll review them yourself. Only the answers above are decided.

## Answer 14 runs (2026-11-21, main session, on yesterday's ohnepixel VOD)

`clipper-backfill calibrate 2934862154` (ohnepixel 2026-11-20 = our live stream 803; we have
live chunks only from 12:32:42 to 17:29:24Z, Docker was off later):
- **Chat clock:** 205 of 315 sampled replay comments matched our live chat by msg_id;
  `createdAt − tmi-sent-ts` = **exactly 0.000 s for all 205**. The backfilled `sent_at =
  createdAt` is the live clock.
- **T0 candidates:** PDT 12:32:36.470; chat anchor 12:32:40.731 truncated / 41.231 rounded
  (= PDT + 4.26 s, near the 3.9 s seen on xqc); Helix `created_at` 12:32:40.000; path epoch
  12:32:33.
- **T0_audio: none ("no usable window"), a bug.** ohnepixel's VOD is **fMP4** (the first fMP4 VOD
  we've seen). `calibrate.py` `_match` (line ~349) writes the fragments as `vod_{seq}.ts` without
  the `EXT-X-MAP` init section, so ffmpeg fails ("tfhd was found … error reading header";
  chunks 42, 136, 243). `pipeline.py` `_build` (lines ~255–268) already prepends `init_uri` and
  writes `.mp4`; calibrate needs the same. 78 of 297 live chunks were skipped as VOD edge or muted.
  Recorded in DESIGN §3 and §17.

**Rerun after the fMP4 fix (2026-11-21, this session, read-only):** `T0_audio` = 12:32:36.486Z
from 3 windows (scores 1.000/0.999/0.948, spread 21.4 ms = one AAC frame); `T0_pdt` is **16 ms**
earlier, the chat anchor +4.25 s later (Twitch's replay latency compensation, as on xqc). So the
PDT T0 the backfill uses is right to within one AAC frame. Details in DESIGN §17 "Calibrate".

`clipper-backfill clips 2934862154`: 100 fetched, 99 kept, all new, all with a time span, linked
to stream 803 (top clip: 5794 views at 12:50:35Z ± 37 s). They're in `viewer_clips`, the ground
truth for the backtest. The live detector didn't run during stream 803, so the backtest needs the
offline pass / detector unit 4.

## Blocked / waiting for you

- Detector unit 4 (backtest CLI): credentials and ground truth are there (99 viewer clips of
  stream 803, our own live recording of ohnepixel 2026-11-20, so no backfill is needed for its
  first 5 h). Buildable once you say resume.
- Judge unit 6d (Claude CLI adapter + usage guard + `clipper-judge eval`): buildable on a fake
  `claude` executable now (model Opus, no caps by default, Docker); the real runs need your
  `claude setup-token` token in `.env` as `CLIPPER_JUDGE_CLAUDE_OAUTH_TOKEN`.

## Stopped after 6b, resumed (2026-11-21)

Stopped on your request "stop after 6b"; in between only docs changed (both batches of your
answers, above). **Resumed on the evening of 2026-11-21 on your "ok - resume"**, in this order: `channel`
list-only, the fMP4 `calibrate` fix + rerun, `--fill-gaps` (stream 803, then 1412), the IRC-tag
columns (built, deployed only when you time the chat-ingest redeploy), judge 6c, detector unit 4,
then 6d's buildable part, renderer §20, review §21 (Discord) and publisher §22 contracts + fakes.
The live stack runs from this worktree: images are built only from a committed state, and the main
session pings first. Tests run with `CLIPPER_DATABASE_URL` pointed at a nonexistent DB, since
`.env` now holds the live URL.
- 2026-11-21: the five-hour usage limit stopped the agents mid-task (the `channel` fix round);
  continued after the reset at 21:50Z.

## Next

- Done 2026-11-21 after the resume: `channel` list-only (b556ed4); `calibrate` reads fMP4 VODs
  (91abaeb) and its rerun on 2934862154 gave the audio T0 (above).
- Done 2026-11-22: `--fill-gaps` built (f5c5b43; refuses to write unless the audio T0 is
  trusted: >= 2 seam windows within 50 ms and within 0.5 s of the playlist PDT, else
  `--accept-t0`); IRC tag columns + migration 0006 (57b6801; not on the live DB yet).
- **Waiting for the stack** (stopped since ~00:00Z on 11-22): the real fill runs, natively from
  this worktree (ffmpeg 9 here; the backfill image is still at 1778961):
  `uv run clipper-backfill vod 2934862154 --fill-gaps --dry-run` (stream 803), check the T0 lines,
  then without `--dry-run`; then the same for 2936064102 (stream 1412). Measured from the VOD
  listing: 803's VOD runs to ~22:14Z (unrecorded tail ~4.7 h after 17:29Z); 1412's VOD ends
  ~21:07Z, and besides its gaps (22 s, 7134 s, 576 s) the recording stopped at 19:06:47Z (the
  stack was down ~19:07–21:58Z), so a ~2 h tail. Audio for both ≈ 0.7 GB, ~9 h to transcribe at
  BACKFILL priority. 803's 6 s head stays unfilled (no whole segment fits). Older ohnepixel
  streams have unrecorded tails too (stream 17, 2026-11-19: recorded to 18:36Z, VOD to ~22:32Z);
  only on your request.
- **Deploy when you say so** (the main session does deploys): migration 0006 alone first
  (`docker compose up --build migrate`, while the old chat-ingest runs; it's catalog-only, 5 s
  lock timeout), then `docker compose up -d --build chat-ingest` (a restart loses a few seconds
  of chat; best while ohnepixel is offline). The backfill doesn't need 0006.
- Judge 6c (service with the fake model: waits for transcripts, context rounds, one
  transaction per plan + render job), then 6d's buildable part (Claude CLI adapter on a fake
  executable + usage guard; also the stale `# cost guard (daily cap)` comment on
  `ClipRejectReason.BUDGET` in `contracts.py`), renderer §20 (contracts, pure ffmpeg graph
  builder, NVENC smoke test only while the GPU isn't busy with the backlog), review §21 (Discord)
  and publisher §22 contracts + fakes.

## How to deploy the transcriber (done 2026-11-21 by the main session, see Environment events)

1. `docker compose up --build migrate` — applies migrations 0002 (transcriber), 0003
   (backfill tables + `streams` CHECKs), 0004 (detector tables) and 0005 (judge tables) to the
   live `clipper` DB.
   Additive: new columns/tables;
   `chunk_transcripts` is empty so its NOT NULL columns are safe; all existing streams are
   `source='live'`, so the new CHECKs hold.
2. `docker compose up -d transcriber` (naming it enables its `gpu` profile) — first start
   downloads ~1.8 GB of weights into the `models` volume (set `HF_TOKEN` to silence the
   unauthenticated-download warning). Queued `transcribe` jobs from the recorder are then
   processed (LIVE priority first). Natively it needs `CLIPPER_TRANSCRIBER_ENGINE`/`_TAGGER`
   set to fake/none (the real libraries are only in the image).
Image: 10.3 GB as Docker counts it, 3.0 GB unique (7.3 GB CUDA base shared with asr-bench).
New disk use so far ≈ 6.8 GB as Docker counts it (3.0 GB image + 3.8 GB build cache, reclaimable
with `docker builder prune` if needed); all throwaway e2e data (DBs, buckets, volumes) deleted.

## Environment events

- 2026-11-22 ~00:00Z: the Clipper stack was stopped cleanly again (all containers exit 0, also
  the main session's `clipper-filer-ui`), not by this session. Not restarted; the real
  `--fill-gaps` runs wait for the stack. Tests meanwhile use a throwaway Postgres container
  `clipper-orch-testpg` on 127.0.0.1:5434 (tmpfs, removed when done).
- 2026-11-21 ~19:07–21:58Z: from the data, the stack was down then too (no chunks or chat after
  19:06:47Z in stream 1412 until the restart at ~21:58Z).
- 2026-11-21 ~21:58Z: the whole stack was restarted by someone (all containers recreated,
  `migrate` ran again as a no-op), not by this session; images were not rebuilt (still the
  15:13 CET images from 1778961). Postgres refused connections for ~2 min; a test run in
  progress skipped its integration tests meanwhile and was rerun.
- 2026-11-21: E: had 65 GB free (87 % used; 108 GB free on 11-19). Live recordings grow without
  video retention (not built yet; retention is one of your open questions).
- 2026-11-21 17:26Z: the main session restarted the stack from this worktree with your OK. Your
  earlier stop left a 7134 s `not_recorded` gap in stream 1412. The transcribe backlog was done by
  then (870 jobs); the detector had 50 candidates.
- 2026-11-21 15:26:56Z: the whole live stack was stopped cleanly (all containers exit 0), not by
  the main session; probably you (another project's DB started then). Not restarted by me; the
  main session is asking you.
- 2026-11-21 ~14:15Z: **deployed for testing** by the main session (your OK) from this worktree
  (`transcriber` @1778961; until it is merged, compose must run from here). Migrate ran
  0001→0005 on the live DB; controller, chat-ingest and recorder were recreated (ohnepixel was
  live, stream 1412; the recorder resumed at seq 100). The transcriber is working through 795
  queued `transcribe` jobs (GPU ~3.8 GB, 96 %; only the HF_TOKEN warning). The detector is live
  with `judge_jobs=false`, `viewer_clips=true`, 8 s default delay, 0 candidates at the time. The
  backfill image is built, not run. The main session copied main's `.env` into this worktree
  (gitignored, never committed): native `uv run` tools here now reach the live DB unless the URL
  is overridden; tests keep using `CLIPPER_TEST_DATABASE_URL`. No GPU e2e runs while the
  backlog is processed.
- 2026-11-21: two tiny `claude -p` calls (Haiku, a few tokens each) on your subscription, to
  verify the CLI's flags and output for §19 (you asked for it to be checked, not taken from
  memory). Nothing else used the CLI.
- 2026-11-21: Docker Desktop was off (reboot/quit); you started it again (~12:40 local); the
  live stack is running.

- 2026-11-19 ~21:34 local: you stopped the Clipper compose stack to use the PC.
- 2026-11-20 02:31 local: restarted at your OK with `docker start` on the same five containers
  (postgres, objectstore, controller, chat-ingest, recorder; same images, no `compose up`, so no
  migration ran and nothing was rebuilt). All three services logged in normally; ohnepixel was
  offline. The throwaway test Postgres `clipper-orch-testpg` (127.0.0.1:5434) was removed on
  2026-11-20 (memory was tight while you used the PC); tests use per-checkout throwaway
  databases on the compose Postgres again.
- Disk: the live recordings volume `clipper_objectdata` is at 24.5 GB (ohnepixel at source
  quality; video retention (~48 h) isn't implemented yet, see DESIGN §14 open items). E: had
  ~108 GB free on 2026-11-19.
- Seen in the live recorder logs: `"level": "info"` in lowercase (importing streamlink renames
  the stdlib level names); fixed in core logs by step 5d (takes effect on the next rebuild).

## Status of external gates

- Benchmark finished 2026-11-19 01:05Z (`BENCHMARK_DONE` exists): GPU and Docker builds allowed
  (keep Docker disk use modest, E: holds live recordings).
- Model decision on main (a2a3487, 2026-11-19): speech = **Whisper large-v3-turbo**
  (faster-whisper) decided; sound classifier = **CED-base, PROVISIONAL** (not confirmed by the
  user; its adapter is built behind the SoundTagger interface and stays swappable).
  Not merged (rule: never merge); the DESIGN.md commit is cherry-picked onto `transcriber`.

## Open questions for the user

Everything asked so far is answered (2026-11-21, both batches above; DESIGN §15). Still open, to be
answered when needed:
- Taste: title style, clip lengths (default 10–60 s), caption presets; sign off ~5 real renders.
- Facecam layout per channel (`layouts.toml`, ohnepixel first).
- A licensed SFX/music library with licence proof per asset.
- Retention: discarded clips 7 days, pinned video of published clips 30 days (proposed); video
  chunk retention (~48 h) isn't implemented yet (the recordings volume grows).
- The Discord and publisher credentials (below).
- Your review of DESIGN §17–§22: they stay "proposed" until you've read them.

## Credentials / decisions needed

- Judge (decided: Opus, Docker, no caps): you create the token with `claude setup-token` and put it
  in `.env` as `CLIPPER_JUDGE_CLAUDE_OAUTH_TOKEN`. Make sure no `ANTHROPIC_API_KEY` sits in
  `.env` (it would bill the API instead of the plan; the adapter strips it anyway).
- Review (Discord): a Discord application with a bot (bot token, application id), a private
  server with a review channel and the bot invited, your Discord user id, the server and channel
  ids; a web UI password; optionally Tailscale on PC + phone.
- Publisher: per platform, the credentials and upload method you choose (answer 4), then the
  routes. For API uploads: YouTube (Google Cloud project, Data API v3, Desktop OAuth client,
  consent screen "In production"); TikTok (developer app with Content Posting API + audit);
  Instagram (professional account + Facebook Page, Meta app + app review); Whop (campaign
  submission stays manual).
