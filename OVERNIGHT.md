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

## In progress

- Step 4b-1: services/transcriber processing pipeline (decode, engine interfaces, Silero VAD via
  onnxruntime, sentences, junk filter, metrics, tail detection), no DB.
- Read-only design drafts (scratchpad, not in the repo yet): §17 backfill, §18 detector.

## Next

- 4b-2: transcriber job handler (window/handover/re-runs, transaction, NOTIFY, heartbeat, main)
  + integration tests with fake engines.
- Real model adapters, CUDA Dockerfile, compose service: wait for
  `C:\Users\hkuna\developer\clips\data\bench\BENCHMARK_DONE` AND the model choice in the decision
  log of DESIGN.md on main.
- Step 5: tools/backfill. Then Phase 2: detector, judge, renderer, review, publisher.

## Status of external gates

- Benchmark finished 2026-11-19 01:05Z (`BENCHMARK_DONE` exists): GPU and Docker builds allowed
  (keep Docker disk use modest, E: holds live recordings). Model choice NOT made yet → real
  engine adapters + CUDA Dockerfile + compose service still skipped. Candidates (from the main
  session): faster-whisper large-v3 / large-v3-turbo, NeMo Parakeet TDT 0.6B v2, Qwen3-ASR;
  taggers AST, CED.

## Open questions for the user

- (none yet)

## Credentials / decisions needed

- Speech model + sound classifier choice (write it into DESIGN.md's decision log on main).
