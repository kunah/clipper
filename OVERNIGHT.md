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

## In progress

- Step 5b: Twitch VOD access (Helix videos/clips, VOD playlist via streamlink, GQL chat replay
  behind interfaces + fakes, one small opt-in smoke test against a real VOD).

## Next

- Step 5c (media + time, CLI) and 5d (chat + clips). Then Phase 2 in roadmap order (detector
  §18 first).

## How to deploy the transcriber (your call, in the morning)

1. `docker compose up --build migrate` — applies migrations 0002 (transcriber) and 0003
   (backfill tables + `streams` CHECKs) to the live `clipper` DB. Additive: new columns/tables;
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

- **Transcriber:** confirm CED-base. On CPU it is the slowest stage (24× realtime on 2 threads;
  34× on 4, 50× on 8). Options: more threads, its Hub repo's `model.onnx` on onnxruntime (CPU or
  GPU, no torch), CUDA torch (+~5 GB image), or a smaller tagger. Also possible: pack short speech
  stretches into fewer 30 s Whisper windows (Whisper is 31× per stretch vs 49× on whole chunks).
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
