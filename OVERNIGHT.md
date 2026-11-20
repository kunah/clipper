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

## Blocked / waiting for you

- Detector unit 4 (backtest CLI): needs ground truth = viewer clips (Twitch app credentials in
  this checkout's `.env`) and some backfilled VODs on a migrated DB.
- Judge unit 6d (real LLM adapters + `clipper-judge eval`): your provider/model choice, API key
  and the eval spend.

## In progress

- Judge unit 6b: input builder + resolver (pure: transcript/chat/audio window → LLM input;
  LLM answer → validated EditPlan with recorded repairs).

## Next

- Judge 6c (service with the fake model), then renderer §20 (contracts, pure graph builder,
  NVENC smoke test), review §21 and publisher §22 contracts + fakes.

## How to deploy the transcriber (your call, in the morning)

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

Designs written overnight and marked "proposed, not yet agreed" in DESIGN.md: §17 backfill,
§18 detector, §19 judge, §20 renderer, §21 review, §22 publisher. Their decision rows sit under each section, not in the
decision log, until you agree.

- **Transcriber:** confirm CED-base (still provisional). On CPU it is the slowest stage (24×
  realtime on 2 threads). **Decided by you (via the main session, 2026-11-19 evening): CED runs on
  the GPU.** The main session is comparing CED's ONNX model on onnxruntime-gpu (preferred, no
  torch) vs CUDA torch (+~5 GB image) and will send a recipe; this session implements it then and
  reruns the GPU e2e test (when you're not using the PC). Until then CED's adapter is unchanged.
  Also possible: pack short speech stretches into fewer 30 s Whisper windows (Whisper is 31× per
  stretch vs 49× on whole chunks).
  The decision log says "built-in VAD"; measured, our Silero gate makes it redundant, so
  `vad_filter` defaults to False (setting).
- **Backfill (§17):** use Twitch's undocumented GQL API at all (VOD playback token + chat
  replay; Twitch discourages third-party use, client ids can be revoked)? Without it, backfill
  gets only Helix metadata + top clips. Which channels and how many VODs back? Audio only, or video
  ranges too? May clips from backfilled VODs ever be published (§2 rights), or is it dev data
  only? OK with the new gap reason `muted` and no `chat_events` from replay? New rule "one writer
  per table *and source*" (backfill writes the rows of backfilled streams in live tables) and the
  controller guard (never touch `source='backfill'` streams).
- **Detector (§18):** defaults OK: 8 s chat delay until learned, ≤ 8 judge (LLM) calls per
  stream-hour? Suppress raid / gifted-sub spikes, or do campaigns want those moments? May the
  detector hold Twitch app credentials (it polls Get Clips itself)? Extra IRC tags (first message,
  channel-point reward, highlighted message) as `_`-keys in `badges` like `_action`, or a proper
  column (needs a chat-ingest redeploy either way)?
- **Judge / renderer / review / publisher (§19–§22):**
  - Rights model (§2): which channels, platforms and accounts; per publish route the rights basis,
    campaign rules and caps; may backfilled clips be published?
  - Judge LLM provider + model (to be picked by a `clipper-judge eval` run: approve its spend),
    budgets in USD per stream-hour and per day (e.g. $0.50 / $10); judge audio_only channels
    (can't be rendered without video) or switch them to `full`?
  - Taste: title style, clip lengths (default 10–60 s), caption presets; sign off ~5 real renders.
  - Facecam layout per channel (`layouts.toml`, ohnepixel first); a licensed SFX/music library
    with licence proof per asset; retention (discarded clips 7 days, pinned video of published
    clips 30 days).

## Credentials / decisions needed

- Confirm the sound classifier (CED-base is provisional).
- Judge: LLM API key (provider of your choice).
- Review: Telegram bot token (@BotFather), your numeric Telegram user id(s) and chat id; a web UI
  password; optionally Tailscale on PC + phone.
- Publisher: YouTube (Google Cloud project, Data API v3, Desktop OAuth client, consent screen "In
  production"); TikTok (developer app with Content Posting API + audit); Instagram (professional
  account + Facebook Page, Meta app + app review); Whop account (campaign submission stays manual).
