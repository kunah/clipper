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

## In progress

- 4b-2: transcriber job handler (window/handover/re-runs, transaction, NOTIFY, heartbeat, main)
  + integration tests with fake engines.

## Next
- Real model adapters, CUDA Dockerfile, compose service: wait for
  `C:\Users\hkuna\developer\clips\data\bench\BENCHMARK_DONE` AND the model choice in the decision
  log of DESIGN.md on main.
- Step 5: tools/backfill. Then Phase 2: detector, judge, renderer, review, publisher.

## Status of external gates

- Benchmark finished 2026-11-19 01:05Z (`BENCHMARK_DONE` exists): GPU and Docker builds allowed
  (keep Docker disk use modest, E: holds live recordings).
- Model decision on main (a2a3487, 2026-11-19): speech = **Whisper large-v3-turbo**
  (faster-whisper) decided; sound classifier = **CED-base, PROVISIONAL** (not confirmed by the
  user; its adapter is built behind the SoundTagger interface and stays swappable).
  Not merged (rule: never merge); the DESIGN.md commit is cherry-picked onto `transcriber`.

## Open questions for the user

Designs written overnight and marked "proposed, not yet agreed" in DESIGN.md: §17 backfill,
§18 detector. Their decision rows sit under each section, not in the decision log, until you
agree. Questions from them:

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
- **Transcriber:** the decision log says faster-whisper with its built-in VAD; our own Silero gate
  already runs in front (§16). The adapter keeps `vad_filter` as a setting; see the e2e note
  below for which value was measured better.

## Credentials / decisions needed

- Confirm the sound classifier (CED-base is provisional).
