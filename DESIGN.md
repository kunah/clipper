# Clipper: design & decisions

Automated clipper for Twitch (Kick/YouTube later). It watches chat and viewers, finds
moments where chat spikes compared to the channel's own history, then records, transcribes,
judges, edits and publishes short vertical clips.

> **Status (2026-11-18):** Phase 1 / steps 1–3 done (workspace, shared core, schema, job queue,
> unified logging, Twitch lib, controller, chat-ingest, recorder). Next: step 4 (transcriber).
> Controller live status needs Twitch app credentials in `.env`; chat logging works without, but
> the recorder only records streams the controller marked live.
> See [Roadmap](#14-roadmap). Keep this file updated when decisions change.

---

## 1. Idea and workflow

```
 Twitch API ──► controller ── which channels are live, starts/stops recorders, viewer counts
                    │
        ┌───────────┴────────────┐
        ▼                        ▼
  chat-ingest              recorder ── 60 s .ts video chunks + separate audio
        │ per-second stats       │
        ▼                        ▼
  live detector            transcriber (GPU) ── words, sentences, laughter/loudness
        │                        │
        ▼                        ▼
  ┌─────────────────────────────────────────────┐
  │   Postgres (state + job queue)  +  S3 API    │
  └─────────────────────────────────────────────┘
        │
        ▼
  offline detector → judge (LLM → edit plan JSON) → renderer → review (Telegram + web)
        ▲                                                         → publisher → analytics
        └──────────────────────── performance feedback ◄─────────────────────────┘
```

1. **Watch**: chat (every message, events) and viewer counts for each monitored channel.
2. **Record everything** in ~60 s chunks (storage unit only, never the unit of analysis).
3. **Transcribe everything** (word-level timestamps). Enables post-processing clip finding,
   sentence-accurate cut points, measuring chat delay, and a searchable archive.
4. **Detect** candidates live (chat spikes) and offline (transcript/audio/chat together).
5. **Judge**: LLM decides if it's a real moment, can request more context before/after, and
   outputs a structured **edit plan** (JSON: in/out points, title, captions, SFX, music mood).
6. **Render** deterministically with ffmpeg: 9:16 layout (facecam + gameplay), animated
   captions from word timestamps, licensed music/SFX only.
7. **Review**: human approve/reject (Telegram bot on phone for speed, web UI for editing).
8. **Publish** to platforms, then pull analytics back to tune detection and editing.

## 2. Business / rights model (open decision)

The stream belongs to the streamer. Clipping random channels onto our own accounts leads to
DMCA takedowns and strikes. Two legitimate models:

- **Tool for streamers**: they connect their accounts; clips go to their channels.
- **Clipping service with permission**, e.g. clipping campaigns (Whop Content Rewards): the
  creator posts a budget + rate per 1,000 views (typically $1–5) + rules; clippers post on their
  own accounts and submit links; the owner approves/rejects; approved views are paid until the
  budget runs out. Speed matters (budgets pay in approval order; owners can reject late).

Also: YouTube/Instagram demote or demonetize raw re-uploads ("reused content"), so the edit
(captions, framing, hook) is required. Music: royalty-free/licensed only; stream audio itself
may contain copyrighted music (Content ID).

## 3. Platform facts we rely on (verify before depending on them)

| Topic | Fact |
|---|---|
| YouTube upload quota | `videos.insert` has its own bucket: **100 uploads/day** per Google Cloud project by default (separate from the 10,000-unit pool). More via the audit/quota-extension form. Don't create extra projects to bypass (ToS). Unaudited apps' uploads may be locked to private. |
| Instagram Reels API | Business/Creator account + Meta app review, rolling 24 h publish cap. |
| TikTok posting API | Posts are private-only until the app passes an audit. |
| Twitch chat | IRC (anonymous read-only) can join many channels. Tags give `tmi-sent-ts`, emotes, badges, USERNOTICE (subs/raids), CLEARMSG. |
| Twitch status | Helix Get Streams: up to 100 channels per request, includes viewer count. |
| Twitch VOD chat | Chat replay exists while the VOD exists; no official API (tools like TwitchDownloader use the internal GQL API, can break). Deleted messages removed; no viewer history. Use for backfill/backtesting; log live chat ourselves. |
| Twitch clips | Get Clips returns top viewer-made clips with `vod_offset` → free ground truth for testing the detector. |
| Twitch HLS | 2 s segments, each with `EXT-X-PROGRAM-DATE-TIME`, in one of two containers depending on the channel (seen 2026-11-18): MPEG-TS (H.264 + AAC 48 kHz + timed ID3; e.g. xqc) or fragmented MP4 (`EXT-X-MAP` init section + `.mp4` fragments, all renditions incl. audio_only; e.g. ohnepixel), even when only H.264 is requested; the live playlist holds only ~30 s, so a recorder restarted within ~30 s loses nothing. Source 1080p60 ≈ 8.4 Mbps (~63 MB/min); audio ≈ 165 kbps (~1.2 MB/min). Ads are inserted for our recorder (a viewer): segments titled "Amazon…" or inside a `twitch-stitched-ad` DATERANGE; filtering them leaves gaps. Turbo token avoids most ads. VODs get muted for copyrighted music; live recordings don't. |
| MinIO | Community Docker images stopped Oct 2025 → we use **SeaweedFS** (`weed mini`). |

## 4. Detection

- **Per-channel baseline**: rolling median/EWMA of messages per second; score spikes against
  it. Normalize by viewer count. Account for time-of-stream (the "hi" flood at start).
- **Unique chatters**, not just messages (copypasta/bots inflate counts).
- **Fake-spike filter**: sub trains, gifted subs, raids, bits, giveaways (`!enter`), bot
  commands, polls/predictions, emote-only/slow-mode toggles.
- **Emote meaning (must-have)**: KEKW/LUL = funny, Pog = hype, monkaS = tense, Sadge/F = sad.
  Gives the moment *type* → feeds editing (music/SFX/title).
- **Explicit signals**: "clip it", fresh viewer-made clips.
- **Viewer count is weak** (slow, laggy): use it for normalization, not triggering.
- **Audio**: loudness and laughter/scream detection on the transcriber's audio.
- **Merge & cooldown**: one moment makes several spikes.
- **Offline pass** over transcripts finds stories/rants chat didn't spike on.
- **Chat delay** (stream latency 2–15 s + reaction 3–10 s) is learned per channel by
  correlating audio/transcript events with chat spikes (`channels.chat_delay_s`).

## 5. Time model (most important rule)

- Everything is **timezone-aware UTC**; naive datetimes are rejected (`clipper_core.timeline`).
- Video time = HLS `EXT-X-PROGRAM-DATE-TIME` (streamlink drops these tags, so the recorder
  uses streamlink only to get the playlist URL and fetches playlist + segments itself).
  Measured on Twitch: one PDT per 2 s segment, segment PDT ≈ server time when produced.
- Chat time = Twitch `tmi-sent-ts` (server time), not our receive time.
- Gaps (ads, reconnects, recorder down) are stored explicitly in `stream_gaps`. Never compute
  time as `chunk_seq × 60`.
- A chunk is **continuous media**: every gap or HLS discontinuity starts a new chunk, so
  `start_utc + offset` is exact inside a chunk (the transcriber relies on this). Chunk lengths
  vary (ads, restarts, shutdown); ~60 s is a target, not a rule.
- `video_time ≈ chat_time − channel_delay`.

## 6. Priorities and media requests

Jobs have a **priority that can change after they're queued** (demand-driven):

| Priority | Meaning |
|---|---|
| 100 `JUDGE_CONTEXT` | judge asked for more context; a clip is already waiting |
| 80 `TRIGGER` | window around a freshly detected moment |
| 10 `LIVE` | "transcribe everything" from live streams |
| 0 `BACKFILL` | old VODs, whenever the GPU is idle |

- Anyone needing a time range calls `media.request_media(stream, window, priority)`:
  queued transcription jobs for overlapping chunks are bumped immediately; chunks recorded
  later inside the range are enqueued at that priority (`media.priority_for_range`), which
  covers context *after* the trigger.
- The judge may extend context (cap: e.g. 10 min before / 2 min after, max 2 rounds). The clip
  goes back to `waiting_for_media`. If the stream ends, continue with what exists.
- Running jobs aren't interrupted (a 60 s chunk takes seconds).
- Own job queue in Postgres (`clipper_core.jobs`), not a library: `FOR UPDATE SKIP LOCKED`,
  `pg_notify` wake-ups (sent on commit), dedupe keys (re-enqueue only raises priority),
  retries with backoff, stale-lock requeue.

## 7. Clip lifecycle

`candidate → waiting_for_media → judging → rejected | planned → rendering → in_review →
approved | discarded → publishing → published`

A candidate at time T can be judged only when transcripts cover T−90 s … T+30 s (or more if
the judge asked). Checked whenever a transcript lands.

## 8. Architecture

**One git repo (uv workspace), one app + Docker image per service**, shared `libs/core`.
Services talk only through Postgres and object storage.

| Service | Does | Writes tables | Image |
|---|---|---|---|
| `db` (migrate) | migrations, chat partitions, bucket; one-off | schema | slim |
| controller | Helix polling, streams, viewer counts, stale-job sweeper | channels, streams, viewer_snapshots | slim |
| chat-ingest | IRC, raw chat, events, per-second stats. **Rarely redeployed** (restart = lost chat) | chat_messages, chat_events, chat_metrics_1s | slim |
| recorder | HLS segments → ~60 s `.ts` + `.aac` (local spool first, crash-safe), gaps, enqueue transcription. Claims channels itself via leases. **Rarely redeployed** | chunks, stream_gaps | slim + ffmpeg |
| transcriber | faster-whisper on GPU, audio features | chunk_transcripts, transcript_segments, audio_metrics_1s; words files (S3) | CUDA |
| detector | live (reads per-second stats, ~1 s behind) + offline. Separate so tuning never restarts chat logging | candidates (later) | slim |
| judge | LLM verdict + edit plan, context requests | clips (later) | slim |
| renderer | ffmpeg + NVENC, 9:16, captions | renders (later) | CUDA |
| review | FastAPI UI + Telegram bot | clip decisions (later) | slim |
| publisher | platform uploads, tokens, retries; later analytics | publications (later) | slim |
| tools/backfill | past VODs + chat replay + top clips, written in the live format | chunks, chat_* | — |

Rules:
- **One writer per table**, anyone reads.
- **Contracts live in `libs/core`**: models, job kinds, payloads, priorities, storage keys.
- **Migrations are additive** (services update at different times).
- Each Dockerfile installs only its package: `uv sync --frozen --no-dev --no-editable --package <name>`.

## 9. Storage

- S3 API: SeaweedFS locally, Cloudflare R2 later (no egress fees). Config change only.
- Layout (`clipper_core.storage.keys`): `streams/{id}/video/{seq}.ts`,
  `streams/{id}/audio/{seq}.aac`, `streams/{id}/transcripts/{seq}.json`.
- `.ts` not `.mp4`: a crashed recorder still leaves a playable file.
- Separate audio so the GPU worker downloads ~1 MB/min instead of ~45 MB/min.
- Retention: video ~48 h unless a clip uses it; audio + transcripts forever.
- Postgres: `chat_messages` partitioned by UTC day (no DEFAULT partition; writers call
  `ensure_chat_partitions` for the days they write). Transcript sentences have full-text
  search (`text_tsv`); pgvector is available for semantic search later.

## 10. Deployment

**Now: everything on the dev PC** (Windows 11, RTX 4060 8 GB, Ryzen 7 5700X3D, 32 GB RAM).
- Docker Desktop's disk image is on `E:\DockerDesktopWSL` (C: is small, D: is a slow HDD). Named volumes.
- Postgres host port **5433** (5432 belongs to another project's container on this PC).
- Use **`127.0.0.1`, not `localhost`**: `localhost` tries IPv6 first and stalls.
- GPU works in containers (Docker Desktop + WSL2, driver 610).
- Risks: sleep, Windows Update reboots mid-stream, gaming competing for the GPU, home
  bandwidth (~6 Mbps per 1080p60 channel).

**Later: rented server** for always-on parts; GPU worker anywhere (pulls jobs + audio).
Moving = copy compose, change env vars.

**Scale path (10 → hundreds of channels)**: channel assignment via leases (add machines),
per-channel `recording_mode` (`full` / `audio_only` / `off`; most channels audio+chat at
scale), raw chat to ClickHouse if Postgres struggles, Redis/NATS only if the Postgres queue
does.

## 11. Latency budget ("clip within minutes")

Moment ends (post-roll ~30 s) → chunk closes (≤60 s) → hot transcription (seconds) → judge
(~10–30 s) → render with NVENC (~20–60 s) ≈ **2–4 min** to review. Review on the phone is the
bottleneck otherwise. Chunk length is configurable (30 s saves up to ~30 s).

## 12. Rough numbers (10 channels × 6 h/day)

Ingest up to ~85 Mbps; video ~230 GB/day at source quality (measured 8.4 Mbps at 1080p60;
~1.6 TB at 7 days, so 48 h retention on this PC, or `CLIPPER_RECORDER_QUALITIES=720p60,best`);
audio ~4.5 GB/day; ~60 h audio/day to transcribe (≈1–3 GPU-hours, to be benchmarked on the
4060); ~1 M chat rows/day.

## 13. Tech stack

Logging: `clipper_core.logs`, structured JSON lines on stdout (console format on a TTY),
per-task context fields; CloudWatch Embedded Metric Format can be added as a formatter later.

Python 3.12, uv workspace, ruff, pytest (+pytest-asyncio), SQLAlchemy 2 (async, psycopg 3),
Alembic, Pydantic v2 / pydantic-settings, boto3, Postgres 18 + pgvector, SeaweedFS,
streamlink (playlist URL only) + own HLS loop, ffmpeg (NVENC), faster-whisper, FastAPI,
Telegram bot. Own minimal Twitch IRC client (full control over tags).

## 14. Roadmap

**Phase 1: data collection (Twitch only, 1–10 channels)**
1. ✅ Skeleton: workspace, `libs/core` (settings, timeline, contracts, models, job queue,
   media requests, leases, heartbeats, storage, partitions), migrations + `migrate`
   container, compose (Postgres 18 + pgvector, SeaweedFS), tests (34 passing).
2. ✅ `libs/twitch` (IRC parser + anonymous chat client, Helix client, 7TV/BTTV/FFZ emotes),
   controller (Helix polling every 30 s, streams with 2-miss grace, viewer snapshots, leader
   lease, maintenance: partitions + stale-job requeue, `clipper-channels` CLI), chat-ingest
   (one connection for all channels, raw messages, events, deletions, per-second stats with
   emote counts by name; 1 s batched writes, retry on DB errors).
3. ✅ recorder: streamlink resolves the playlist URL, own HLS loop (PDT times, ad filter,
   dedupe across reloads/restarts), local spool (`.ts` + state file per chunk, recovered after a
   crash), ffmpeg copies audio to `.aac`, upload, then chunk + gap + transcription job (priority
   from `priority_for_range`) in one transaction. Channels claimed via leases. Verified live on
   Twitch (full + audio_only, graceful stop, hard kill + recovery, restart without gaps).
4. ⬜ transcriber: GPU worker for `transcribe` jobs. Words (UTC) → S3 JSON per chunk,
   sentences → `transcript_segments`, per-second sound classes + loudness → new
   `audio_metrics_1s` table, all in one transaction + NOTIFY. Chunk boundaries: the last
   sentence of a chunk that runs into the cut is *provisional* and is re-transcribed and taken
   over by the next contiguous chunk (no sentence is ever cut, no added latency).
   Model picked from `tools/asr-bench` (real stream audio, measured on the 4060).
   Full design: [§16](#16-transcriber-design-step-4).
5. ⬜ backfill tool: past VODs + chat replay + top clips → detector development offline.
   Design: [§17](#17-backfill-tool-step-5--proposed-2026-11-19-not-yet-agreed) (proposed
   2026-11-19, not yet agreed).

**Phase 2**: detector (live + offline, emote categories, fake-spike filter) → judge (LLM,
context requests) → renderer (9:16, captions) → review (Telegram + web) → publisher →
analytics loop. Then Kick/YouTube. Detector design:
[§18](#18-detector-phase-2--proposed-2026-11-19-not-yet-agreed) (proposed 2026-11-19, not yet
agreed).

**Open items**: Twitch app registered and in `.env` (2026-11-18); first channel ohnepixel
(`full`); pick the rest of the real channels (`clipper-channels add ...`).
FFZ emote API was unreachable on 2026-11-18; its parser follows the documented format but is
unverified. chat-ingest is a single instance for now (use per-channel leases to shard later).
Recorder: ad detection follows streamlink's rules but hasn't been seen live yet (no ad during
the test); we request H.264 only (HEVC/AV1 renditions untested);
gap rows become visible with the chunk after the gap (up to ~60 s later); video retention
(delete after ~48 h) is not implemented yet; recording starts up to ~40 s after go-live
(controller poll 30 s + recorder sync 10 s), and the playlist's ~30 s backlog covers most of it.

## 15. Decision log

| Date | Decision | Why |
|---|---|---|
| 2026-11-18 | Twitch first; Kick/YouTube later | simplest APIs, best tooling |
| 2026-11-18 | Record everything (1-min chunks) and transcribe everything | post-processing clip finding, cut points, delay measurement; VODs can be deleted/muted |
| 2026-11-18 | Run on the dev PC first, portable to a server | cheapest start; Docker + S3 API keep it portable |
| 2026-11-18 | Clips within minutes | campaigns reward speed |
| 2026-11-18 | Python | streamlink, faster-whisper, audio ML are Python-native |
| 2026-11-18 | Separate app + image per service (one repo) | different deps (CUDA vs slim), chat/recorder must not restart when others change |
| 2026-11-18 | Detector separate from chat-ingest | tune constantly without losing chat |
| 2026-11-18 | Demand-driven, changeable job priorities; own Postgres queue | judge can request more context; libraries can't re-prioritize queued jobs |
| 2026-11-18 | Postgres as queue + state, no Redis/NATS yet | low volume, transactional enqueue, fewer moving parts |
| 2026-11-18 | SeaweedFS instead of MinIO | MinIO stopped community images |
| 2026-11-18 | Postgres host port 5433, use 127.0.0.1 | 5432 taken on this PC; `localhost` stalls on IPv6 |
| 2026-11-18 | Twitch clients in a separate `libs/twitch` (not core) | core stays dependency-light; backfill tool will reuse IRC/Helix/emotes |
| 2026-11-18 | Chat via anonymous IRC; own minimal client | no credentials needed, full access to tags (tmi-sent-ts, emotes, USERNOTICE) |
| 2026-11-18 | Per-second stats store emote counts **by name**; categories applied by the detector | the emote→meaning mapping can change without re-ingesting chat |
| 2026-11-18 | Third-party emotes (7TV/BTTV/FFZ) recognised by token match | KEKW/OMEGALUL/Pog etc. are not Twitch emotes |
| 2026-11-18 | Unified structured logging in core (stdlib logging, JSON lines to stdout, context fields) | one format for all services and libraries; ready for CloudWatch Logs/Insights and EMF metrics later without changing call sites |
| 2026-11-18 | Recorders claim channels themselves (lease `record:channel:{id}`); the controller doesn't assign | no extra coordination; a second recorder machine just starts and takes unleased channels |
| 2026-11-18 | Record one rendition (video incl. audio) and copy the audio out with ffmpeg; `audio_only` mode uses Twitch's audio rendition | one download per channel; no re-encoding; audio stays sample-aligned with the video |
| 2026-11-18 | Spool chunks on local disk before upload; a state file per chunk, rewritten after each segment | S3/DB outages and crashes don't lose media; recovery cuts back to the last complete segment |
| 2026-11-18 | Chunks are continuous; a gap row is committed with the chunk that follows it | exact time mapping inside a chunk; gap + chunk are atomic and survive crashes |
| 2026-11-18 | New gap reason `not_recorded` (no recorder running: joined late, restart) | consumers can tell "nothing was there" (ad) from "we missed it" |
| 2026-11-18 | fMP4 streams: spool init section + fragments as `.mp4`, remux to `.ts` (ffmpeg copy) when the chunk is finished; a changed init section starts a new chunk | storage and every consumer keep one format (`.ts` + `.aac`); the spool file stays readable after a crash |
| 2026-11-18 | Transcription is English-only | the channels are English; opens English-specialised models |
| 2026-11-18 | Chunk boundaries: provisional last sentence, taken over by the next contiguous chunk | independent chunks cut a word every minute (captions, judge context); waiting for the next chunk adds 60 s latency |
| 2026-11-18 | Sound classification (laughter, screaming, cheering, music...) in the transcriber from the start | strong signal for the detector; the audio is decoded anyway |
| 2026-11-18 | Pick models by benchmark on real stream audio (`tools/asr-bench`), not by leaderboard | leaderboard sets are clean speech; stream audio has game sound, music, several speakers |
| 2026-11-19 | Speech model: **Whisper large-v3-turbo** via faster-whisper (float16, beam 5, built-in VAD, word timestamps), "for now" | benchmark (§16): fast enough (49× realtime, ~1.2 GPU-h/day for 60 h audio), least GPU memory (2.3 GB), no made-up text on music/silence, most central transcript of the 5 candidates, word timing close to the others, light runtime (no torch/NeMo); Parakeet is 4× faster but transcribes song lyrics and needs NeMo; Qwen3-ASR too slow on the 4060 |
| 2026-11-19 | Sound classifier: CED-base (`mispeech/ced-base`), **provisional, not yet confirmed by the user** | 666× realtime, 0.5 GB, works on 2 s windows (per-second resolution); AST needs 10 s windows. Neither could be judged for accuracy: the sample had little laughter/screaming |
| 2026-11-19 | Voice-activity gate: Silero VAD v6.2 run by onnxruntime on the CPU (pinned model URL + sha256), numpy port of `get_speech_timestamps`; no torch in the transcriber for it | gate is model-independent (§16 step 4); ~185× realtime on one CPU thread; onnxruntime is small, torch would be ~GBs |

## 16. Transcriber design (step 4)

**Status:** design agreed 2026-11-18. Models (2026-11-19, see decision log): speech **Whisper
large-v3-turbo** (faster-whisper) decided; sound classifier **CED-base** provisional until the user
confirms. Both sit behind the engine interfaces, so either can be swapped later.

**Benchmark** (`tools/asr-bench`, 2026-11-19; 20 × 60 s chunks from 5 English channels, idle
RTX 4060, each chunk transcribed on its own):

| model | x realtime | s per 60 s chunk (median / p95) | GPU peak | load | non-speech (music, silence) |
|---|---|---|---|---|---|
| Parakeet TDT 0.6B v2 (NeMo) | 189× | 0.32 / 0.38 | 5.4 GB | 38 s | transcribes song lyrics |
| **Whisper large-v3-turbo** (faster-whisper) | 49× | 1.41 / 2.63 | 2.3 GB | 4 s | clean (0 words) |
| Whisper large-v3 (faster-whisper) | 18× | 3.59 / 7.66 | 4.2 GB | 6 s | clean (0 words) |
| Qwen3-ASR 0.6B + aligner | 8× | 7.83 / 17.0 | 4.2 GB | 25 s | made-up words |
| Qwen3-ASR 1.7B + aligner | 4× | 10.2 / 67.8 | 6.4 GB | 32 s | made-up words, runaway generation |

On the 12 minutes with speech, average disagreement with the other models was 6.4–7.4 % of words
(turbo the most central); word start times agreed within 60–90 ms (large-v3: 130–170 ms).
Sound classifiers: CED-base 666× realtime, 0.5 GB, 2 s windows; AST 92×, 1.4 GB, needs 10 s windows.
Implementation notes: pass 16 kHz float32 arrays to faster-whisper (its PyAV file reading breaks
with current PyAV); `language="en"`, `beam_size=5`, `word_timestamps=True`, `vad_filter=True`,
`condition_on_previous_text=False`; faster-whisper's Silero VAD
(`faster_whisper.vad.get_speech_timestamps`) can also give the speech fraction per second. CED's
Hub model code already applies the sigmoid (its "logits" are probabilities). CUDA base image that
works with CTranslate2 4.9: `nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04` (see
`tools/asr-bench/Dockerfile`).

**Role.** `services/transcriber`, one process per GPU (CUDA image). Claims `transcribe` jobs with
`jobs.JobRunner` (highest priority first, then oldest). Writer of `chunk_transcripts`,
`transcript_segments`, `audio_metrics_1s` (new) and the words files in object storage
(`storage.keys.chunk_words`). English only. A single worker keeps chunks of one stream in order.

**Per job (chunk N):**
1. Load chunk N. No `audio_key` → nothing to do, job done.
2. **Window.** If the previous chunk P (same stream, `seq - 1`) is contiguous (`P.end_utc ==
   N.start_utc` within 50 ms), already transcribed, and has a provisional tail (`tail_from` not
   null), the window starts at `max(P.tail_from, P.start_utc, N.start_utc − 30 s)`: download P's
   audio too and prepend that part. When the 30 s cap applies, P keeps (now non-provisional)
   ownership of `[P.tail_from, window start)`. Otherwise the window starts at `N.start_utc`.
   The window end (`owned_to`) is `N.end_utc`; on a re-run while the next contiguous chunk is
   already transcribed it is `min(N.end_utc, next.owned_from)`, and N keeps no provisional tail.
3. **Decode** to 16 kHz mono float32 PCM ourselves (ffmpeg/soundfile), never via an engine's own
   file reader (faster-whisper's PyAV path broke with current PyAV).
4. **Speech recognition** through the engine interface → words (text, start, end, confidence)
   relative to the window start, plus sentence segments if the engine gives them (otherwise split
   on `.?!` and pauses > 1 s, max ~30 s per sentence). Voice-activity gating in front of the
   model: non-speech stretches are not sent to it (avoids made-up text, and LLM-decoder models
   running away on music/silence).
5. **Provisional tail.** If the last sentence ends within 0.5 s of the window end (speech runs
   into the cut), its start becomes `tail_from`; its words/sentences are provisional until the
   next contiguous chunk takes them over (step 2 of that chunk).
6. **Junk filter.** Drop known phantom phrases ("thanks for watching", ...), repetition loops
   (a 3-gram repeated ≥ 4 times) and words outside detected speech; keep the quality scores on
   the segments (`avg_logprob`, `no_speech_prob` or the engine's confidence) so consumers can
   filter further.
7. **Audio metrics** for N's own range `[start_utc, end_utc)`, one row per second: loudness
   (dBFS), speech fraction (from the voice-activity detector), and sound-class scores by group
   (laughter, screaming, shouting, cheering, gasp, crying, music, singing, speech,
   gunfire_explosion; each the max over its AudioSet labels, see `tools/asr-bench/run_tagger.py`).
8. **Words file** for N: `streams/{id}/transcripts/{seq}.json` = `WordsFile` (chunk id, stream id,
   seq, model, `t0` = UTC of the window start, words with start/end seconds relative to `t0`).
9. **One transaction:** upsert `chunk_transcripts` (+ `owned_from` = window start, `owned_to`
   from step 2, `tail_from`); delete + insert N's `transcript_segments` (retries are idempotent);
   if P's tail was taken over: set `P.owned_to = N.owned_from` (the window start, **not**
   `P.tail_from`: they differ when the cap applied), `P.tail_from = NULL`, and replace P's
   provisional segments by non-provisional ones from P's words before the window start (none
   unless the cap applied); upsert N's `audio_metrics_1s` rows;
   `NOTIFY clipper_transcripts, '<stream_id>'` (sent on commit; the judge waits on it).
   Writer rules (the CHECK enforces `owned_from <= owned_to` and `owned_from <= tail_from <
   owned_to`): words are clamped to the window, a tail that would start at or after `owned_to`
   is dropped, loudness is floored at a finite dBFS (CHECK: `loudness_db <= 0`, no NaN/-inf,
   `speech_ratio` 0..1).

**Ownership rule.** A chunk's words file is authoritative only for `[owned_from, owned_to)` of its
`chunk_transcripts` row; a word belongs to the chunk whose owned range contains the word's start.
Owned ranges of contiguous chunks tile (`P.owned_to == N.owned_from`). Consumers never read words
files directly; they call `clipper_core.transcripts.words(session, store, stream_id, interval)`
(UTC words, ownership applied) and `transcripts.coverage(session, stream_id, interval)`, which the
judge uses for "transcripts cover T−90 s … T+30 s". Coverage splits the interval into:
- `transcribed` (union of owned ranges; `provisional` = their `[tail_from, owned_to)` parts),
- `untranscribed` (a chunk with audio is there but no transcript yet: job queued, running or
  failed),
- `gap` (`stream_gaps`, chunks without audio, before the stream started or its first chunk, after
  `ended_at` or the last chunk of an ended stream; from `ended_at + 5 min` (`ended_grace`) on,
  everything after the last chunk, since the recorder writes nothing more),
- `unrecorded` (no chunk there yet).
`pending` = untranscribed + unrecorded; `complete` = nothing pending (provisional counts as done).

Details decided while building it (step 4a):
- **Chunk jitter 0.5 s** (`contracts.CHUNK_JITTER`): neighbouring chunks don't line up to the
  millisecond (a chunk ends at its last segment's PDT + duration, the next starts at the next
  PDT), and the recorder writes gap rows only for longer holes (its tolerance uses the same
  constant). Coverage treats shorter holes with known parts on both sides as transcribed (between
  two transcribed parts), untranscribed (next to an untranscribed chunk) or gap (otherwise); it
  never bridges over a chunk with audio.
- **Time before `streams.started_at`** is gap (else a window around a moment in the first 90 s
  never completes).
- **Later chunk wins** where two owned ranges overlap (jitter), so `words()` never returns a word
  twice.
- **Words files are forward-compatible**: `WordsFile`/`Word` ignore unknown fields (old readers
  read newer files after additive changes); incompatible changes bump `version` (now 1), which
  old readers reject.

Details decided while building it (step 4b-1, processing pipeline `clipper_transcriber.pipeline`):
- **Voice activity**: Silero VAD v6.2 ONNX on onnxruntime (CPU, no torch; pinned URL + sha256,
  downloaded into the models dir), regions by a numpy port of `get_speech_timestamps` with
  `max_speech_s = 29.6` (gate max − 2 × pad), so long speech splits at its longest pause.
  `EnergyVad` (loudness threshold) for tests and as a fallback.
- **Gating**: speech stretches (regions closer than 1 s merged, 0.2 s padding, at most 30 s) go
  to the engine in one `transcribe_many` call per window (batching adapters); the engine also
  hears audio after `owned_to` (context), words are clamped to it.
- **Time grid**: all window offsets are whole microseconds (Postgres precision); a word must
  start, and a tail must start, at least 1 µs before the window end (`owned_to` or the end of the
  audio, rounded down), so `start + offset < owned_to` holds after the conversion.
- **Sentences**: text and times always come from the words; engine segments only group words
  (by word midpoint) and carry their scores (`avg_logprob`, `no_speech_prob`, `confidence`, else
  the mean word confidence; non-finite → NULL). New nullable `transcript_segments.confidence`.
- **Junk filter**: sentences without letters/digits always; sentences made only of phantom
  phrases or starting with a credit prefix ("subtitles by", ...) when their words' mean VAD
  speech share is < 0.8; "thank you"/"you"/"bye"-type sentences when < 0.5; words whose span
  stays > 0.2 s away from all VAD speech; loops = a 3-gram recurring every ≤ 20 words ≥ 4 times
  in a row, across sentences ("here" × 6 → "here here here"), cut only where the engine gives no
  scores or they agree (compression ratio > 2.4 or avg_logprob < −1).
- **Tail** also when VAD speech runs into the cut but the engine gave no words for its end (the
  cut word is often dropped): the tail sentence is then the last one if it reaches into that
  speech region, else the region itself. The tail starts 0.2 s (gate pad) before it, as lead-in
  for the next chunk (engines put word starts late), but never before the previous words' end,
  so no word changes owner.
- **Metric rows**: a UTC second belongs to the chunk whose own range contains its middle
  (`ts + 0.5 s`): chunks write disjoint seconds even with jitter, and each row has ≥ 0.5 s of the
  chunk's audio. A row uses the window audio it has (the first and last may be partial);
  `speech_ratio` = share of samples in VAD frames with prob ≥ threshold; loudness RMS dBFS clamped
  to [−100, 0]. The tagger gets audio starting on a whole second: up to 2 s of window audio
  before the first row as context, zero-padded when the first second starts before the audio.
- **Decode errors**: `audio.AudioError` (ffmpeg missing, OS error, timeout: retry) vs
  `audio.DecodeError` (ffmpeg can't decode the file: give up on the chunk).

**Contract changes (libs/core, additive migration 0002):**
- `chunk_transcripts`: + `owned_from`, `owned_to`, `tail_from` (timestamptz; tail nullable),
  CHECK `owned_from <= owned_to` and `owned_from <= tail_from < owned_to`.
- `transcript_segments`: + `provisional` (bool, default false), + `confidence` (float,
  nullable), index on `chunk_id` (the transcriber replaces a chunk's segments on every job).
- new `audio_metrics_1s` (PK `stream_id, ts`, FK streams; `loudness_db`, `speech_ratio`,
  `classes` jsonb; CHECK ranges). Writer: transcriber; the detector reads it next to
  `chat_metrics_1s`.
- `contracts`: `TRANSCRIPTS_NOTIFY_CHANNEL = "clipper_transcripts"`, `WordsFile` / `Word` models,
  `CHUNK_JITTER`.
- new module `transcripts` with `words()` and `coverage()`.

**Engine interfaces** (in the service): `SpeechEngine.transcribe(pcm) -> words + segments` and
`SoundTagger.classify(pcm) -> per-second group scores`. Real adapters (and the CUDA Dockerfile)
are added once the benchmark has picked the models; a fake engine drives the tests.

**Edge cases.** Out-of-order processing (N+1 bumped and done before N): N+1 starts at its own
start; N then keeps no provisional tail (possible small glitch at that one boundary). Gap or
stream end after N: the provisional tail stays (best available). Requests in `media_requests`
are marked fulfilled by whoever asked, not by the transcriber.

**Runtime.** CUDA base image, model weights in a `models` volume (`HF_HOME`), GPU reserved in
compose. Heartbeat info: models, device, realtime factor, queue depth by priority. Crashed jobs
are requeued by the controller after 15 min (exists).

## 17. Backfill tool (step 5) — proposed 2026-11-19, not yet agreed

**Status:** proposal, nothing built; the smoke test (5b) checks every UNVERIFIED fact first.

**Role.** `tools/backfill` (`clipper-backfill`) runs natively (ffmpeg on PATH) or via
`docker compose run --rm backfill` (`tools` profile). It writes archives (VOD media + chat replay)
and viewer clips in the live format, so transcriber, detector and judge need no special case.

**Commands** (idempotent; `--dry-run` does all reads, prints the plan, writes nothing):
- `vod <id|url>... [--no-media] [--video [Q]] [--video-range A-B]... [--no-chat] [--no-clips]
  [--no-transcribe]`: default is audio_only + chat + clips + jobs. `--video` takes a video rendition
  and copies its audio out, like the recorder; `--video-range` adds video to the chunks it overlaps.
- `channel <login> [--since D] [--limit N]`: the newest N archives, **oldest first** (VODs expire).
- `clips <login>`, `status`, `calibrate <video_id>` (read-only; a stream also recorded live): T0
  methods vs live PDT by audio correlation, `createdAt` vs `tmi-sent-ts`.

**Skipped:** highlights/uploads (no `stream_id`), archives still live, streams with
`source = 'live'` (clips only), sub-only media (usher 403 → one `not_recorded` gap; chat tried).

**Idempotency and resume.** `streams` (unique on the archive's `stream_id`) and the new `vods` row
are created together and found again on a re-run; a lease `backfill:vod:{id}` keeps runs apart.
- Media: the chunk row, its gaps, its `transcribe` job and `vods.media_done_s` (VOD offset reached)
  commit together. The open chunk sits in a temp dir (no spool: the VOD can be fetched again). A
  crash loses only that chunk, and a re-run continues at `media_done_s` with the next `seq`.
- Chat: messages use `ON CONFLICT DO NOTHING` (PK `sent_at, msg_id`, deterministic). Metrics are
  final only before `vods.chat_metrics_to`, which commits with `chat_done_s`, so a resume re-reads
  from just before it. Jobs use `dedupe_key`; clips are upserted.

**Who writes what: one writer per table *and source*** (replaces the §8 rule; decision log). Only
backfill writes a backfilled stream's rows (`streams.source = 'backfill'`): the stream, its
`chunks`, `stream_gaps`, `chat_messages` and `chat_metrics_1s`. Rows are marked by `source` where
the column exists, else by their stream. Neither side writes the other's. Derived tables
(transcripts, audio metrics) keep one writer for both; `vods`/`viewer_clips` are backfill-only.
- Guard 1: the controller's stream upsert gets `WHERE streams.source = 'live'`. Otherwise a stream
  id seen live again would flip a backfilled stream to `live`, and the recorder would write into it.
- Guard 2: a live `chat_metrics_1s` row of the channel in the VOD range → chat `skipped` (the PK
  can't hold both). Missing channels are inserted inactive (`off`), never changed.

**Time model.** VOD playlists have no PDT (UNVERIFIED), so offset `t` (ΣEXTINF) maps to
`UTC = T0 + t`. T0 is frozen on the first run in `vods.t0_utc` (= `streams.started_at`).
- **Chat anchor (default):** `x = createdAt − contentOffsetSeconds` over ~15 sampled pages,
  whole-hour outliers (timezone bug) fixed first. Offsets are whole seconds, so T0 = median(x)
  − 0.5 s if truncated (median if rounded). Error ±0.5 s + Twitch's anchor; chat↔media = its replay.
- **Helix `created_at`** (1 s, a few s off) is fallback below 20 comments. > 30 s off the chat
  anchor = old-VOD offset bug → Helix + flag `t0_conflict`. Path `<epoch>`, TDTG: logged only.
- **Audio** (`calibrate`, ±20 ms, needs a live recording) measures both methods' bias once.
- **Chat time:** `sent_at = createdAt` (ms, the `tmi-sent-ts` clock). Replay shows comments late by
  latency + reaction, like live, so `video_time ≈ chat_time − channel_delay` keeps the live delay.
  Missing or > 2 s off → `T0 + offset + 0.5 s`. ~1 s suffices (per-second features).
- **Gaps:** VODs have no ads. Each `EXT-X-DISCONTINUITY` starts a chunk and a *part*. T0 is
  re-checked per part; a step ≥ 1 s becomes a gap (`unknown`) that shifts the rest.
  TOTAL-SECS − ΣEXTINF > 1 s with no step → flag `timeline_uncertain`.
- **Muted** (`N-muted.ts`) → gap reason **`muted`**, not a chunk flag. Silence isn't
  stored/transcribed; `coverage()` already counts gaps as "never", and the detector reads "audio
  unknown", not "quiet". `--video`: video-only chunk. < 24 h: try `N.ts`. Bias: no music moments.

**Media.** Playlist URL from streamlink (`audio_only`, ~1.2 MB/min). 10 s TS/fMP4 segments, six per
~60 s chunk, ending early at a discontinuity, init change, muted run or the end, so chunks stay
contiguous and exact. ffmpeg copy → `.ts` + `.aac` at `storage.keys`; 403 → re-resolve, 404 → `gone`
(data kept). Jobs: `priority_for_range(default=BACKFILL)`.
- **Shared:** ffmpeg helpers → `clipper_core.ffmpeg`; chunk + gaps + job commit →
  `clipper_core.chunks.commit_chunk()`, used by the recorder too; plus a new
  `hls.parse_vod_playlist()`. **Duplicated:** the segment download with retries (live gives up fast,
  backfill retries long). The live loop, tracker and spool stay in the recorder.

**Chat replay.** GQL `VideoCommentsByOffsetOrCursor` → chat-ingest's rows, same code:
`MessageRow`/`MetricRow`/`SecondAggregator`/`ChatWriter` move to `clipper_core.chat` (so live and
backfill match), `message_emotes()` → `clipper_twitch.emotes` (takes parsed Twitch emotes).
- Mapping: `msg_id` = comment id. The user comes from `commenter` (null: kept, not a chatter).
  Twitch emotes come from fragments; 7TV/BTTV/FFZ match the *current* sets, so removed ones are
  lost. Badges: `{setID: version}`. Clock: `T0 + offset`.
- **Not recoverable:** deleted messages (backfill is quieter in spam/mod waves) and all events (no
  USERNOTICE/CLEAR*/ROOMSTATE; bits only as text). So: **no** `chat_events` or `viewer_snapshots`,
  which the detector reads as "unknown", not "none".

**Viewer clips (ground truth).** New table `viewer_clips` (writer: backfill tool, PK `(platform,
clip_id)`), from Helix Get Clips per stream window (top by views) and matched to our live or
backfilled stream via the VOD's `stream_id`. Span: `T0 + vod_offset` for `duration`, ± `slack_s`
(duration + 10 s: reported offset bug; + Helix T0 error). Re-runs fill a null `vod_offset`. Phase-2
evaluation: view-weighted recall of top clips, hit rate of top-N candidates (a precision lower
bound), and median (peak − clip start) for `chat_delay_s`. Muted seconds, flagged VODs excluded.

**Interfaces and tests.** Twitch access sits behind Protocols in `libs/twitch`, each with a fake:
`VodSource.playlist_url()` (raises `VodRestricted`/`VodGone`; real: streamlink) and
`ChatReplay.page(video_id, offset=|cursor=)` (real: `GqlChatReplay`, hash and client id overridable
via `CLIPPER_TWITCH_GQL_*`). Helix uses the existing client on `httpx.MockTransport`; segments are
built with lavfi; DB tests are `integration`. Smoke test (`twitch_live`, off unless
`CLIPPER_TWITCH_LIVE_TESTS=1`): one VOD, no rows, saves fixtures.

**Risks, politeness, budget.**
- GQL is undocumented, discouraged by Twitch for third parties, and its client ids can be revoked:
  anonymous use, isolated, personal research only, no redistributed media; `GqlBroken` stops a run.
- Politeness: GQL ≤ 2 req/s, one at a time, backoff, stop after 10 errors; ≤ 4 segment downloads.
- Budget per 6 h VOD: audio ≈ 430 MB (kept forever), chat ≈ 50 MB, GPU ≈ 10–20 min. Source
  video ≈ 23 GB: full `--video` needs `--yes` above 10 GB; `--max-gb` caps a run; video: 48 h rule.
- **User decisions:** GQL at all (else Helix metadata + clips only); channels and depth; audio or
  video; may backfilled clips be published (§2); the `muted` gap; no `chat_events`.

**Build plan.**
1. **5a Shared pieces + contract:** migration 0003, `GapReason.MUTED`,
   `clipper_core.{ffmpeg,chunks,chat}` (recorder + chat-ingest switched), controller guard,
   `parse_vod_playlist`. *Done when:* old tests pass unchanged and the parser and guard are tested.
2. **5b Twitch VOD access:** Helix `get_videos`/`get_clips`, `VodSource`, `GqlChatReplay` (limiter,
   retries), fakes, fixtures, smoke test. *Done when:* everything is tested offline and §3 holds the
   smoke answers (PDT, container, muted names, rounding, timezone).
3. **5c Media + time:** the CLI with `--dry-run`, a pure T0 estimator and chunk planner,
   `commit_chunk`. *Done when:* a mock VOD gives contiguous chunks, `muted` gaps and priority-0
   jobs; a crash plus a re-run gives identical rows; and a dry run writes nothing.
4. **5d Chat + clips (+ calibrate):** mapping, watermark resume, the live-chat skip, `clips`,
   DESIGN.md updates. *Done when:* fixture pages give the live aggregator's metrics, a resume adds
   no duplicates, and clips map to `T0 + vod_offset`.

### Proposed decision-log rows

| Date | Decision | Why |
|---|---|---|
| 2026-11-19 | One writer per table **and source**: everything belonging to a backfilled stream (`streams.source = 'backfill'`: the stream row, its chunks, gaps, chat messages, chat metrics) is written only by the backfill tool, and live services never touch it; derived tables (transcripts, audio metrics) keep one writer for all sources | backfill must write the live tables in the live format; two writers on the same rows would race |
| 2026-11-19 | The controller never updates a stream with `source = 'backfill'` (upsert `WHERE streams.source = 'live'`) | a stream id that shows up live again would flip it to live, and the recorder would write into it |
| 2026-11-19 | Backfill skips VODs of streams recorded live (clips only), and writes chat only where the channel has no live chat | live data is better (ms chat times, events, PDT); the `chat_metrics_1s` PK `(channel_id, ts)` can't hold both |
| 2026-11-19 | VOD time: `UTC = T0 + offset`. T0 from the chat anchor (median of `createdAt − contentOffsetSeconds`, ±0.5 s); Helix `created_at` as fallback and cross-check (> 30 s apart → Helix + flag); frozen per VOD in `vods.t0_utc` | VOD playlists have no PDT; the chat anchor reproduces Twitch's own replay sync |
| 2026-11-19 | VOD chat `sent_at = createdAt` (fallback `T0 + offset + 0.5 s`) | the same server clock as `tmi-sent-ts`, at ms resolution; `video ≈ chat − delay` holds with the delay learned live |
| 2026-11-19 | Muted VOD segments become a gap with reason `muted`; their audio is not stored or transcribed | silence would read as a quiet moment; `coverage()` and the detector already understand gaps |
| 2026-11-19 | Backfill takes audio_only by default; video only on request (full, or ranges added to existing chunks) | ~1.2 MB/min vs ~63 MB/min; detector development needs audio + chat |
| 2026-11-19 | Chat row types, per-second aggregation and the chunk commit move into `libs/core` (`chat`, `chunks`); ffmpeg copy helpers into `clipper_core.ffmpeg`; `message_emotes` into `clipper_twitch.emotes` | live and backfill must compute and commit identically; a tool must not import a service |
| 2026-11-19 | Viewer clips go into `viewer_clips` (writer: backfill tool) with span ± (duration + 10 s) | ground truth for the detector; Helix `vod_offset` is reportedly off by about the clip length |
| 2026-11-19 | Backfill writes no `chat_events` and no `viewer_snapshots` | replay has no USERNOTICE/CLEAR*/ROOMSTATE and there is no viewer history; the detector reads backfill as "unknown", not "none" |
| 2026-11-19 | GQL chat replay is isolated in `clipper_twitch.gql`: anonymous, ≤ 2 req/s, client id and hash overridable by env | an undocumented API can change or be revoked; a config change fixes most breaks |
| 2026-11-19 | Channels missing for a backfill are inserted inactive (`off`); existing rows are never changed | backfill must not start live monitoring |

### Contract changes

**Migration `0003_backfill`** (additive: two new tables, nothing else). `stream_gaps.reason` has no
CHECK, so `muted` needs no DDL. `streams`, `chunks` and `chat_messages` already have `source`. CHECK
texts are repeated in the models, as in 0002 (`db/tests/test_migrations.py` compares them).

**`models.py`**
```python
class Vod(Base):
    """A platform archive we backfilled, and the time anchor of its media. Writer: backfill tool.

    `t0_utc` = UTC of VOD offset 0 (= the stream's `started_at`), frozen on the first run; every
    backfilled time is `t0_utc + offset` (shifted per part after a timeline step, see flags).
    """
    __tablename__ = "vods"
    __table_args__ = (
        UniqueConstraint("channel_id", "platform_video_id"),
        UniqueConstraint("stream_id"),
        CheckConstraint(
            "media_status IN ('pending', 'done', 'restricted', 'gone', 'off')", name="media_status"
        ),
        CheckConstraint(
            "chat_status IN ('pending', 'done', 'skipped', 'unavailable', 'off')",
            name="chat_status",
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"))
    stream_id: Mapped[int] = mapped_column(ForeignKey("streams.id"))
    platform_video_id: Mapped[str] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    platform_created_at: Mapped[datetime]          # Helix created_at
    duration_s: Mapped[float] = mapped_column(Float)
    t0_utc: Mapped[datetime]
    t0_method: Mapped[str] = mapped_column(Text)   # contracts.T0Method
    t0_candidates: Mapped[dict[str, Any]] = mapped_column(server_default=sql_text("'{}'::jsonb"))
    #   {"helix": iso, "path_epoch": iso, "chat": iso, "chat_spread_s": 1.0, "comments": 812}
    rendition: Mapped[str | None] = mapped_column(Text)  # audio_only, 720p60, chunked, ...
    media_status: Mapped[str] = mapped_column(Text, server_default="pending")
    media_done_s: Mapped[float] = mapped_column(Float, server_default="0")  # chunks+gaps committed
    chat_status: Mapped[str] = mapped_column(Text, server_default="pending")
    chat_done_s: Mapped[int] = mapped_column(Integer, server_default="0")  # messages committed
    chat_metrics_to: Mapped[datetime | None]       # chat_metrics_1s rows before this are final
    flags: Mapped[dict[str, Any]] = mapped_column(server_default=sql_text("'{}'::jsonb"))
    #   {"t0_conflict": true, "timeline_uncertain_s": 3.2, "steps": [...], "muted_s": 840,
    #    "chat_time_fallbacks": 3, "comments_without_user": 12}
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _created_at()


class ViewerClip(Base):
    """Viewer-made platform clips: ground truth for the detector. Writer: backfill tool.

    `start_utc`/`end_utc` = the VOD's T0 + `vod_offset_s` (+ `duration_s`); the moment lies within
    ± `slack_s`. NULL while the platform hasn't assigned a VOD offset yet. `platform_created_at` is
    when the clip was made, not the moment.
    """
    __tablename__ = "viewer_clips"
    __table_args__ = (
        PrimaryKeyConstraint("platform", "clip_id"),
        Index(None, "stream_id", "start_utc"),
        Index(None, "channel_id", "platform_created_at"),
    )
    platform: Mapped[str] = mapped_column(Text, server_default="twitch")
    clip_id: Mapped[str] = mapped_column(Text)               # Twitch clip slug
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"))
    stream_id: Mapped[int | None] = mapped_column(ForeignKey("streams.id"))  # live or backfilled
    platform_video_id: Mapped[str | None] = mapped_column(Text)  # Helix "" -> NULL
    vod_offset_s: Mapped[int | None] = mapped_column(Integer)
    duration_s: Mapped[float] = mapped_column(Float)
    start_utc: Mapped[datetime | None]
    end_utc: Mapped[datetime | None]
    slack_s: Mapped[float | None] = mapped_column(Float)
    time_source: Mapped[str | None] = mapped_column(Text)  # vod_t0 | helix_created_at
    title: Mapped[str | None] = mapped_column(Text)
    creator_login: Mapped[str | None] = mapped_column(Text)
    view_count: Mapped[int] = mapped_column(Integer)
    is_featured: Mapped[bool] = mapped_column(server_default=sql_text("false"))
    platform_created_at: Mapped[datetime]
    fetched_at: Mapped[datetime] = _created_at()             # last refresh
```
Docstrings only (no DDL): `Stream`: "Writer: controller (`source='live'`), backfill tool
(`source='backfill'`)". `StreamGap` and `ChatMetricSecond`: the same split (recorder or chat-ingest
for live streams, the backfill tool for backfilled ones). `ChatEvent`: chat-ingest only.

**`contracts.py`**
- `GapReason.MUTED = "muted"`: VOD audio silenced by the platform (copyrighted music), so there is
  no usable audio. Video may exist as a video-only chunk.
- `class T0Method(StrEnum)`: `CHAT = "chat"`, `HELIX = "helix"`, `AUDIO = "audio"`.
- `class VodMediaStatus(StrEnum)`: `pending`, `done`, `restricted`, `gone`, `off`.
  `class VodChatStatus(StrEnum)`: `pending`, `done`, `skipped`, `unavailable`, `off`.
- `def backfill_lease(video_id: str) -> str: return f"backfill:vod:{video_id}"`.
- Unchanged and used as they are: `Priority.BACKFILL`, `DataSource.BACKFILL`, `dedupe_key`,
  `CHUNK_JITTER`.

**New `libs/core` modules (moved code, no behaviour change)**
- `clipper_core.chunks.commit_chunk(session, *, stream_id, seq, interval, video_key, audio_key,
  video_bytes, audio_bytes, source, gaps_before, default_priority) -> tuple[int, bool]`. It does the
  chunk upsert, inserts the gap rows only when the chunk is new, and enqueues the transcribe job
  with `priority_for_range` when the chunk has audio. It comes from `recorder.finisher.finish`,
  generalised from one gap to a list. The caller owns the transaction, so backfill can add its
  `vods` update to it.
- `clipper_core.chat`: `MessageRow` (`received_at: datetime | None`), `EventRow`, `MetricRow`,
  `SecondAggregator`, `ChatWriter`, moved unchanged from chat-ingest (`mapping`, `aggregator`,
  `writer`).
- `clipper_core.ffmpeg`: `copy()`, `extract_audio()`, `remux_to_ts()` from `recorder.finisher`.

**Outside `libs/core` (for completeness)**
- `clipper_twitch`:
  - `hls.parse_vod_playlist()`: `VodSegment(index, uri, offset_s, duration, muted, discontinuity,
    init_uri)` + `total_secs`.
  - `helix.get_videos()`, `get_clips()`, `parse_duration()`.
  - `gql` (the `ChatReplay` Protocol, `GqlChatReplay`, `CommentPage`, `GqlBroken`) and
    `vod.VodSource` (streamlink).
  - `emotes.message_emotes()`.
  - `TwitchSettings` gets `gql_client_id` and `gql_comments_hash`.
- Controller `poller._upsert_stream`: `... DO UPDATE ... WHERE streams.source = 'live'`, with
  `.first()` instead of `.one()`, and a log line when the row belongs to a backfilled stream.
- Root `pyproject.toml`: add `tools/backfill` to `members` (not `tools/*`: asr-bench stays outside),
  add it to `testpaths`, add `clipper_backfill` to `known-first-party`, and add the marker
  `twitch_live`. `compose.yaml`: a `backfill` service under `profiles: [tools]` (image with ffmpeg,
  like the recorder).

## 18. Detector (phase 2) — proposed 2026-11-19, not yet agreed

**Status:** proposal. Schema, scorer and live service can be built now with provisional defaults;
thresholds, lexicon and offline weights are tuned by backtest once step 5 and the transcriber
deliver data. v1 needs no chat-ingest change.

**Role.** `services/detector` (slim), one process for all channels, `CLIPPER_DETECTOR_MODE` =
`live|offline|all`. Writer of `candidates`, `chat_delay_estimates` (new), its own `media_requests`
rows and `judge`/`detect_offline` jobs. **One scorer, three callers:** `ChannelScorer.step(second)`
is a pure, clock-free state machine (time comes from the rows) run by the live loop, the offline
job and the backtest CLI, so a backtest reproduces live exactly. Tunables in one `DetectorParams`
model (code defaults, optional TOML); `detector_version` = code + params hash + lexicon version.

**1. Live scoring (stage 1: per-second stats).**
- **Input**: chat_metrics_1s of held channels by `channel_id` (`stream_id` is null until
  chat-ingest's 30 s sync; assign the stream by time); no row = 0. Final at `ts < now() − 8 s`
  (5 s grace + 1 s flush): ~8 s behind chat, not ~1 s as §8 says; harmless for the budget.
- **Activity** `A(t)` = Σ `unique_chatters` over the last 10 s. **Baseline** `B(t)` = median of `A`
  over `[t − 20 min, t − 60 s]` (not EWMA, which a long moment drags up; the last minute is left out
  so a moment doesn't raise its own bar); scale `S = max(1.4826·MAD, √B, 2)` (Poisson floor).
- **Viewers normalise, never trigger**: `B' = B · clamp(V_now / V_med, 0.67, 2)` (latest
  viewer_snapshot vs its median over the baseline window): covers the start ramp and raids.
- **Scores** `z = (A − B') / S`, `ρ = A / max(B', 3)`, floor `A ≥ max(8, 0.005·V)`. **Episode**
  opens at `z ≥ 4 ∧ ρ ≥ 2 ∧ floor`, stays open while `z ≥ 2`, closes after 10 s below (bursts
  < 10 s apart merge), force-closes at 180 s. *Suspect* (`z ≥ 3 ∧ ρ ≥ 1.5`) starts stage 2 early.
- **Time of stream**: the first 10 min after `streams.started_at` (and 5 min after a slow/emote/
  subs-only toggle) need `z ≥ 6 ∧ ρ ≥ 3`; until 20 min of history exist `B = max(own, prior)`,
  prior = median `A` of the channel's previous stream × viewer ratio. "hi"/"bye" floods: §3.
- **Missing chat**: a zero run ≥ 60 s where `B ≥ 5` is missing data, not fed to the baseline.
- **State in memory only** (per channel: ring buffer, viewers, open episode, recent candidates).
  Restart: replay `[now − 21 min, final]`, emit only episodes peaking in the last 15 min that
  overlap no candidate (±60 s); the offline pass catches longer outages.

**2. Emote lexicon.** `clipper_detector/data/lexicon.toml` in git, `version = N` (in
`detector_version`; ships with a cheap detector redeploy), applied to names at read time (§15).
`[emotes]` exact name → category: funny (KEKW, OMEGALUL), hype (Pog, EZ), shock (NOWAYING, D:),
tense (monkaS), sad (Sadge), cringe, wholesome (<3), greeting (peepoHey); `[words]` case-insensitive
(lmao, w, f, no way, hi, clip it); `[patterns]` regexes at weight 0.5 for 7TV variants (`kek|lul` →
funny); `[bots]`; `[transcript]` phrases; `[channels.<login>]` overrides. Unknown emotes count as
`uncategorised`; `clipper-detector lexicon --unknown` lists the most used (a table only once
non-developers edit it). **Moment type** = dominant category of per-message votes (one per message
and category, one per user; `emote_counts` counts tokens, so it's used only where raw chat isn't
read): top share ≥ 0.4, else `mixed`; < 5 votes `unknown`.

**3. Stage 2: raw chat and fake spikes.** On suspect/close, read the channel's `chat_messages`
`[onset − 20 s, peak + 5 s]` and `chat_events` `[onset − 60 s, peak + 5 s]` (index `channel_id,
sent_at`; raw rows commit ~5 s before their second's stats). **Clean uniques** = distinct users
without bots, `!commands`, deleted messages, one user's repeats; `z_clean` = `z` with `A ×
clean_fraction`; emitting needs `z_clean ≥ 4` (warm-up 6). **Shares**: `reaction` (category emote/
word), `greeting`, `command`, `bot`, `vote` (message = one number/letter or believe/doubt), `dup`
(top normalised text among *non-reaction* messages: mass "KEKW" is real, a copypasta is not),
`deleted`. **Score** (first guess): `z_clean · (0.6 + 0.8·min(reaction, 0.5))` + boosts (§4). A
matching rule stores the candidate as `suppressed` + reason (no judge job), to audit the filter:

| Reason | Rule (window `[onset − 20 s, peak]`) | Data stored today |
|---|---|---|
| raid | `raid` from `onset − 30 s`, unless `z_clean ≥ 8 ∧ reaction ≥ 0.5` | chat_events |
| subs | `submysterygift` with `mass-gift-count ≥ 5`, or ≥ 10 sub/resub/subgift | chat_events |
| bits | cheers ≥ 30 % of messages (≥ 1000 bits is a feature only: TTS reactions can be real) | chat_events |
| commands | command share ≥ 0.3 (`!enter` giveaways, bot games) | raw text |
| copypasta / vote | dup ≥ 0.4 or vote ≥ 0.4 (polls, predictions, "type 1 if…") | raw text |
| bots / greeting | bot share ≥ 0.3 / greeting share ≥ 0.4 | `user_login`, text |
| mode_toggle | `roomstate` change from `onset − 10 s` (also starts a 5 min warm-up) | chat_events |
| spam_wave | `clearchat`/timeouts and deleted ≥ 0.3 | chat_events, `deleted` |

**Backfilled streams** (§17) have no `chat_events`, `viewer_snapshots` or deleted rows: read as
*unknown* (no viewer scaling, event rules off, bits from `Cheer\d+` text; dup still catches raids).
**Not stored**: polls, predictions, hype trains, channel points (not on anonymous IRC; EventSub
needs the broadcaster's token), so vote/dup/command heuristics stand in; tags `first-msg`,
`custom-reward-id`, `msg-id=highlighted-message`. **Chat-ingest proposal for its next necessary
redeploy (v1 doesn't need it)**: those tags as `_first`/`_reward`/`_highlight` keys in `badges`
(`_action` precedent); `chat_metrics_1s` + ints `command_messages`, `top_text_count`,
`clip_mentions`, jsonb `emote_messages`: stage 1 filters without raw reads at scale; `NOTIFY` too.

**4. Explicit signals.**
- **"Clip it"**: every 5 s, scan new raw messages of held channels for `\bclip(ped| it| that|
  this)?\b|^!clip`; ≥ max(3, 3 % of clean uniques) users within 30 s → trigger (chat peak = first
  mention − 5 s). Clip URLs in chat (bot replies, viewer links) count as viewer clips.
- **Viewer clips**: the **detector** polls Helix Get Clips per held live channel every 60 s
  (`started_at = now − 10 min`; 1 point per call of the client id's 800/min, shared with the
  controller's ~2/min). Only it needs fresh clips: kept in memory + `candidates.features`, so
  `viewer_clips` stays backfill-only (§17). Live clips lack `vod_offset` for minutes → moment ≈
  `[created_at − duration − 5 s, created_at − 5 s]`. ≥ 2 creators overlapping within 90 s (3 above
  20 clips/h) → trigger.
- **Boosts** +2 (clip mentions), +1 (viewer clip). A signal within ±60 s of a candidate is attached
  to it; a *suppressed* one is then re-emitted (a viewer thought it was real).

**5. Merge, delay, media request.**
- **Emit at episode close** (≤ ~20 s after the peak): stable peak/window; the judge waits for
  T + 30 s anyway. **Peak** (chat time) = argmax of the 3 s centred mean of `unique_chatters`;
  **onset** = first second in `[open − 10 s, peak]` where that mean reaches a quarter of the rise.
- **Delay** `d` = `channels.chat_delay_s` (manual override) → median of the last 5 valid
  `chat_delay_estimates` → **8 s**; fixed per stream. `T = peak_utc = chat_peak − d`. **Window**
  (§7) `[min(T − 90 s, onset − d − 30 s), max(T + 30 s, close − d + 15 s)]`, at most 5 min.
- **Cooldown 60 s**: a weaker episode opening < 60 s after a candidate closed folds into it
  (`features.followups`); a stronger one is emitted with `features.prev_candidate_id`. **Budget**:
  ≤ 8 emitted per stream per rolling hour, then only above the lowest of them (caps LLM cost).
- **One transaction per candidate**: insert → `media.request_media(stream, window, Priority.TRIGGER,
  requested_by="detector")` → `jobs.enqueue(JUDGE, ref_id=id, dedupe_key=dedupe_key(JUDGE, id),
  priority=TRIGGER)` → commit. A 60 s sweep marks its requests fulfilled once `coverage(window)`
  is complete or 15 min after `window_to`. Until the judge exists: `CLIPPER_DETECTOR_JUDGE_JOBS=0`.

**6. Offline pass (v1) and chat delay.**
- **Job** `detect_offline` (ref = stream), enqueued by a 5 min sweep for ended streams with complete
  `coverage([started_at, ended_at])` (backfill: also `vods.chat_metrics_to ≥ ended_at` or chat not
  available) and no job for this version (dedupe `detect_offline:{stream}:{version}`).
- **Triggers** (video time, merged when peaks are < 30 s apart): (a) chat: the live scorer replayed,
  shifted by `d` (so backfilled streams get it too); (b) audio_metrics_1s: laughter ≥ 0.5 in 2 of
  4 s, screaming/shouting ≥ 0.6, cheering/gasp/crying ≥ 0.5 for 2 s, 3 s loudness − 5 min median
  ≥ 10 dB; not where music or gunfire_explosion ≥ 0.6; needs `z ≥ 2.5` in `[t + d − 5 s, t + d +
  15 s]` unless two groups fire or laughter ≥ 0.7 for 3 s (thresholds depend on the tagger picked);
  (c) transcript_segments text vs lexicon phrases, strongest the streamer saying "clip that".
- **Score** = max trigger score + 0.5 per extra kind; top 4 *new* candidates per stream-hour; judge
  priority `LIVE`/`BACKFILL`. **Deferred**: stories/rants chat didn't spike on (LLM/embeddings over
  transcript windows; needs judge/review labels), emotion models, an incremental pass while live.
- **Delay learning** (first step of each job): onsets of laughter/screaming/cheering ≥ 0.5 (≥ 20 s
  apart, not under music); event-triggered average of chat `z` at lags 1–30 s (cross-correlation of
  event train and chat excess); `d` = argmax after 3 s smoothing, valid with ≥ 15 events and a peak
  ≥ 1.5 above the median over lags. One `chat_delay_estimates` row per stream; its own offline
  candidates use it, later streams the median of the last 5. **Writer conflict**: the detector never
  writes `channels`; `channels.chat_delay_s` becomes the operator's manual override.

**7. Data model.** `candidates` holds detector output only (columns below). §7's `candidate` = an
emitted row with a judge job and no clip yet; from `waiting_for_media` on, the lifecycle lives in
the judge's `clips` (`candidate_id` unique FK): one writer each. Detector status is `emitted` /
`suppressed` (+ reason). The `judge` job wakes the judge (durable, prioritised, deduped; no new
NOTIFY); waiting clips LISTEN on `clipper_transcripts` (§16). Ground truth: §17's `viewer_clips`.

**8. Evaluation (backtest).** Truth: `viewer_clips` (§17) with a span (`start_utc`..`end_utc` ±
`slack_s`), top 10 per stream by views plus all ≥ its p75; hit = candidate peak inside the span.
Streams: backfilled ones (chat + audio only) and our own live-recorded ones once `backfill clips`
has attached their clips (the only set with events and viewers). At `k = ceil(4 × hours)` (also
2/h, 8/h): hit rate@k (precision lower bound: unclipped good moments exist), view-weighted
recall@k, median (peak − clip start) as a delay check, candidates/h, false suppressions per
reason; §17's exclusions apply (muted, `timeline_uncertain`). `clipper-detector backtest --stream
12 | --channel x --since 2026-11-01 [--params p.toml] [--grid z_on=3,4,5]`: read-only, a table.

**9. Service shape.** Loops: live 1 s (one query `channel_id = ANY(:held) AND ts > :last AND
ts < now() − 8 s` on the PK → scorers in ts order); explicit 5 s; streams/leases 10 s; sweeps 60 s /
5 min; offline `jobs.JobRunner` (CPU work in a thread); heartbeat 15 s. **Polling, not LISTEN**:
chat-ingest sends no NOTIFY, is rarely redeployed, and its 5 s grace dominates latency. **Leases**
`detect:channel:{id}` (TTL 60 s, renewed every 20 s; a stable `CLIPPER_WORKER_ID` retakes them).
**Heartbeat**: versions, channels held, max lag, emitted/suppressed by reason (1 h), offline jobs.
`CLIPPER_TWITCH_*` enables Get Clips. Controller: `STALE_AFTER` += judge 10 min, offline 30 min.

**10. Build plan.**
1. **Contracts + migration** (now). *Done:* `test_migrations` passes; payload/enum tests.
2. **Scorer, stage 2, lexicon** (now; pure, synthetic fixtures). *Done:* noise → nothing; 15 s ×4
   burst → one candidate, peak ± 2 s; start ramp → nothing; raid, mass gift, copypasta, `!enter`,
   "1/2" votes, greeting flood → suppressed with the right reason; replay from a later start → same.
3. **Live service** (now; defaults provisional). *Done:* integration test: stats + raw chat →
   candidate + media request + bumped transcribe jobs + judge job in one transaction; restart → no
   duplicate; a live run decides ≤ 10 s after a second is final.
4. **Backtest CLI** (needs step 5 data). *Done:* hit rate/recall@k for ≥ 10 streams, < 1 min each;
   then tune the defaults (**depends on real data**) and log them with their numbers.
5. **Offline pass, delay learning, explicit signals** (needs the transcriber running, the tagger
   choice, §17's `get_clips`). *Done:* delay within ± 3 s of a hand measurement on 3 channels;
   backtest reports offline vs chat-only recall (measured, no target yet).

### Proposed decision-log rows

| Date | Decision | Why |
|---|---|---|
| 2026-11-19 | Two-stage live detection: per-second stats find suspects, the raw chat of that window confirms and filters | exact unique users, copypasta, commands, bots, votes, "clip it" without changing or redeploying chat-ingest |
| 2026-11-19 | Baseline = 20 min rolling median of 10 s unique-chatter sums (last minute excluded), robust z + ratio, scaled by viewers | robust to the spike itself and to viewer ramps/raids; viewers only normalise |
| 2026-11-19 | One pure, clock-free scorer for live, offline and backtest; live state in memory, rebuilt from chat_metrics_1s | backtests reproduce live exactly; no state table |
| 2026-11-19 | Emit a candidate when its chat episode closes, not at the first crossing | stable peak/window; the judge waits for T + 30 s transcripts anyway |
| 2026-11-19 | Emote categories in a versioned `lexicon.toml` in the detector (names, words, patterns, bots, per-channel overrides) | ships with the often-redeployed detector; version in `detector_version` keeps backtests reproducible |
| 2026-11-19 | Fake-spike matches are stored as `suppressed` candidates with a reason, without a judge job | audit and tune the filter against viewer clips |
| 2026-11-19 | Detector writes `candidates`; judge writes `clips` (`candidate_id`); a `judge` job + `request_media(TRIGGER)` go in the candidate's transaction | one writer per table; durable, prioritised, deduped wake-up; no new NOTIFY channel |
| 2026-11-19 | Learned chat delay goes to the detector's `chat_delay_estimates` (per stream); `channels.chat_delay_s` is a manual override | keeps one writer per table; delay changes with the streamer's latency mode |
| 2026-11-19 | The detector polls Helix Get Clips for its live channels itself and keeps fresh clips in memory/candidate features; `viewer_clips` stays backfill-only | it is the only consumer of fresh clips; keeps `viewer_clips` single-writer (§17); 1 point/channel/min of the shared 800/min |
| 2026-11-19 | Backfilled streams are read as "events and viewers unknown": no viewer scaling, event rules off | §17 writes no `chat_events`/`viewer_snapshots`; "unknown" must not look like "none" |
| 2026-11-19 | Detector follows chat_metrics_1s by 1 s polling, no NOTIFY from chat-ingest | chat-ingest is rarely redeployed; its 5 s grace dominates latency |
| 2026-11-19 | Offline pass v1 = chat replay + audio events + transcript phrases; story/rant finder deferred | simple first; the finder needs labels from judge/review |

### Contract changes

All additive: one hand-written migration **`0004_detector`** (after §17's `0003_backfill`, which
creates `viewer_clips`), two new tables, no change to existing tables.

`contracts.py`:

```python
class JobKind(StrEnum):
    TRANSCRIBE = "transcribe"
    JUDGE = "judge"  # ref_id = candidates.id; enqueued by the detector with the candidate
    DETECT_OFFLINE = "detect_offline"  # ref_id = streams.id; enqueued by the detector's sweep

class CandidateSource(StrEnum):
    LIVE = "live"
    OFFLINE = "offline"

class CandidateStatus(StrEnum):
    EMITTED = "emitted"  # judge job enqueued
    SUPPRESSED = "suppressed"  # fake-spike rule or budget; see suppress_reason

class MomentType(StrEnum):
    FUNNY = "funny"
    HYPE = "hype"
    SHOCK = "shock"
    TENSE = "tense"
    SAD = "sad"
    CRINGE = "cringe"
    WHOLESOME = "wholesome"
    MIXED = "mixed"
    UNKNOWN = "unknown"

class JudgePayload(Payload):
    candidate_id: int

class DetectOfflinePayload(Payload):
    stream_id: int
    detector_version: str

def dedupe_key(kind: JobKind, ref_id: int, *parts: str | int) -> str:
    """One job per (kind, object[, parts]); extra parts for jobs that may run again, e.g. per
    detector version. `dedupe_key(kind, 5)` stays "kind:5"."""
    return ":".join(str(p) for p in (kind, ref_id, *parts))

def detect_lease(channel_id: int) -> str:
    """Lease a detector process holds while it scores a channel live."""
    return f"detect:channel:{channel_id}"
```

`models.py` (and the same in the migration):

- **`Candidate`** (`candidates`), writer: detector. `id` bigint PK; `stream_id` FK streams,
  `channel_id` FK channels (not null); `source` text CHECK IN ('live','offline'); `status` text
  CHECK IN ('emitted','suppressed'); `suppress_reason` text null (raid, subs, bits, commands,
  copypasta, vote, bots, greeting, mode_toggle, spam_wave, budget); `trigger` text (chat,
  clip_mentions, viewer_clips, audio, transcript; no CHECK, it grows); `peak_utc` timestamptz (video
  time); `chat_peak_utc` timestamptz null; `chat_delay_s` float; `delay_source` text (override,
  learned, default); `moment_from`, `moment_to` timestamptz (CHECK `moment_to >= moment_from`);
  `window_from`, `window_to` timestamptz (CHECK `window_to > window_from`); `score` float;
  `moment_type` text default 'unknown'; `features` jsonb default '{}' (z, ρ, baseline, uniques,
  viewers, shares, categories, top emotes, events, explicit signals, audio, transcript hits,
  followups); `detector_version` text; `media_request_id` FK media_requests null; `created_at`,
  `updated_at`. UNIQUE (`stream_id`, `source`, `peak_utc`); INDEX (`stream_id`, `peak_utc`); INDEX
  (`channel_id`, `created_at`).
- **`ChatDelayEstimate`** (`chat_delay_estimates`), writer: detector. `id` bigint PK; `channel_id`
  FK; `stream_id` FK; `method` text ('audio_events'); `delay_s` float; `events` int; `strength`
  float (peak above the median over lags); `valid` bool; `detector_version` text; `computed_at`
  default now(). UNIQUE (`stream_id`, `method`); INDEX (`channel_id`, `computed_at`).
- `viewer_clips`: unchanged from §17 (writer: backfill tool only); the detector only reads it.
- Docstrings only: `Channel.chat_delay_s` = manual override by the operator (learned values in
  `chat_delay_estimates`); `MediaRequest`: detector requests are linked from
  `candidates.media_request_id`, `clip_id` stays for the judge.

Outside core: controller `maintenance.STALE_AFTER` += `JUDGE: 10 min`, `DETECT_OFFLINE: 30 min`;
the detector uses `clipper_twitch.helix.get_clips` (added by §17) with `TwitchSettings`.
No new notify channel: the jobs NOTIFY (`clipper_jobs`) wakes the judge.
