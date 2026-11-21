# Clipper: design & decisions

Automated clipper for Twitch (Kick/YouTube later). It watches chat and viewers, finds
moments where chat spikes compared to the channel's own history, then records, transcribes,
judges, edits and publishes short vertical clips.

> **Status (2026-11-21):** Phase 1 / steps 1–5 done (workspace, shared core, schema, job queue,
> unified logging, Twitch lib, controller, chat-ingest, recorder, transcriber, backfill). The transcriber
> is built and tested end to end on throwaway data but not deployed yet (migration 0002 + the
> `transcriber` compose service, profile `gpu`). Step 5 (backfill tool, design §17 proposed)
> done 2026-11-20; it needs migration 0003 on the live DB before a real backfill.
> Phase 2 designs §18–§22 are proposed, not yet agreed; detector build item 1 (contracts +
> migration 0004, §18) built 2026-11-20, not on the live DB yet; build item 2 (scorer, stage 2,
> lexicon, merge: pure code in `services/detector`, synthetic tests) built 2026-11-20; build item 3
> (live service `clipper-detector`, compose profile `detector`, judge jobs off) built 2026-11-20,
> not deployed (needs 0004 on the live DB). Judge build item 6a (contracts + migration 0005 +
> `clipper_core.lifecycle`, §19) built 2026-11-20, not on the live DB yet; build item 6b (input
> builder + resolver, pure code in `services/judge`) built 2026-11-21; no judge service yet.
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
| Twitch VOD chat | Chat replay exists while the VOD exists; no official API (tools like TwitchDownloader use the internal GQL API, can break). Deleted messages removed; no viewer history. Use for backfill/backtesting; log live chat ourselves. Seen 2026-11-19 (smoke test, xqc archive, anonymous GQL `VideoCommentsByOffsetOrCursor` with the old mobile client id, no integrity error): pages of 53–56 comments; a page asked by offset starts a few s **before** it (41867 → 41864); `contentOffsetSeconds` is a JSON int; `createdAt` is UTC (`Z`, ms, trailing zeros dropped), no whole-hour shift in 216 comments; `createdAt − offset` stays in one 1 s band from the start to 11.6 h in (no drift): Twitch's chat T0 ≈ the video's GQL `createdAt` + 0.7 s if offsets are truncated (+ 1.2 s if rounded; not decidable from replay alone). 5c run: 338 comments from 6 pages spread over the 23 h: one band of 0.99 s, chat anchor (truncated) = `T0_pdt` + 3.916 s. 5d run (first 4 min, 2426 comments, 0 off the band by > 2 s, none without `createdAt` or commenter): `createdAt − T_chat − offset` ∈ [0.001, 0.987] s on the re-fetched boundary pages, i.e. consistent with truncated offsets; ~5.6 s of VOD per page at the stream start (~10 comments/s); cursor pages never repeated a comment. The smoke test compared against GQL `createdAt`; Helix `created_at` was not fetched (no credentials). A resub showed up as a plain comment carrying the system text; 7TV/BTTV emotes are plain text; badges with empty set id/version occur. |
| Twitch VOD HLS | Seen 2026-11-19 (smoke test, xqc archive 2932113307, 23 h, via streamlink): renditions `audio_only`, 160p30–1080p60 (`best` = source 1080p60). Media playlists `index-dvr.m3u8`: `PLAYLIST-TYPE:EVENT` + `ENDLIST`, segments `N.ts`, **MPEG-TS** (audio_only: AAC 48 kHz stereo + timed ID3, ~274 KB per 10 s; 1080p60: H.264 + AAC + ID3, ~10.5 MB per 10 s), EXTINF mostly 10.000 with some longer (TARGETDURATION 13), segment timestamps don't start at 0. **Only `audio_only` has `EXT-X-PROGRAM-DATE-TIME`** (every segment, ms), and it is exactly T0 + ΣEXTINF (zero deviation over 23 h, so it shows no gaps) with T0 = the video's GQL `createdAt` − 3.2 s (Helix not checked: no credentials); video renditions have none. `EXT-X-TWITCH-TOTAL-SECS` = ΣEXTINF (83735.25; `lengthSeconds` 83735). `#ID3-EQUIV-TDTG` (no zone) ≈ the VOD's *end* (+ 6 s), not its start. Path `<hash>_<login>_<stream_id>_<epoch>`: epoch = GQL `createdAt` − 6 s. Muted parts: `N-muted.ts`; `audio_only` and 1080p60 were compared: same count (1280 of 8343) and same first names (18–20-muted), full index sets not compared; a muted segment still has its AAC track. No `-unmuted` names, no discontinuities. fMP4 VODs not seen yet (xqc is TS live too). Unknown VOD id: no playback token (streamlink returns no streams); GQL `video: null`. 5c run (2026-11-19, same VOD, first 10 min): segments 18–35 muted, the stored `.aac` (ADTS copy of the TS audio) is ~1.27 MB per 60 s; anonymous GQL metadata (plain query, web client id) gives `createdAt`, `lengthSeconds`, `status`, `broadcastType`, owner, game and `seekPreviewsURL`, whose path `<hash>_<login>_<stream_id>_<epoch>` names the broadcast without a playlist (also for sub-only VODs, presumably). |
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
- The judge may extend context (cap: e.g. 10 min before / 2 min after, ≤ 2 extensions, so ≤ 3
  calls per clip). The clip
  goes back to `waiting_for_media`. If the stream ends, continue with what exists.
- Running jobs aren't interrupted (a 60 s chunk takes seconds).
- Own job queue in Postgres (`clipper_core.jobs`), not a library: `FOR UPDATE SKIP LOCKED`,
  `pg_notify` wake-ups (sent on commit), dedupe keys (re-enqueue only raises priority),
  retries with backoff (a handler raises `jobs.PermanentJobError` to fail a job at once when
  retrying can't help), stale-lock requeue.

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
| detector | live (reads per-second stats, ~10 s behind, §18) + offline (not built yet). Separate so tuning never restarts chat logging. Claims channels via leases | candidates, chat_delay_estimates, own media_requests | slim (profile `detector`) |
| judge | LLM verdict + edit plan, context requests (§19; contract + 0005 built, service not yet) | clips, clip_plans, judge_calls, own media_requests | slim |
| renderer | ffmpeg + NVENC, 9:16, captions | renders (later) | CUDA |
| review | FastAPI UI + Telegram bot | clip decisions (later) | slim |
| publisher | platform uploads, tokens, retries; later analytics | publications (later) | slim |
| tools/backfill | past VODs + chat replay + top clips, written in the live format; one-off CLI (`clipper-backfill`) | for backfilled streams: streams, vods, chunks, stream_gaps (5c), chat_messages, chat_metrics_1s (5d); viewer_clips (5d, all streams) | slim + ffmpeg (profile `tools`) |

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
4. ✅ transcriber: GPU worker for `transcribe` jobs. Words (UTC) → S3 JSON per chunk,
   sentences → `transcript_segments`, per-second sound classes + loudness → new
   `audio_metrics_1s` table, all in one transaction + NOTIFY. Chunk boundaries: the last
   sentence of a chunk that runs into the cut is *provisional* and is re-transcribed and taken
   over by the next contiguous chunk (no sentence is ever cut, no added latency).
   Models from `tools/asr-bench`: Whisper large-v3-turbo (faster-whisper), CED-base
   (provisional; ONNX fp16 on onnxruntime-gpu since 2026-11-20, no torch in the image). Silero
   VAD gate (onnxruntime, CPU), CUDA image, compose service (profile `gpu`).
   `clipper_core.transcripts.words()/coverage()` for consumers. E2e on 20 real chunks: 29×
   realtime overall (Whisper ~34×, CED ~1,470×, VAD ~270×), 2.60 GB GPU peak, Whisper at
   temperature 0 (deterministic).
   Full design: [§16](#16-transcriber-design-step-4).
5. ✅ backfill tool (`tools/backfill`, `clipper-backfill vod|channel|clips|status|calibrate`):
   past VODs written in the live format. Media: `audio_only` ~60 s chunks, T0 from the playlist's
   PDT (chat anchor + created_at as cross-checks), `muted` gaps, priority-0 transcribe jobs, one
   transaction per chunk with `vods.media_done_s`, lease + resume, `--dry-run`. Chat replay (GQL)
   → `chat_messages` / `chat_metrics_1s` through the live aggregator, watermark resume, skipped
   where live chat exists. Viewer clips (Helix) → `viewer_clips` (T0 + vod_offset ± slack).
   Shared code moved to `clipper_core.{chunks,chat,ffmpeg}`; migration 0003 (`vods`,
   `viewer_clips`, `streams` CHECKs). Tested on a real VOD (10 min media, 4 min chat); clips and
   `calibrate` still need a real run (Helix credentials; a stream recorded live). Design:
   [§17](#17-backfill-tool-step-5--proposed-2026-11-19-not-yet-agreed) (proposed 2026-11-19, not
   yet agreed).

**Phase 2**: detector (live + offline, emote categories, fake-spike filter) → judge (LLM,
context requests) → renderer (9:16, captions) → review (Telegram + web) → publisher →
analytics loop. Then Kick/YouTube. Detector build items 1–3 (contracts, scorer, live service)
built 2026-11-20, see §18.10; judge build item 6a (contracts, migration 0005, lifecycle helper)
built 2026-11-20, 6b (input builder + resolver, pure) 2026-11-21, see §19. Designs (all proposed 2026-11-19, not yet agreed):
detector [§18](#18-detector-phase-2--proposed-2026-11-19-not-yet-agreed), judge
[§19](#19-judge-phase-2--proposed-2026-11-19-not-yet-agreed), renderer
[§20](#20-renderer-phase-2--proposed-2026-11-19-not-yet-agreed), review
[§21](#21-review-phase-2--proposed-2026-11-19-not-yet-agreed), publisher
[§22](#22-publisher-phase-2--proposed-2026-11-19-not-yet-agreed).

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
| 2026-11-19 | Transcriber runtime (4b-3): faster-whisper's `vad_filter` **off**; **sequential** decoding of the gated stretches; model libraries as **optional extras** of clipper-transcriber (`whisper`, `ced`; only the CUDA image installs them); CED on the **CPU build of torch** | measured on the 20 benchmark chunks: `vad_filter` gave identical words and was 3 % slower (our Silero gate already runs in front); the batched pipeline was 1.2× faster but changed 1–2 % of the words; extras keep the dev env and tests free of torch/CUDA; the CPU build keeps the image ~5 GB smaller (CED speed is open, §16) |
| 2026-11-20 | **CED runs on the GPU**: ONNX fp16 on onnxruntime-gpu (CUDA 12 build, ORT's own feed), exported at image build time from the pinned weights (build fails unless the sha256 matches); no torch in the image. Supersedes 4b-3's CED on CPU torch, the `ced` extra and the PyTorch CPU index. CED itself stays **provisional** (the user decided only where it runs) | on the CPU CED was the slowest stage (24× realtime, ~2.4 s per chunk). Measured on the idle 4060 (20 bench chunks, 2 s windows / 1 s hop): ORT fp16 849× realtime, 505 MB GPU, 1.1 s load, venv 0.77 GB vs torch CUDA fp16 751×, 509 MB, 3.8 s, 5.4 GB; ONNX vs torch outputs: max group-score diff 0.0022, top-1 agreement 100 % of 1170 windows. The Hub's own `model.onnx` (int8 incl. the mel frontend, no −120 dB floor) is off by up to 0.075. In the pipeline: CED ~1,000–1,500× realtime, overall 13× → 29×, per-second classes within 0.003 of the CPU run (§16) |
| 2026-11-20 | Whisper decodes at **temperature 0 only** (faster-whisper's temperature fallback off; setting `whisper_temperature_fallback`) | measured on the 20 bench chunks: same words and benchmark agreement either way, the fallback re-decoded 4 of 190 segments per pass and made 2 of 20 chunks differ between two runs (once an invented "we'll be right back"); temperature 0 is identical run to run (retries, re-plans) and 3–5 % faster; no repetition loops either way. Trade-off: a low-scoring segment that isn't a repetition loop is now kept as decoded instead of re-decoded (the loop filter only drops repetition runs) (§16) |

## 16. Transcriber design (step 4)

**Status:** design agreed 2026-11-18. Models (2026-11-19, see decision log): speech **Whisper
large-v3-turbo** (faster-whisper) decided; sound classifier **CED-base** provisional until the user
confirms (where it runs is decided: on the GPU as ONNX, 2026-11-20). Both sit behind the engine
interfaces, so either can be swapped later.

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
with current PyAV); `language="en"`, `beam_size=5`, `word_timestamps=True`, `vad_filter=True`
(the benchmark's setting; the service runs it off behind its own Silero gate, see 4b-3 below),
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
   N.start_utc` within `CHUNK_JITTER`, 0.5 s), already transcribed, and has a provisional tail (`tail_from` not
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
- **Decode errors**: `audio.AudioError` (ffmpeg missing, OS error, timeout, killed by a signal:
  retry) vs `audio.DecodeError` (ffmpeg can't decode the file: give up on the chunk).

Details decided while building it (step 4b-2, job handler `clipper_transcriber.handler`, entry
`clipper-transcriber`):
- **Planning is a pure function** (`plan_window`) of N, P, S and their transcript rows.
  **Re-take**: when N already has a row starting before N's start and the contiguous P ends exactly
  there without a tail, an earlier attempt took P's tail over; the window starts at that
  `owned_from` again and P is left alone (else a retry/re-run would leave `[owned_from, N.start)`
  unowned). A takeover needs P to have audio and `window start >= P.owned_from` (degenerate rows
  are left alone).
- **Contiguous = at most `CHUNK_JITTER` (0.5 s) apart** (not 50 ms), like coverage's jitter
  bridging and the recorder's gap tolerance; setting `contiguity_tolerance_s`.
- **Window audio**: P's slice is cut or zero-padded to exactly `round((N.start − window start) ×
  16 kHz)` samples, so N's audio starts at the offset of N's start (jitter holes become silence,
  overlaps are cut).
- **Concurrency**: the commit takes `pg_advisory_xact_lock` per stream, re-reads P's, N's and S's
  transcripts and compares them with what the plan saw; any difference is a `HandoverConflict`:
  the handler plans again (up to 3 plans per job, decoded audio reused), then the job runner
  retries (backoff). Several workers may process neighbours at the same time without breaking
  the tiling (a randomised model of plan + commit checks this in the tests).
- **Words file inside the locked transaction**, after the re-check and before the DB writes: a
  stale attempt can never overwrite the file of a newer committed one. A transaction failing
  after the upload leaves the new file under the old row until the retry (same window for the
  same row state; the file carries its own `t0`).
- **After a takeover** P's segments are deleted where provisional or starting at or after
  `least(old tail_from, new owned_to)` (a tail inside a jitter overlap starts after N's start),
  the rebuilt ones inserted (capped case: sentences split from P's words file, no engine scores,
  P's model/language), and P's `segment_count` recounted. A tail dropped for the CHECK leaves no
  segment provisional.
- **`processing_s`** = pipeline time (VAD + speech + tagger), without download/decode; realtime
  factor = window audio / that (the heartbeat shows an EWMA, α = 0.2). A `DecodeError` becomes
  `jobs.PermanentJobError`: the job fails at once (a broken file never decodes), coverage keeps
  showing the chunk as untranscribed. Both audio downloads finish before the temp dir goes.
- **Engines by name** (`SPEECH_ENGINES`, `SOUND_TAGGERS` in `service.py`); "fake" only when set
  explicitly (defaults since 4b-3: "whisper" + "ced"). Heartbeat `info`: `models` (speech, tagger, vad), `device`,
  `realtime_factor`, `jobs_done`, `jobs_failed` (failed attempts), `current_chunk`, `queue`
  (queued transcribe jobs by priority, keys are the priority as text).

Details decided while building it (step 4b-3, model adapters, image, compose):
- **Adapters** `whisper.WhisperEngine` ("whisper") and `ced.CedTagger` ("ced") are the defaults of
  `CLIPPER_TRANSCRIBER_ENGINE` / `_TAGGER` ("none" turns sound classes off). Their libraries are
  **optional extras** of clipper-transcriber, imported only when the adapter is built: `whisper` =
  faster-whisper 1.2.1, ctranslate2 4.9.0 (as benchmarked), nvidia-ml-py; `ced` = torch 2.15.0 and
  torchaudio 2.11.0 from the **PyTorch CPU index** (explicit `[[tool.uv.index]]`; the CUDA build
  adds ~5 GB, PyPI's torchaudio loads `libcudart.so.13` at import) + transformers 5.23.0.
  `uv sync --all-packages` leaves them out (dev env and tests need no torch/CUDA; adapter tests
  fake the libraries). A missing extra raises `engines.MissingDependency`; the entry logs it and
  exits 1. *(2026-11-20: the `ced` extra and the PyTorch CPU index are gone, CED runs on
  onnxruntime-gpu; see "CED on the GPU" below.)*
- **Whisper**: loaded once at start plus a 1 s warm-up decode (a broken CUDA setup fails at start,
  not on the first job); calls serialised; float32 arrays in; word probability → confidence,
  segments keep avg_logprob / no_speech_prob / compression_ratio. **Revision pinned** to the
  benchmarked commit (`WHISPER_REVISIONS`, setting `whisper_revision`); the stored model id carries
  it: `faster-whisper/large-v3-turbo@0a363e9`. **`whisper_vad_filter` off**: on the 20 benchmark chunks (as 5
  contiguous streams) faster-whisper's own VAD behind our Silero gate gave identical words (1747)
  and segments and was 3 % slower (speech 40.7 s vs 39.3 s). **Sequential** `transcribe_many`:
  `BatchedInferencePipeline` (stretches as `clip_timestamps`, timestamps on) was only 1.2× faster,
  changed 1–2 % of the words and drops the temperature fallback.
- **GPU out of memory** (a game holding the VRAM): an engine call that fails with a CUDA
  allocation error is retried in-process after 30, 60, 120, 240, 150 s (`gpu_oom_wait_s` = 10 min
  in all; warnings with the GPU's memory), then the job attempt fails. Shutdown ends the wait at
  once (`Engines.request_stop`). The job lock (15 min sweeper) outlasts the wait.
- **CED**: Hub revision pinned (its code runs with `trust_remote_code`), 2 s windows every second,
  per-second max over windows, group max over labels, the model's own sigmoid kept. CPU by default
  (`tagger_device`), `tagger_threads` = 2 (set on every call: with OpenMP the thread count belongs to
  the calling thread), float16 when on CUDA (needs a CUDA torch, not in the image: `load` refuses
  `tagger_device=cuda` with a CPU-only torch). Its config takes the label names from
  `topel/ConvNeXt-Tiny-AT` on `main`, which the pin doesn't cover and the remote code can't pin, so
  `load` checks 527 labels with the 23 group labels at their AudioSet indices (`AUDIOSET_INDEX`)
  and refuses the model otherwise. **CPU speed:
  24× realtime on 2 threads** (13× on 1, 34× on 4, 50× on 8) — below the 30× aimed for: ~2.4 s per
  60 s chunk, more than Whisper's ~2 s. Not yet decided (CED is provisional anyway): more threads,
  the repo's `model.onnx` on onnxruntime, a smaller CED, or the tagger running alongside the speech
  engine. *(2026-11-20: replaced by CED on the GPU, see below.)*
- **Speed in the pipeline**: speech 31× realtime on window audio, slower than the benchmark's 49×
  because every gated stretch costs a full 30 s Whisper window (79 stretches in 20 min); overall
  13× with the tagger (VAD 4.6 s + speech 39 s + tagger 49 s for 1223 s of window audio). GPU: models
  take 2.2 GB (heartbeat `gpu`: device-wide used/total MB and utilisation via NVML, plus
  `tagger_device`; start log `gpu_memory_for_models_mb`). *(2026-11-20, CED on the GPU: overall
  29×, see below.)*
- **Logging**: every line is JSON. `faster_whisper` and `httpx2` (the hub's HTTP client) log at
  WARNING by default (core `DEFAULT_LEVELS`; one INFO line per call/request); transformers and
  huggingface_hub log through our handler (their own stderr handlers removed, no progress bars,
  `HF_HUB_DISABLE_PROGRESS_BARS=1`); the image has no NVIDIA entrypoint banner (`ENTRYPOINT []`).
- **Image** `services/transcriber/Dockerfile`: `nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04` (as the
  bench image, layers shared), uv 0.13.9, Python 3.12.15 via uv, ffmpeg + tzdata (psycopg's time
  zone lookup) via apt. Layers from slowest- to fastest-changing: apt, Silero, Python, the
  third-party venv (a `deps` stage syncs `--no-install-workspace` from the pyproject files + lock
  only), then the workspace packages with uv and only their sources bind-mounted (a code change adds
  ~1 MB instead of 1.7 GB; other repo changes rebuild nothing). **Silero baked in** by `ADD
  --checksum=sha256:…` (`/opt/silero_vad.onnx`, `CLIPPER_TRANSCRIBER_VAD_MODEL_PATH`; a test keeps
  URL + hash equal to `vad.py`'s pin): no GitHub access at runtime. `HF_HOME=/models/hf`. Size 10.3 GB as Docker counts it (7.3 GB shared
  with asr-bench, 3.0 GB unique; CUDA base ~4.5 GB unpacked). A read-only models volume needs
  `HF_MODULES_CACHE` on a writable path (transformers copies CED's Hub code there). *(2026-11-20:
  CED baked in as ONNX, no Hub code and no `HF_MODULES_CACHE` any more; see below.)*
- **Compose** `transcriber`: profile `gpu` (a bare `docker compose up -d` leaves it out;
  `docker compose up -d transcriber` enables it by name), `x-app-env`, `CLIPPER_WORKER_ID:
  transcriber-1`, GPU reservation (nvidia, 1, `[gpu]`), `models` volume at `/models`, after
  `migrate`, `restart: unless-stopped`, 30 s stop grace (the job in progress finishes). **Cold
  start** (checked on an empty volume, online): the first start downloads ~1.8 GB (turbo at the
  pinned commit, CED, the label CSV) and writes CED's code to `/models/hf/modules`; the first jobs
  then ran as usual. Unauthenticated Hub requests only log a warning (set `HF_TOKEN` for higher rate
  limits). *(2026-11-20: only Whisper is downloaded now, ~1.5 GB.)*
- **End-to-end check** (throwaway DB + bucket, the bench image's weights mounted read-only and
  offline): 20/20 jobs done in one attempt; coverage complete; owned ranges tile; 8 takeovers
  (0.8–7.4 s lead-in), no duplicate or lost words at the boundaries; 180 segments (none > 30 s, none
  overlapping), provisional tails only at stream ends; 240 metric rows per 4-minute stream
  (speech_ratio 0.0 and `music` top class on the music-only channel); words agree 93.7 % per chunk
  (87–96 % per stream) with the benchmark's turbo output of the same chunks.

Details decided while building it (2026-11-20, **CED on the GPU**, decision log; replaces the
CED-on-CPU parts of 4b-3 above: the `ced` extra, `tagger_device`, the label CSV, `HF_MODULES_CACHE`):
- **Evaluation** (main session, idle 4060, 20 bench chunks, 2 s windows / 1 s hop, batches of 32):
  ORT fp16 849× realtime, 505 MB GPU, 1.1 s load, 0.77 GB venv; torch CUDA fp16 751×, 509 MB,
  3.8 s, 5.4 GB. ONNX vs torch: max group-score diff 0.0022, top-1 agreement 100 % of 1170
  windows. The Hub's `model.onnx` is int8 (incl. STFT/mel), has no −120 dB floor and is off by up to
  0.075, so the graph is exported ourselves.
- **Model file**: exported at image build time (Dockerfile stage `ced-export`) from
  `mispeech/ced-base` @ db3e14a `model.safetensors` by `services/transcriber/ced_export/export_onnx.py`
  (the evaluation's script) with the pinned CPU toolchain of `ced_export/requirements.txt` (Python
  3.12.15, torch 2.15.0+cpu, torchaudio 2.11.0+cpu, transformers 5.23.0, onnxscript 0.7.2, onnx
  1.23.2): fp16 transformer, fp32 mel frontend inside the graph, opset 18, 172,665,049 bytes, sha256
  `76cbe72e…6a6dc6` (reproduced exactly by the first build). The build fails on any other sha256,
  `CedTagger.load` checks it again (`ced.CED_ONNX_SHA256`; a test keeps Dockerfile and code
  equal). Only the .onnx is in the image (`/opt/ced/ced-base-fp16.onnx`, setting
  `tagger_model_path`). The output sha256 also covers the weights (not checked on their own).
  The build stage needs Hub access (~345 MB). CED's Hub code there also downloads
  `topel/ConvNeXt-Tiny-AT`'s label CSV from its unpinned `main` (names only, not in the graph), so
  the build depends on that repo; `ced_export/check_labels.py` fails the build unless the 23 group
  labels sit at their `AUDIOSET_INDEX` positions (`group_labels.json`, kept equal to the table by a
  test). At runtime CED needs nothing from the Hub (no `trust_remote_code`, no label CSV, no
  `HF_MODULES_CACHE`).
- **Graph**: `waveform` float32 [batch, samples], 16 kHz mono, ≤ 161,760 samples (10.11 s; the
  graph itself only fails well above that, so the scorer refuses longer input with a ValueError) →
  `probs` float32 [batch, 527], already probabilities, index i = AudioSet class i.
  The −120 dB floor is batch-wide, as torchaudio's was (batches of 16, as before). The graph has no
  label names; they come from the pinned `AUDIOSET_INDEX` table (`config.json` has short names for
  75 labels), and `load` checks the output width (527) and the groups against it.
- **Packages**: onnxruntime-gpu 1.29.0 from ORT's CUDA 12 feed (built for CUDA 12.8; PyPI's ≥ 1.27
  are CUDA 13 builds whose CUDA provider can't load on the 12.8 base, and ORT then silently runs on
  the CPU). The transcriber depends on `onnxruntime==1.31.1; sys_platform != 'linux'` and
  `onnxruntime-gpu==1.29.0; sys_platform == 'linux'` (explicit index); the root pyproject's
  `override-dependencies = ["onnxruntime==1.31.1; sys_platform != 'linux'"]` drops faster-whisper's
  CPU `onnxruntime` on Linux (both packages write the same module). So the Windows dev env and the
  tests keep the CPU package for Silero, and the image has only onnxruntime-gpu (Silero's session
  stays on `CPUExecutionProvider`). The base image's CUDA 12.8 runtime, cuBLAS and cuDNN 9.8 serve
  CTranslate2 and ORT in one process.
- **Session**: CUDA provider first (`device_id` 0; `arena_extend_strategy` kSameAsRequested: the
  default power-of-two growth peaked at 633 MB instead of 505; `cudnn_conv_algo_search` HEURISTIC;
  `use_tf32` 0, which keeps the fp32 frontend exact), then CPU; `intra_op_num_threads` 2
  (`tagger_threads`; ORT keeps a dozen int64 shape ops on the CPU). **Guard**: `get_providers()[0]`
  must be CUDA, else `MissingDependency` (the entry exits 1), since a CUDA provider that can't load
  only logs a warning. `disable_cpu_ep_fallback` can't be the guard: those shape ops trip it.
  **GPU only**, no CPU mode: fp16 runs at ~6× on the CPU, and a CPU mode would need the fp32 export
  (344 MB more). One warm-up batch at load, as for Whisper. ORT logs errors only, for the session
  and process-wide (1.29 warns "No registered plugin EP device found for 'CUDAExecutionProvider'"
  at every start, harmless and not JSON); a provider that can't load is still logged as an error
  with the reason.
- **GPU out of memory**: ORT raises its own `onnxruntime_pybind11_state.Fail` (parent `Exception`).
  `gpu.is_out_of_memory` also accepts ORT's exception types and matches their messages ("Available
  memory of X is smaller than requested bytes of Y", "Failed to allocate memory", "CUDA failure 2:
  out of memory", "…_ALLOC_FAILED"), so the 4b-3 in-process wait covers CED unchanged.
- **Image**: venv 1.69 → 1.25 GB (no torch/torchaudio/transformers; onnxruntime-gpu 0.71 GB) plus
  the 173 MB model. Size as Docker counts it is unchanged (10.31 → 10.33 GB): the CUDA provider
  library is about as big as CPU torch was. A cold start downloads only Whisper (~1.5 GB).
- **End-to-end rerun** (as in 4b-3: throwaway DB and bucket, the 20 bench chunks as 5 contiguous
  streams, Whisper weights read-only and offline; the old CPU-CED image was rerun first as the
  reference): 20/20 jobs in one attempt, coverage complete, owned ranges tile, 8 takeovers (same
  lead-ins), 180 segments, words identical to the CPU run (1747; 93.7 % per-chunk agreement with
  the benchmark's turbo output). Speed on 1223 s of window audio: CED 48.3 s (25×) → 0.84 s
  (~1,470×; ~1,000× before the warm-up was added, when the first job paid for the arena), overall
  13.1× → 29.3× (VAD 4.5 s, speech 36.3 s). GPU: Whisper + CED peak 2.60 GB above the idle baseline
  (2.32 GB with CED on the CPU), 2.48 GB after loading. Per-second classes vs the CPU run: max
  difference 0.003 over 12,000 values, none above 0.01, the top class equal in all 1200 seconds.
  Every log line is JSON.
- **Whisper temperature**: faster-whisper's fallback re-decodes a stretch with sampling at
  temperatures 0.2 … 1.0 when its decode looks bad (compression ratio > 2.4 or avg_logprob < −1).
  Measured in the image on the 20 bench chunks (each on its own through the pipeline, two passes
  per setting, alternating): with the fallback, 4 of 190 segments per pass went through all
  temperatures (faster-whisper then keeps the best decode, mostly the temperature-0 one); per-chunk
  agreement with the benchmark was 94.0 % both ways, with 1755–1759 words vs 1755. With the
  fallback, 2 of 20 chunks differed between two passes (once an inserted "we'll be right back",
  once a spelling); at temperature 0 all 20 were identical. Speech took 35.0–36.5 s vs 34.2–34.6 s
  (3–5 % faster), and the raw output had no repetition loops either way. In the e2e both settings
  gave the same 1747 words, with speech 37.6 s vs 36.6 s. **Default: temperature 0 only**
  (`whisper_temperature_fallback` = false): the same text on every run (retries, re-plans,
  rebuilt words files) at no measured cost. Trade-off: the fallback re-decodes every
  bad-looking segment (compression ratio > 2.4 or avg_logprob < −1). The loop filter only drops
  repetition runs, so a low-scoring segment that isn't a loop is now kept as decoded (its
  avg_logprob is stored for consumers to filter). Caveat: the sample had no looping audio, so a
  loop the fallback would have rescued couldn't be observed.

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
`SoundTagger.classify(pcm) -> per-second group scores`. Real adapters: faster-whisper and CED
(step 4b-3, above); fake engines drive the tests.

**Edge cases.** Out-of-order processing (N+1 bumped and done before N): N+1 starts at its own
start; N then keeps no provisional tail (possible small glitch at that one boundary). Gap or
stream end after N: the provisional tail stays (best available). Requests in `media_requests`
are marked fulfilled by whoever asked, not by the transcriber.

**Runtime.** CUDA base image, model weights in a `models` volume (`HF_HOME`), GPU reserved in
compose. Heartbeat info: models, device, realtime factor, queue depth by priority. Crashed jobs
are requeued by the controller after 15 min (exists).

## 17. Backfill tool (step 5) — proposed 2026-11-19, not yet agreed

**Status:** proposal; 5a (shared pieces + contract), 5b (Twitch VOD access), 5c (media + time:
`tools/backfill`) and 5d (chat replay, viewer clips, `calibrate`) are built; `calibrate` has not
run on real data yet (needs a broadcast we recorded live, and the live DB/storage). The 5b smoke test (2026-11-19, one xqc archive) answered the playlist
and chat questions: see §3 (rows "Twitch VOD chat", "Twitch VOD HLS"); the time model below is
updated with it (5c: T0 from PDT, see "Time model").

**Role.** `tools/backfill` (`clipper-backfill`) runs natively (ffmpeg on PATH) or via
`docker compose run --rm backfill` (`tools` profile). It writes archives (VOD media + chat replay)
and viewer clips in the live format, so transcriber, detector and judge need no special case.

**Commands** (idempotent; `--dry-run` does all reads, prints the plan, writes nothing):
- `vod <id|url>... [--no-media] [--video [Q]] [--video-range A-B]... [--no-chat] [--no-clips]
  [--no-transcribe] [--max-minutes N] [--chat-max-minutes N]`: default is audio_only + chat +
  clips + jobs (in that order; clips only with Helix credentials). `--video` takes a video rendition
  and copies its audio out, like the recorder; `--video-range` adds video to the chunks it overlaps.
- `channel <login> [--since D] [--limit N]`: the newest N archives, **oldest first** (VODs expire).
- `clips <login|video>...`, `status`, `calibrate <video_id>` (read-only; a stream also recorded
  live): T0 methods vs live PDT by audio correlation, `createdAt` vs `tmi-sent-ts`.

**Skipped:** highlights/uploads (no `stream_id`), archives still live, streams with
`source = 'live'` (clips only), sub-only media (usher 403 → one `not_recorded` gap; chat tried).

**Idempotency and resume.** `streams` (unique on the archive's `stream_id`) and the new `vods` row
are created together and found again on a re-run; a lease `backfill:vod:{id}` keeps runs apart.
The stream row is created as `status = 'ended'` from the start (`ended_at` = T0 + duration): the
recorder and chat-ingest follow every `live` row, so a DB CHECK (`source = 'live' OR status =
'ended'`) keeps a backfilled stream from ever being live.
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

**Time model** (decided for 5c, 2026-11-19; replaces the chat-anchor default of this draft).
Offset `t` (ΣEXTINF) maps to `UTC = T0 + t`. The `audio_only` playlist carries PDT on every segment
(2026-11-19), but only as `T0_pdt + t`: an anchor (ms), not a clock that shows gaps. Video
renditions have none. T0 is frozen on the first run in `vods.t0_utc` (= `streams.started_at`), with
every candidate in `vods.t0_candidates` (pure estimator `clipper_backfill.t0`).
- **PDT (`T0Method.PDT`, default):** T0 = the `audio_only` playlist's first PDT − its offset.
  Live media times come from PDT, so VOD media follows the same clock. The chat anchor sits ~4 s
  later (3.9 s on the smoke VOD, 3.916 s in the 5c run) because Twitch's replay compensates stream
  latency; taking it for VOD media would silently cancel the chat delay (`video ≈ chat − delay`).
  Video renditions (`--video`) use the same T0: their segments line up with `audio_only`'s (the
  first run fetches the `audio_only` playlist for it).
- **Chat anchor (`T0Method.CHAT`, no PDT):** `x = createdAt − contentOffsetSeconds` over the
  comments of `t0_chat_pages` (6) pages spread over the VOD, whole-hour outliers (timezone bug)
  shifted back first, others > 30 s from the median dropped; offsets are whole seconds (verified),
  assumed truncated: T0 = median(x) − 0.5 s (rounded would be the median; still open:
  `calibrate`). Needs ≥ 20 comments. Always sampled on a first run as a cross-check.
- **created_at (`T0Method.HELIX`):** Helix `created_at`, or GQL `createdAt` without Helix
  credentials (`t0_candidates.gql_created_at`); fallback without PDT and chat, and when the chat
  anchor is > 30 s off it (old-VOD offset bug, flag `t0_conflict`). Path `<epoch>` (= GQL
  `createdAt` − 6 s): candidate `path_epoch`, logged only. TDTG (the VOD's *end*): ignored.
- **Flags** (`vods.flags`): `t0_chat_pdt` when |chat − pdt − 3.9 s| > 3 s; `t0_conflict` when
  created_at is > 30 s from T0; `pdt_steps` when the PDT jumps ≥ 1 s against `T0 + offset`
  (recorded, not applied: UTC stays `T0 + offset`); `chat_hour_fixed`, `chat_outliers`.
- **Audio** (`calibrate`, ±20 ms, needs a live recording) measures both methods' bias once.
- **Chat time:** `sent_at = createdAt` (ms, the `tmi-sent-ts` clock). Replay shows comments late by
  latency + reaction, like live, so `video_time ≈ chat_time − channel_delay` keeps the live delay.
  Missing or > 2 s off → the **chat clock** `T_chat + offset + 0.5 s` (5d: not the media T0, which
  is ~4 s earlier), `T_chat` = the frozen chat anchor (`t0_candidates.chat`), else `T0 + 3.9 s`
  for a PDT T0, else T0. ~1 s suffices (per-second features).
- **Gaps:** VODs have no ads. Each `EXT-X-DISCONTINUITY` starts a chunk (5c). Planned, not built:
  a *part* per discontinuity with T0 re-checked per part, a step ≥ 1 s becoming an `unknown` gap
  that shifts the rest (5c only flags PDT steps and counts `discontinuities`).
  TOTAL-SECS − ΣEXTINF > 1 s → flag `timeline_uncertain_s` (equal on the smoke VOD, which had no
  discontinuity; PDT can't reveal gaps, it is T0 + ΣEXTINF).
- **Muted** (`N-muted.ts`, verified; `audio_only` and 1080p60 had the same count and first
  names) → gap reason **`muted`**,
  not a chunk flag. Silence isn't
  stored/transcribed; `coverage()` already counts gaps as "never", and the detector reads "audio
  unknown", not "quiet". `--video`: video-only chunk. < 24 h: try `N.ts`. Bias: no music moments.

**Media.** Playlist URL from streamlink (`audio_only`: ~1.6 MB/min as TS, ~1.2 MB/min as `.aac`).
10 s segments (TS seen; fMP4 handled but not seen on a VOD yet), six per
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
  lost. Badges: `{setID: version}`. Clock: see "Chat time" (the chat clock, not `T0 + offset`).
- **Not recoverable:** deleted messages (backfill is quieter in spam/mod waves) and all events (no
  USERNOTICE/CLEAR*/ROOMSTATE; bits only as text; a resub appeared as a plain comment by the
  subscriber carrying the system text, so sub notices count as chat messages, not events). So: **no** `chat_events` or `viewer_snapshots`,
  which the detector reads as "unknown", not "none".

**Viewer clips (ground truth).** New table `viewer_clips` (writer: backfill tool, PK `(platform,
clip_id)`), from Helix Get Clips per stream window (top by views) and matched to our live or
backfilled stream via the VOD's `stream_id`. Span: `T0 + vod_offset` for `duration`, ± `slack_s`
(duration + 10 s: reported offset bug; + Helix T0 error). Re-runs fill a null `vod_offset`. Phase-2
evaluation: view-weighted recall of top clips, hit rate of top-N candidates (a precision lower
bound), and median (peak − clip start) for `chat_delay_s`. Muted seconds, flagged VODs excluded.

**Interfaces and tests.** Twitch access sits behind Protocols in `libs/twitch`, each with a fake:
`VodSource.renditions()`/`playlist_url()` (raises `VodRestricted`/`VodGone`/`VodRenditionMissing`/
`VodError`; real: `StreamlinkVodSource`) and `ChatReplay.page(video_id, offset_s=|cursor=)` (real:
`GqlChatReplay`, hash and client id overridable via `CLIPPER_TWITCH_GQL_*`; raises `GqlBroken`/
`GqlUnavailable`/`GqlRateLimited`/`ReplayUnavailable`). Helix uses the existing client on
`httpx.MockTransport`; segments are built with lavfi; DB tests are `integration`. Smoke test
(`libs/twitch/tests/test_live_smoke.py`, `twitch_live`, off unless `CLIPPER_TWITCH_LIVE_TESTS=1`):
one VOD, no rows, writes `report.json` and sanitized samples for `tests/fixtures/`.

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
   **Done 2026-11-19** (not deployed: migration 0003 + controller/recorder/chat-ingest rebuild).
   Deviations: 0003 also adds the two `streams` CHECKs above; `commit_chunk` returns
   `CommittedChunk(chunk_id, created, priority)` (the recorder logs the priority), takes any gap
   with `start/end/reason` (`chunks.Gap`), refuses a re-commit with another interval (ValueError)
   and keeps stored media keys a re-commit leaves None; the controller also never *ends* a
   backfilled stream; `VodSegment.program_date_time` (raw PDT), `VodPlaylist.media_sequence`/
   `ended`, `EXT-X-BYTERANGE` refused; chat-ingest keeps `aggregator` as a re-export, `writer` gone.
2. **5b Twitch VOD access:** Helix `get_videos`/`get_clips`, `VodSource`, `GqlChatReplay` (limiter,
   retries), fakes, fixtures, smoke test. *Done when:* everything is tested offline and §3 holds the
   smoke answers (PDT, container, muted names, timezone; offset rounding stays open for
   `calibrate`). **Done 2026-11-19.** Smoke run on xqc archive 2932113307 (twice: the first run
   failed in its own report code after the downloads; ~24 MB in all, media deleted). Answers in
   §3; still open: truncated vs rounded offsets (`calibrate`), fMP4 and sub-only VODs. Deviations:
   `get_videos`/`get_clips` return `HelixPage(items, cursor)` with `limit` and `after` (resume
   without skipping: pages ask only for what is still wanted), `parse_duration` → int seconds;
   `VodSource.renditions()` added (one streamlink resolution for all renditions) plus
   `VodRenditionMissing` and a `VodError` base; streamlink returns no streams for both "restricted"
   and "gone", so `StreamlinkVodSource` records usher's answers with a requests response hook on
   the streamlink session to tell them apart (also when streamlink raises after an usher error);
   streamlink is the optional extra
   `clipper-twitch[vod]` (imported lazily; chat-ingest/controller don't need it); GQL errors:
   `GqlBroken` (at once, or after retries for an unknown error message), `GqlUnavailable`,
   `GqlRateLimited` (a subclass of it), `ReplayUnavailable` (`video: null`, or comments missing
   after retries on a first page; mid-run it is `GqlUnavailable`); a hiccup error is retried even
   when it comes with partial data; `Comment.content_offset_s` is an int and a fractional value
   raises `GqlBroken` (the T0 estimate assumes whole seconds); `GqlChatReplay` refuses
   `min_interval_s` < 0.5 and paces per instance (share one per process); Helix paging raises
   `HelixError` on a repeated cursor; `Comment.twitch_emotes()` feeds `message_emotes`; the smoke
   test needs `ffprobe` (skips without) and finds its VOD anonymously via
   GQL (no Helix credentials in the worktree; the Helix part skips without them).
3. **5c Media + time:** the CLI with `--dry-run`, a pure T0 estimator and chunk planner,
   `commit_chunk`. *Done when:* a mock VOD gives contiguous chunks, `muted` gaps and priority-0
   jobs; a crash plus a re-run gives identical rows; and a dry run writes nothing.
   **Done 2026-11-19** (not deployed: the `backfill` compose service is defined, not built).
   `tools/backfill` (`clipper-backfill vod <id|url>... [--dry-run] [--no-media] [--video [Q]]
   [--max-minutes N] [--start-offset S] [--no-transcribe] [--no-chat] [--no-clips]`, `channel
   <login> [--since D] [--limit N]` (Helix credentials, else exit 2), `status [ids]`):
   - **T0** from PDT (see "Time model": a deviation from this draft, which had the chat anchor
     first): `T0Method.PDT` added to the contract.
   - **Planner** (`planner.plan_units`, pure): units = one chunk (≤ `chunk_seconds`, closed before
     a discontinuity, another init section, a muted or lost segment, the end or `--max-minutes`)
     with the gaps before it, or trailing gaps alone (a muted end); `UTC = T0 + offset` on the
     µs grid (neighbours share boundaries exactly). Muted runs → `muted` gap (with `--video`: a
     video-only chunk + its own `muted` gap); a segment still 404 after 2 tries → `not_recorded`
     gap (re-planned on the fly, `flags.lost_segments`); `--start-offset` → `not_recorded` gap
     before it; resume = plan again from `media_done_s` with the next seq.
   - **Pipeline:** `Downloader` (≤ 4 in flight, backoff 1 s doubling to 30 s, 6 tries, Retry-After;
     404/410 = lost; 403 and the rest stop the run, a re-run resolves again); concatenated TS (or
     init + fMP4 fragments, remuxed when video is kept) → `ffmpeg.extract_audio` → upload →
     one transaction: lease check + `commit_chunk(source=backfill, gaps_before, default_priority=
     BACKFILL, transcribe=...)` + `vods.media_done_s`/`media_status`/`flags`. Temp dir per run.
     An audio-only chunk whose audio can't be extracted stops the run (`MediaError`) instead of
     committing an empty chunk. `media_done_s` must be a segment boundary of the playlist used
     (`planner.check_aligned`): another rendition cut elsewhere (e.g. `--video` after an audio run)
     is refused (`refused`, nothing written) instead of producing overlapping chunks. Nothing
     left to plan and `media_done_s` at the end → `done` (a VOD restricted on its first run and
     playable later becomes `done`; `done` is never turned into `restricted`). `vods.flags` are
     only ever merged in SQL (`flags || ...`; `lost_segments` is incremented with the unit that
     commits the gap), never overwritten from a copy read at the start of the run.
     `chunk_seconds` ≤ 120 (a chunk's segments are held in memory). Temp dir per run
     (`clipper-backfill-*`, cleanup errors ignored); lease renewals touch it, and a run removes
     such dirs older than the lease TTL (left by killed runs). Logs go to stderr, the report and
     plan to stdout (errors and failed VODs to stderr).
   - **Rows:** stream (`backfill`, `ended`, `started_at = T0`, `ended_at = T0 + ΣEXTINF`) + vods
     created together under the lease; channel inserted inactive (`off`, `active = false`) when
     missing; stream id from Helix > the playlist's CDN path > GQL `seekPreviewsURL`'s path
     (`video:<id>` + flag `stream_id_unknown` if none); a broadcast recorded live → skipped, nothing
     written; highlights/uploads and archives still recording → skipped; sub-only → `restricted`
     + `not_recorded` gap over the rest (`media_done_s = duration`); `VodGone` with a row → `gone`.
   - **Lease** `backfill:vod:{id}`, owner `CLIPPER_WORKER_ID` (host-pid), TTL 120 s renewed every
     40 s in the background and checked in every commit; a holder that isn't us → `busy` before
     any download. A hard-killed run leaves its lease and temp dir: the next run waits for the TTL.
   - **Contract/libs (additive):** `commit_chunk(transcribe=True)`; `clipper_twitch`: GQL video
     metadata (`GqlChatReplay.video()`, plain query with the web client id
     `CLIPPER_TWITCH_GQL_WEB_CLIENT_ID`, same pacer; `VideoInfo`; fake), `vod.parse_vod_path()`,
     `HelixClient.get_video()`, `HelixError.status`. `--no-transcribe` chunks get their jobs on the
     next run without the flag. `--yes`/`--max-gb`/`--video-range` not built yet.
   - **Tests:** 75 in `tools/backfill` (lavfi TS segments on a mock CDN + fakes: contiguous chunks,
     `muted` gap, priority-0 jobs, crash after a commit and before one + re-run = rows identical to
     a clean run, dry run writes nothing, restricted (also after `done`, and playable later),
     recorded-live skip, lease busy/expired/lost between commits, stop between units, `MediaError`,
     a rendition cut elsewhere refused, flags merged, lost segment, `--video` with a video-only
     muted chunk, `--max-minutes`/`--start-offset`/`--no-transcribe`/`--no-media`, gone; T0 cases
     incl. the real smoke fixtures; planner edges; metadata/broadcast from GQL + Helix; stale temp
     dirs). GQL `video: null` *with* errors is retried and then `GqlBroken`, never "gone".
   - **Coverage of pending VODs (note, no code yet):** a backfilled stream is `ended` from the
     start, so `transcripts.coverage()` reports the part not fetched yet, `[T0 + media_done_s,
     T0 + duration)`, as `gap` (after the last chunk of an ended stream), not as `unrecorded`.
     Consumers of backfilled streams (detector, judge, evaluation) must check
     `vods.media_status`/`media_done_s` (or `streams.source = 'backfill'` + the vods row) before
     reading "gap" as "nothing there". Proposal for later: `coverage()` reads `vods.media_done_s`
     for backfilled streams and reports the rest as `unrecorded` while `media_status = 'pending'`.
   - **Real run** (2026-11-19, xqc archive 2932113307, throwaway DB + bucket, dropped afterwards):
     T0 = `T0_pdt` 06:27:49.784 (8343 PDT points, max deviation 0), chat anchor + 3.916 s (338
     comments, spread 0.99 s), GQL `createdAt` + 3.216 s, path epoch − 2.784 s; run 1
     (`--max-minutes 5`, ~15 s): chunks [0, 180) + `muted` [180, 300); run 2 hard-killed after 2
     chunks ([360, 480), `muted` [300, 360)); run 3 right away → `busy`; after the 30 s test TTL it
     resumed with seq 5 ([480, 600)); `--dry-run` then planned seq 7..11 from 600 s. 7 chunks, all
     contiguous with the gaps, `.aac` 1.25–1.29 MB per 60 s (8.9 MB), 7 jobs at priority 0. ~16 MB
     downloaded in all (each run: ~0.6 MB playlist + streamlink's resolution). Adjacent muted gaps
     split at run boundaries stay two rows.
4. **5d Chat + clips (+ calibrate):** mapping, watermark resume, the live-chat skip, `clips`,
   DESIGN.md updates. *Done when:* fixture pages give the live aggregator's metrics, a resume adds
   no duplicates, and clips map to `T0 + vod_offset`.
   **Done 2026-11-19** (not deployed; no migration: 0003 has every column). `vod` runs media, then
   chat, then clips; `clipper-backfill clips <login|video>...` and `calibrate <video>` are new.
   - **Chat** (`clipper_backfill.chat`): pages `VideoCommentsByOffsetOrCursor` from 0 (or the
     resume offset) by cursor; `map_comment` gives chat-ingest's `MessageRow` (`source='backfill'`,
     `received_at` NULL) and `SecondAggregator.add_row` counts it exactly like live (new
     `add_row`, which chat-ingest now uses too). Emote sets fetched once per run
     (`HttpEmoteSets`; tests `StaticEmoteSets`). **Watermark:** comments come in offset order;
     after a page whose last offset is L, a batch commit (every `chat_batch_pages` = 20 pages, at a
     limit, at a stop and at the end; one transaction with the lease check) writes the messages
     with offset < L (those at L wait), the metrics of seconds before
     `M = second(T_chat + L − 3 s)`, `chat_done_s = L`, `chat_metrics_to = M` and the flag
     counters (`chat_time_fallbacks`, `comments_without_user`, `chat_late_dropped`) of exactly the
     committed rows. A resume pages from `M − T_chat − 3.5 s` (≈ L − 7 s), counts only comments with
     `sent_at ≥ M` and inserts only those with offset ≥ `chat_done_s`: a crash loses at most a
     batch and the rows equal one clean run's (tested with crashes after batches, `--max-minutes`
     slices, a GQL error mid-run). At the end: `done`, `chat_done_s ≥ duration`,
     `chat_metrics_to ≥ max(ended_at, last second + 1)`. Statuses: `pending` → `done` /
     `unavailable` (first page: no replay) / `skipped` (guard 2) / `off` (`--no-chat` on a first
     run); a later run retries anything but `done`.
   - **Guard 2:** `chat_metrics_1s` rows of the channel in `[min(T0, T_chat) − 10 s,
     max(T0, T_chat) + duration + 10 s]` that are not the VOD's stream (a live stream, or none:
     chat-ingest logs active channels while offline): at least `chat_guard_min_s` (60) of them →
     `skipped`, nothing written; fewer (stray seconds) → the chat is backfilled, those seconds
     stay theirs (`ChatWriter(keep_foreign_metrics=True)` never overwrites another stream's or a
     stream-less metrics row) and `flags.chat_foreign_seconds` counts them.
   - **Quality flags:** a third-party emote provider that fails (FFZ is unreachable from the dev
     PC) doesn't stop the chat (its emotes are simply not counted, as live): `flags.
     emote_sets_failed` lists the providers of every run so far (a run proceeds and flags rather
     than refusing; review decision 2026-11-20). `flags.chat_time_fallback_share` while more than
     5 % of the VOD's messages got the clock's time (checked after each run).
   - **Clips** (`clipper_backfill.clips`, writer of `viewer_clips`): Helix Get Clips over the
     broadcast's window ± 2 min, top `clips_top` (100) by views, kept when cut from that VOD (or
     from no VOD); linked through the VOD: our `vods` row (`time_source = vod_t0`, slack =
     duration + 10 s) else Helix Get Videos' `stream_id` → our live stream (`helix_created_at`,
     slack + 5 s). Upsert: counters/titles refreshed, link and span only overwritten by known
     values (a null `vod_offset_s` gets filled, never erased); `stream_id` keeps its first value
     (only a clip without a known VOD, found by two overlapping stream windows, could be claimed
     twice). Clip ids repeated by Helix are written once; clips of other videos are dropped before
     any lookup. `clips <login>` covers every stream of ours of the channel (live and backfilled).
     Without Helix credentials: skipped with a line in the report (`clips` exits 2). A VOD
     recorded live gets its clips only. A clips failure never fails a `vod`/`channel` run (media
     and chat are committed by then): logged, `flags.clips_error` until a fetch succeeds.
   - **Calibrate** (`clipper_backfill.calibrate`, read-only, numpy): live chunks spread over the
     stream whose ± 20 s search range lies inside the VOD and has no muted segment; the VOD's
     `audio_only` segments around each are decoded (ffmpeg, mono 8 kHz) and the chunk's audio is
     located by normalised FFT cross-correlation: `T0_audio = chunk.start_utc − VOD offset of the
     match` (median of the windows with a score ≥ 0.5). Table: every T0 candidate (PDT, chat
     anchor truncated and rounded, created_at, path epoch) minus `T0_audio`; replay comments of
     the T0 sample found in our live chat by msg_id: `createdAt − tmi-sent-ts` and
     `tmi-sent-ts − (T0_audio + offset)`. Synthetic test (noise cut two ways from one AAC
     encode): T0 recovered to < 1 ms, a 137 ms PDT error shown as +0.137. On real data read
     `T0_audio` as ± one AAC frame (~21 ms): the match is sample-exact, but neither the live
     `.aac` (ADTS) nor the playlist offsets carry the audio PTS (no PTS correction: the live side
     has none to correct with). Near-silent windows score 0, scores ≤ 1; its temp dir
     (`clipper-calibrate-*`) is not a backfill temp dir; no T0 candidate at all → exit 2.
     **To run once by the user** on a broadcast recorded live (needs the live DB + storage):
     `clipper-backfill calibrate <video_id>`; it settles truncated vs rounded offsets and
     `T0_pdt`'s bias.
   - **Deviations:** the chat-time fallback uses the chat clock (above), not `T0 + offset`;
     `--max-minutes` limits media and chat (each from its own resume point), `--chat-max-minutes`
     chat alone; `--start-offset` is media only (chat always starts at 0: missing seconds would
     read as "no chat"); `--no-media` needs no object storage; `Comment.created_at` may be None
     (missing `createdAt` → fallback instead of `GqlBroken`); `ClipTimeSource` enum in contracts;
     `viewer_clips.creator_login` holds Helix's `creator_name` (Helix has no creator login);
     `vods.flags` gets `chat_time_fallbacks`/`comments_without_user`/`chat_late_dropped` only when
     non-zero; the report prints the GQL request count; `clipper_core.logs` prints level names
     upper-case (importing streamlink renames the stdlib levels to lower case: backfill logs said
     `warning`, and `test_logs` failed when `test_vod` ran first). Not built: `--yes`/`--max-gb`/
     `--video-range` (5c), clips in `--dry-run` (described, not fetched).
   - **Tests:** 101 in `tools/backfill` (+26), 1 in core, 1 in `test_emotes`: the fixture pages
     through chat-ingest's real IRC path (`ChatIngest.on_message`) and through `map_comment` give
     identical rows and metrics; chat rows/metrics/emotes/badges/fallbacks/deleted users; crash
     after batches + resume = one clean run (messages, metrics, flags); `--chat-max-minutes`
     slices; guard 2 (a minute of live chat → skipped, two stray seconds → kept + flagged);
     failed emote sets and the fallback share flagged; `--no-chat` → `off` → done; unavailable;
     GQL error mid-run; watermark math (2000 random cuts); clips (span, fill, never erase,
     repeated ids, first stream kept, live stream by Helix `stream_id`, unknown VOD, a Get Clips
     failure that doesn't fail the run); `vod` with clips and a live-recorded VOD (clips only);
     calibrate (synthetic audio, muted windows, needs a live recording, silence scores 0, temp dir
     prefix, CLI `ValueError` → exit 2); `ChatWriter(keep_foreign_metrics)`.
   - **Real run** (2026-11-19, xqc archive 2932113307, chat only, throwaway DB on the test
     Postgres, dropped afterwards; no Helix credentials, so clips were skipped): run 1
     `--no-media --max-minutes 1.5` (16 pages, 829 messages, 23 GQL requests incl. metadata + the
     6 T0 sample pages); run 2 `--max-minutes 2` hard-killed (TerminateProcess) mid-run after 3
     committed batches (of 5 pages); run 3 at once → `busy` (0 requests); after the 30 s test TTL run 3 resumed at offset
     141 (watermark 149, 94 comments read again) and ended at 241 s. Result: 2426 messages, 2426
     distinct ids, 905 chatters, 215 metric seconds, all before `chat_metrics_to`, and all 215
     equal to a recomputation from the stored messages (messages, unique chatters, emote counts);
     emote tokens 7TV 1615, Twitch 465, BTTV 6 (FFZ timed out again: its sets were empty; top:
     OOOO, xqcL, AURA, LOL, agahi); 0 fallbacks, 0 comments without user, no `chat_events`.
     Re-fetching the pages at both run boundaries (offsets 83–92 and 143–152): no comment
     missing. ~70 GQL requests in all (+ streamlink's resolution per run, one 0.6 MB playlist,
     the emote sets).

### Proposed decision-log rows

| Date | Decision | Why |
|---|---|---|
| 2026-11-19 | One writer per table **and source**: everything belonging to a backfilled stream (`streams.source = 'backfill'`: the stream row, its chunks, gaps, chat messages, chat metrics) is written only by the backfill tool, and live services never touch it; derived tables (transcripts, audio metrics) keep one writer for all sources | backfill must write the live tables in the live format; two writers on the same rows would race |
| 2026-11-19 | The controller never updates a stream with `source = 'backfill'` (upsert `WHERE streams.source = 'live'`) | a stream id that shows up live again would flip it to live, and the recorder would write into it |
| 2026-11-19 | Backfill skips VODs of streams recorded live (clips only), and writes chat only where the channel has no live chat | live data is better (ms chat times, events, PDT); the `chat_metrics_1s` PK `(channel_id, ts)` can't hold both |
| 2026-11-19 | VOD time: `UTC = T0 + offset`, T0 frozen per VOD in `vods.t0_utc`. T0 = the `audio_only` playlist's PDT (`T0Method.PDT`; video renditions use the same T0); without PDT the chat anchor (median of `createdAt − contentOffsetSeconds` − 0.5 s), then Helix/GQL `created_at` (also when the chat anchor is > 30 s off it); all candidates in `vods.t0_candidates`, disagreements (chat − pdt not ≈ 3.9 s ± 3 s, created_at > 30 s off, PDT steps) in `vods.flags` | live media times come from PDT; the chat anchor sits ~4 s after `T0_pdt` (Twitch's replay compensates latency: 3.9 s smoke test, 3.916 s 5c run), so using it for media would silently cancel the chat delay (`video ≈ chat − delay`). `calibrate` (5d) still checks `T0_pdt` against a live recording |
| 2026-11-19 | VOD chat `sent_at = createdAt` (fallback, when missing or > 2 s off: the chat clock `T_chat + offset + 0.5 s`, `T_chat` = the frozen chat anchor) | the same server clock as `tmi-sent-ts`, at ms resolution; `video ≈ chat − delay` holds with the delay learned live; the media T0 is ~4 s earlier than the chat clock |
| 2026-11-19 | Backfilled chat metrics are final only before `vods.chat_metrics_to` (`second(T_chat + L − 3 s)` after the comments up to offset L), committed with the messages before L; a resume re-reads from ~7 s before | a crash or a `--max-minutes` slice never double counts nor loses a second; same rows as one clean run |
| 2026-11-19 | Muted VOD segments become a gap with reason `muted`; their audio is not stored or transcribed | silence would read as a quiet moment; `coverage()` and the detector already understand gaps |
| 2026-11-19 | Backfill takes audio_only by default; video only on request (full, or ranges added to existing chunks) | ~1.2 MB/min vs ~63 MB/min; detector development needs audio + chat |
| 2026-11-19 | Chat row types, per-second aggregation and the chunk commit move into `libs/core` (`chat`, `chunks`); ffmpeg copy helpers into `clipper_core.ffmpeg`; `message_emotes` into `clipper_twitch.emotes` | live and backfill must compute and commit identically; a tool must not import a service |
| 2026-11-19 | Viewer clips go into `viewer_clips` (writer: backfill tool) with span ± (duration + 10 s) | ground truth for the detector; Helix `vod_offset` is reportedly off by about the clip length |
| 2026-11-19 | Backfill writes no `chat_events` and no `viewer_snapshots` | replay has no USERNOTICE/CLEAR*/ROOMSTATE and there is no viewer history; the detector reads backfill as "unknown", not "none" |
| 2026-11-19 | GQL chat replay is isolated in `clipper_twitch.gql`: anonymous, ≤ 2 req/s, client id and hash overridable by env | an undocumented API can change or be revoked; a config change fixes most breaks |
| 2026-11-19 | Channels missing for a backfill are inserted inactive (`off`); existing rows are never changed | backfill must not start live monitoring |

### Contract changes

**Migration `0003_backfill`** (additive: two new tables, and two CHECKs on `streams`:
`source IN ('live', 'backfill')` and `source = 'live' OR status = 'ended'`; every existing row is
live). `stream_gaps.reason` has no CHECK, so `muted` needs no DDL. `streams`, `chunks` and
`chat_messages` already have `source`. CHECK texts are repeated in the models, as in 0002
(`db/tests/test_migrations.py` compares them).

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
- `class T0Method(StrEnum)`: `PDT = "pdt"` (added in 5c, the default), `CHAT = "chat"`,
  `HELIX = "helix"`, `AUDIO = "audio"`.
- `class VodMediaStatus(StrEnum)`: `pending`, `done`, `restricted`, `gone`, `off`.
  `class VodChatStatus(StrEnum)`: `pending`, `done`, `skipped`, `unavailable`, `off`.
- `def backfill_lease(video_id: str) -> str: return f"backfill:vod:{video_id}"`.
- `class ClipTimeSource(StrEnum)` (5d): `VOD_T0 = "vod_t0"`, `HELIX_CREATED_AT =
  "helix_created_at"` (`viewer_clips.time_source`).
- Unchanged and used as they are: `Priority.BACKFILL`, `DataSource.BACKFILL`, `dedupe_key`,
  `CHUNK_JITTER`.

**New `libs/core` modules (moved code, no behaviour change)**
- `clipper_core.chunks.commit_chunk(session, *, stream_id, seq, interval, video_key, audio_key,
  video_bytes, audio_bytes, source, gaps_before, default_priority, transcribe=True) ->
  CommittedChunk` (`transcribe=False`: no job, for `--no-transcribe`; added in 5c). It does the
  chunk upsert, inserts the gap rows only when the chunk is new, and enqueues the transcribe job
  with `priority_for_range` when the chunk has audio. It comes from `recorder.finisher.finish`,
  generalised from one gap to a list. The caller owns the transaction, so backfill can add its
  `vods` update to it.
- `clipper_core.chat`: `MessageRow` (`received_at: datetime | None`), `EventRow`, `MetricRow`,
  `SecondAggregator`, `ChatWriter`, moved unchanged from chat-ingest (`mapping`, `aggregator`,
  `writer`). 5d (additive): `SecondAggregator.add_row(row)` (chat-ingest uses it too) and
  `ChatWriter(keep_foreign_metrics=True)` (the backfill: never overwrite another stream's or a
  stream-less metrics row).
- `clipper_core.ffmpeg`: `copy()`, `extract_audio()`, `remux_to_ts()` from `recorder.finisher`.

**Outside `libs/core` (for completeness)**
- `clipper_twitch`:
  - `hls.parse_vod_playlist()`: `VodSegment(index, uri, offset_s, duration, muted, discontinuity,
    init_uri)` + `total_secs`.
  - `helix.get_videos()`, `get_video()` (5c), `get_clips()`, `parse_duration()`.
  - `gql` (the `ChatReplay` Protocol, `GqlChatReplay`, `CommentPage`, `GqlBroken`) and
    `vod.VodSource` (streamlink).
  - `emotes.message_emotes()`.
  - `TwitchSettings` gets `gql_client_id` and `gql_comments_hash` (+ `gql_web_client_id` in 5c
    for the anonymous video metadata query `GqlChatReplay.video()`); `vod.parse_vod_path()` (5c).
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
model (code defaults, optional TOML); `detector_version` = code + params hash + lexicon version
(and file hash; build item 2).

**1. Live scoring (stage 1: per-second stats).**
- **Input**: chat_metrics_1s of held channels by `channel_id` (`stream_id` is null until
  chat-ingest's 30 s sync; assign the stream by time); no row = 0. Final at `ts < now() − 10 s`
  (5 s grace + 1 s flush + margin; 8 s as first proposed left ~1 s; built: build item 3): ~10 s
  behind chat, not ~1 s as §8 says; harmless for the budget.
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
(lmao, w, f, no way, hi, clip it); `[patterns]` anchored regexes at weight 0.5 for 7TV variants
(`^kek`, `^lul` → funny; only on emote-like tokens, build item 2); bots (since lexicon v2 in
`clipper_core/bots.toml`, shared with the judge; a lexicon file may add its own `[bots]`);
`[transcript]` phrases; `[channels.<login>]` overrides. Unknown emotes count as
`uncategorised`; `clipper-detector lexicon --unknown` lists the most used (a table only once
non-developers edit it). **Moment type** = dominant category of per-message votes (one per message
and category, one per user; `emote_counts` counts tokens, so it's used only where raw chat isn't
read): top share ≥ 0.4, else `mixed`; < 5 votes `unknown`. Only `MomentType` values are stored
(CHECK): a winning category that isn't one (greeting, uncategorised) maps to `mixed` (enough
votes) or `unknown`.

**3. Stage 2: raw chat and fake spikes.** On suspect/close, read the channel's `chat_messages`
`[onset − 20 s, peak + 5 s]` and `chat_events` `[onset − 60 s, peak + 5 s]` (index `channel_id,
sent_at`; raw rows commit ~5 s before their second's stats). **Clean uniques** = distinct users
without bots, `!commands`, deleted messages, one user's repeats; `z_clean` = `z` with `A ×
clean_fraction`; emitting needs `z_clean ≥ 4` (warm-up 6). **Shares**: `reaction` (category emote/
word), `greeting`, `command`, `bot`, `vote` (message = one number/letter or believe/doubt), `dup`
(top normalised text among *non-reaction* messages: mass "KEKW" is real, a copypasta is not),
`deleted`. **Score** (first guess): `z_clean · (0.6 + 0.8·min(reaction, 0.5))` + boosts (§4). A
matching rule stores the candidate as `suppressed` + reason (no judge job), to audit the filter
(no rule, but `z_clean` below the emit bar: reason `weak`; build item 2 changed windows and
details, see there):

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
- **"Clip it"**: every 5 s, scan new raw messages of held channels for a request to clip: "clip
  it/that/this" (also "clipit", "clip_it", stretched "CLIIIP IT"), "clipped", a message that is
  only "clip" ("CLIP", "clip?") or starts with `!clip`; not after a negation ("don't clip that",
  "not clipped"); links (with a scheme, `www.` or a bare domain with a path) removed first; a bare
  "clip" in a sentence ("nice clip", "watch the clip", "Clip of the week") is no request (build
  item 3 review). ≥ max(3, 3 % of clean uniques) users (no bots, no deleted messages) within 30 s
  → trigger (chat peak = first mention − 5 s). Clip links in chat (`clips.twitch.tv/<slug>`,
  `[m.|www.]twitch.tv/[<login>/]clip/<slug>`; not `/embed`, `/create`) count as viewer clips of
  their poster; bots' links (replies to `!clip`) and deleted messages don't, so two people must
  post.
- **Viewer clips**: the **detector** polls Helix Get Clips per held live channel every 60 s
  (`started_at = now − 10 min`; 1 point per call of the client id's 800/min, shared with the
  controller's ~2/min). Only it needs fresh clips: kept in memory + `candidates.features`, so
  `viewer_clips` stays backfill-only (§17). Live clips lack `vod_offset` for minutes → moment ≈
  `[created_at − duration − 5 s, created_at − 5 s]`. ≥ 2 creators overlapping within 90 s (3 above
  20 clips/h) → trigger. Clips cut from the VOD later (a `vod_offset` more than 120 s from where a
  live clip would sit, `created_at − started_at − duration`) or from another VOD (a `video_id`
  other than the one consistent live clips carry) don't count.
- **Boosts** +2 (clip mentions), +1 (viewer clip). A signal within ±60 s of a candidate is attached
  to it; a *suppressed* one is then re-emitted (a viewer thought it was real): an in-place UPDATE
  to `emitted` that keeps `suppress_reason` (audit), with its media request and judge job in the
  same transaction. A signal nobody claims becomes a candidate of its own; at most 4 such per
  stream-hour (`signal_budget_per_hour`; more: suppressed `budget`, and further signals don't lift
  that).

**5. Merge, delay, media request.**
- **Emit at episode close** (≤ ~20 s after the peak): stable peak/window; the judge waits for
  T + 30 s anyway. **Peak** (chat time) = argmax of the 3 s centred mean of `unique_chatters`;
  **onset** = first second in `[open − 10 s, peak]` where that mean reaches a quarter of the rise.
- **Delay** `d` = `channels.chat_delay_s` (manual override) → median of the last 5 valid
  `chat_delay_estimates` of the channel, ordered by their stream's `streams.started_at` (join; not
  `computed_at`: a backfill computes old streams late), skipping backfilled streams flagged
  `timeline_uncertain_s` → **8 s**; fixed per stream. `T = peak_utc = chat_peak − d`. **Window**
  (§7) `[min(T − 90 s, onset − d − 30 s), max(T + 30 s, close − d + 15 s)]`, at most 5 min.
- **Cooldown 60 s**: a weaker episode opening < 60 s after a candidate closed folds into it
  (`features.followups`); a stronger one is emitted with `features.prev_candidate_id`. **Budget**:
  ≤ 8 emitted per stream per rolling hour, then only above the lowest of them (caps LLM cost); it
  counts chat candidates only (a signal-only candidate's score is just its boost).
- **One transaction per candidate**: insert → `media.request_media(stream, window, Priority.TRIGGER,
  requested_by="detector")` → `jobs.enqueue(JUDGE, ref_id=id, dedupe_key=dedupe_key(JUDGE, id),
  priority=TRIGGER)` → commit. A 60 s sweep marks its requests fulfilled once `coverage(window)`
  is complete or 15 min after `window_to`. Until the judge exists: `CLIPPER_DETECTOR_JUDGE_JOBS=0`.
- **Idempotent** (restart replay, offline re-runs): the insert is `ON CONFLICT DO NOTHING` (UNIQUE
  `stream_id, peak_utc, source`); the media request and judge job are created only when a row was
  inserted. Before that the detector dedupes in code against every candidate of the stream within
  ± 60 s (any source and version), since a new delay shifts `peak_utc` and the UNIQUE alone
  wouldn't catch it.

**6. Offline pass (v1) and chat delay.**
- **Job** `detect_offline` (ref = stream), enqueued by a 5 min sweep for ended streams with complete
  `coverage([started_at, ended_at])` (backfill: also `vods.chat_metrics_to ≥ ended_at` or chat not
  available) and no job for this version (dedupe `detect_offline:{stream}:{version}`). A job whose
  `DetectOfflinePayload.detector_version` isn't the running version completes as a no-op (the
  sweep enqueues one for the current version). `JobRunner` doesn't renew `locked_at`, so a pass
  longer than `STALE_AFTER` (30 min) is requeued and runs twice: harmless (inserts are idempotent,
  estimates upsert), but measure pass times in build item 5.
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
  ≥ 1.5 above the median over lags. One `chat_delay_estimates` row per stream, upserted (`ON
  CONFLICT (stream_id, method) DO UPDATE …, computed_at = now()`); invalid rows may lack `delay_s`/
  `strength` (no events). Its own offline candidates use it, later streams the median of the last
  5 valid (by `streams.started_at`, §5). **Writer conflict**: the detector never
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
ts < now() − 10 s` on the PK → scorers in ts order); explicit 5 s; streams/leases 10 s; sweeps 60 s /
5 min; offline `jobs.JobRunner` (CPU work in a thread); heartbeat 15 s (built: each live tick
also re-reads the 30 s before the cursor to count late rows, ~31× the rows, ≈ 3k rows/s at 100
channels; `late_check_s` = 0 turns it off). **Polling, not LISTEN**:
chat-ingest sends no NOTIFY, is rarely redeployed, and its 5 s grace dominates latency. **Leases**
`detect:channel:{id}` (TTL 60 s, renewed every 20 s; a stable `CLIPPER_WORKER_ID` retakes them).
**Heartbeat**: versions, channels held, max lag, emitted/suppressed by reason (1 h), offline jobs.
`CLIPPER_TWITCH_*` enables Get Clips. Controller: `STALE_AFTER` += judge 10 min, offline 30 min.

**10. Build plan.**
1. ✅ **Contracts + migration** (built 2026-11-20). *Done:* `test_migrations` passes; payload/enum
   tests. Built as the contract block below: migration `0004_detector`, contracts, controller
   `STALE_AFTER`. Review changes (2026-11-20, folded into the block below): UNIQUE (`stream_id`,
   `peak_utc`, `source`) replaces the separate (`stream_id`, `peak_utc`) index; CHECKs
   `suppress_reason` (one-way), `moment_type` and `nesting` on candidates; nullable `delay_s`/
   `strength` with CHECKs `valid_has_delay` and `events`; no index on `media_request_id` (add it in
   the first migration that deletes `media_requests` rows). Columns not marked "null" are NOT NULL,
   without defaults unless given; `delay_source` and `suppress_reason` are plain text. §19's
   per-round keys (`judge:{cand}:r{n}`) use the same `dedupe_key`. Needs 0004 on the live DB
   before the detector runs.
2. ✅ **Scorer, stage 2, lexicon** (built 2026-11-20, review fixes the same day; pure, synthetic
   fixtures). *Done:* noise → nothing; 15 s ×4 burst → one candidate, peak ± 2 s; start ramp →
   nothing; raid, mass gift, copypasta, `!enter`, "1/2" votes, greeting flood → suppressed with
   the right reason; replay from a later start → same. Built as `services/detector`
   (`clipper_detector`, no Dockerfile/compose yet): `params.DetectorParams` (every tunable above,
   flat TOML overrides, unknown keys rejected), `lexicon` + `data/lexicon.toml` v1 (v2 since
   2026-11-21: the bots moved to `clipper_core/bots.toml`, see §19 6b; first
   hand-made list, to be tuned), `scorer.ChannelScorer`, `stage2.evaluate`,
   `merge.plan_candidate`, `pipeline.run_stream` (the three over in-memory rows: the reference for
   the live service and the backtest), `synthetic` (seeded chat through the real
   `SecondAggregator`). `detector_version` = `d<SCORER_VERSION>-p<8 hex of the scoring
   params>-l<lexicon version>.<6 hex of the lexicon file>` (no `:`), e.g. `d1-p3f9a0c1d-l1.dea5e8`:
   the file hash shows a lexicon edited without a bump; live-loop fields (`final_lag_s`,
   `restart_replay_s`, `restart_emit_s`, `dedupe_s`) are left out of the params hash. The root
   `.gitignore` ignores `data/`: the lexicon's directory is re-included there and is a hatch
   `artifact` (else the wheel/image would lack it). 156 tests, ~9 s; the scorer runs a 2 h stream
   in ~0.1 s; the exact-series scorer tests fail when `close_after_s`, `z_stay`, `floor_activity`
   (upwards), `ratio_min_baseline` or `baseline_exclude_s` change (checked by mutating the
   defaults). Decisions and deviations from the text above (all provisional, like the defaults):
   - **Own baseline required**: no scoring until the own baseline holds `min_baseline_values` =
     300 values (≈ 6 min after the stream start or a > 20 min outage), prior or not; the prior
     only raises the bar. Measured: with 60 values a 5 min start ramp without viewers or prior
     (a first backfilled stream) opened an episode in 5 of 10 seeds, with 120 in 2, with 180+ in
     none; a prior far below the chat (a quieter previous stream) standing in for the missing own
     baseline made the first seconds of the stream a spike.
   - **In-stream only**: seconds before `streams.started_at` are neither scored nor fed to the
     baseline, and zero runs there are no missing chat (offline chat is another regime); "20 min
     of history" for the prior counts from `max(started_at, first row)`.
   - **Scaling**: the viewer ratio scales the own baseline and its MAD (S uses √B'); the prior has
     its own ratio (V_now / previous stream's median viewers, same clamp), so nothing is scaled
     twice. The suspect needs the floor too and re-arms only after z < 2. The floor of 8 never
     binds with these defaults: z ≥ 4 with S ≥ 2 already needs A ≥ B + 8; only the viewer part
     (0.005·V) does.
   - **Missing chat** detection marks the run retroactively and drops every A whose 10 s window
     touches it; the scorer reports `MissingChat(start, end)`.
   - **Stage-2 shares and votes count the burst `[onset, peak + 5 s]`** (`shares_before_onset_s`
     = 0), not the table's `[onset − 20 s, peak]`: a 15 s ×4 burst of one copypasta has a dup
     share of 0.28–0.42 there (median 0.34 over 10 seeds; 0.36–0.49 over `[onset − 20 s, peak +
     5 s]`), so the 0.4 threshold was a coin flip; on the burst it is 0.60–0.73. The 20 s before
     the onset still feed the events. Raw messages are read over `[min(onset − 20 s, strongest −
     9 s), max(peak + 5 s, strongest) + 1 s)`: the strongest-z second comes 4–8 s after the peak
     (plateaus), and `clean_fraction` uses its whole 10 s window.
   - **Clean uniques** in A's unit: per second, distinct users with a message that is no bot's,
     no `!command`, not deleted; a user's repeats across seconds stay (removing them made real
     mass-KEKW moments, many users typing KEKW every second, look weak).
   - **Rule order** (first = `suppress_reason`, all in `features.rules`): raid, subs, bits,
     mode_toggle, spam_wave, commands, vote, copypasta, bots, greeting. Vote before copypasta
     ("1"/"2" floods are polls). A command is `!` + a letter/digit ("!enter", not "!!!" or "!!!
     LETS GO"). Dup ignores messages without free text: only emotes (known or not: Kappa,
     pokiShock floods), lexicon words, greetings, emoticons (":D", "xD", a lone "D") or
     punctuation ("???"). Greeting counts only
     messages that aren't reactions ("YO WTF", "hi KEKW" are reactions); "yo" is in no category
     (greeting and shout alike). A vote is one number/letter or believe/doubt with only trailing
     punctuation dropped, not a lexicon word (W/L/F are reactions) and not an emoticon letter
     (D/O/P/X: ":D", "xD"). spam_wave = moderated share ≥ 0.3: deleted, or by a user timed
     out/banned in the window, or sent before a `clearchat` (timeouts don't mark rows deleted).
     Bits: bits events (live) or `Cheer100`-style text (events unknown); ≥ 1000 bits sets
     `features.bits_big` only.
   - **New suppress reason `weak`**: no rule matched but z_clean is below the emit bar (bots,
     commands and deleted messages mixed, each under its share). Stored for the audit like the
     others (no CHECK on the column).
   - **Lexicon matching**: patterns must be anchored; one anchored at one end (`^kek`, `love$`) is
     tried only on emote-like tokens (a recognised emote, or a token with an inner capital such as
     KEKWait, peepoLove), so plain words (glove, unhappy, microwave, telescope, lullaby, pogo) never
     match; one anchored at both ends is a word shape (`^(?:ha){2,}h?$`) tried on every token. An
     emote is found with punctuation around it ("monkaS,"). Moment-type ties go to the first
     category in lexicon order; pattern matches vote 0.5.
   - **Cooldown** only against *emitted* candidates (a real moment right after a raid must not
     fold into the raid's suppressed row); a fold-in is `merge.FoldIn` (the live service appends
     it to the earlier candidate's `features.followups`) and moves that candidate's chat end to
     the folded episode's end (`FoldIn.extended`), so the cooldown runs from the last close: a
     burst every ~45 s stays one candidate instead of every other burst being emitted. **Window
     clamp**: the side reaching further beyond `[T − 90 s, T + 30 s]` is trimmed first, then both
     equally, the moment is clamped into the window. Budget counts emitted candidates by chat peak
     in `(peak − 1 h, peak]`; suppressed ones don't count.
   - `SuppressReason` and `DelaySource` are in `clipper_core.contracts` (shared value sets of
     `candidates` that the judge and review read, §19/§21); the columns stay plain text.
   - Open (for build item 4): with these defaults pure Poisson noise at 0.3–1 unique chatters/s
     (A ≈ 3–10) opens 0.5–0.9 episodes/h (0.1/h at 2/s); stage 2 emits them. Real chat is
     burstier than Poisson; check on backfilled quiet channels before changing the floor.
3. ✅ **Live service** (built 2026-11-20; defaults provisional, not deployed). *Done:* integration
   tests (Postgres, injected clock, ~2 s each): stats + raw chat → candidate + media request +
   bumped transcribe jobs + judge job with one `xmin` (one transaction); raid → suppressed, no
   media request or job; restart (same worker id) and lease handover → no duplicate; a restart
   emits a missed burst once and nothing outside its emit window; decision 0 s after the closing
   second is final (≤ 10 s required); backfilled stream (events/viewers unknown); "clip it" →
   own candidate; mentions after a suppressed raid → in-place re-emit (UPDATE + media request +
   job, one transaction); viewer clips (fake Helix) attach (+1); learned delay/override and prior;
   the real loops (`run`) with a clock offset into the data. Built as `clipper_detector.service`
   (`LiveDetector`), `reads`, `writes`, `explicit`, `settings`, `main` (`clipper-detector`);
   `services/detector/Dockerfile` (slim); compose service `detector` (profile `detector`,
   `CLIPPER_DETECTOR_JUDGE_JOBS=0`, `CLIPPER_WORKER_ID=detector-1`). Settings
   `CLIPPER_DETECTOR_*`: `MODE` live|offline|all (offline/all log "not built yet" and run no
   jobs), `JUDGE_JOBS` (default 0: candidates and media requests are still written),
   `PARAMS_PATH`, `LEXICON_PATH`, `MAX_CHANNELS`, loop intervals, `LEASE_TTL_S` 60 /
   `LEASE_RENEW_S` 20 (validated: renew < TTL and sync interval + renew < TTL), `FULFIL_AFTER_S`
   900, `LATE_CHECK_S` 30, `WATERMARK_SLACK_S` 10, `SIGNAL_MAX_AGE_S` 900. Review (2026-11-20)
   added: fold-ins through the DB, a restart after a fold chain (the replay scores the follow-ups
   above their candidate: nothing new), a late stream row, chat clip links (two posters), a
   deactivated channel releasing its lease, late metric rows + the watermark, two writers at once,
   a retaken channel not widening the others' reads, signals surviving a failed decision;
   re-review: restarts mid fold chain and after a follow-up = the uninterrupted run, plus the
   restart property test. 191 detector tests, ~1.6 min. Decisions and deviations (all provisional):
   - **Final seconds** = `ts < ceil(now − 10 s)` from the service's clock (injectable), not the
     DB's `now()` (`final_lag_s` 8 → 10: the margin was ~1 s), and no further than the newest
     `chat_metrics_1s` row of the held channels + 1 s (chat-ingest writes a second of every
     channel in one flush) unless that is more than 10 s behind (all quiet or chat-ingest down:
     the clock decides, zeros as before). Each tick also re-reads the 30 s before the cursor: a row
     for a second already fed with other values is counted (`late_rows`, heartbeat) and logged.
     The re-read is the price of `late_check_s`: ~31 rows per channel per tick instead of 1
     (≈ 3k rows/s at 100 channels, on the PK); of `event_counts` only the roomstate count is
     selected (decoding every row's JSON cost more than the scorer). One query per tick with per-channel ranges
     (`JOIN unnest(:ids, :sinces)`: §18.9's
     `ts > :last` per channel), the same for raw chat and viewer snapshots, so a channel retaken
     with a 15 min rescan doesn't widen the others' reads.
   - **Streams by time**: each second goes to the stream whose `[started_at, ended_at)` holds it
     (any source); seconds outside a stream are skipped; leaving a stream flushes its scorer
     (an open episode is decided as truncated); a stream row that turns up after its start (the
     controller polls every 30 s) rebuilds the channel (replay). Viewers: the latest snapshot at
     or before the second, fed per second (the scorer takes the median itself).
   - **Prior** only when the scorer sees the stream from its start: the scorer counts "20 min of
     history" from its first second, so a prior during a restart replay of an old stream would
     raise the bar where the backtest doesn't.
   - **Suspects** don't prefetch raw chat (not needed: stage 2 at close is one indexed read).
   - **Failure = rebuild**: a channel whose scoring or decision failed is replayed on the next
     sync; the emit window and the dedupe make that idempotent. One asyncio lock serialises the
     loops (no signal resolution between an episode's close and its decision).
   - **Writes**: one transaction per episode under a transaction-level advisory lock per stream
     (a handover can leave two processes on a stream for up to one lease renewal): the stream's
     candidates re-read, the replay dedupe, then the fold-in, the candidate (+ its ± 60 s video
     peak dedupe, the insert, media request, judge job) or the signals onto a duplicate. **Replay
     dedupe** (review blocker: a restart re-emitted folded follow-ups, whose replay scored above
     their candidate with a shorter baseline): an episode is a candidate's duplicate when its
     chat peak lies in the candidate's chat span (onset to end, extended by its follow-ups), or,
     for a replayed episode (peak before the replay's end), an *unclaimed* peak (a candidate's
     or a follow-up's) that an earlier process decided lies in the episode's own burst
     `[onset − 10 s, end + 10 s]`. Earlier peaks are matched one to one (a dedupe claims the
     nearest one in the burst) and dropped as the replay passes them (before the current
     episode's burst), so an episode the earlier process never decided (the next burst of a fold
     chain, a stronger burst right after a follow-up, also after a follow-up the replay doesn't
     reproduce) goes through `plan_candidate` as without the restart (re-reviews: a plain ± 60 s
     test, then ± 60 s against unclaimed peaks, swallowed those). Tests compare restarts with the
     uninterrupted run: at several points of a chain, after a follow-up, and a seeded property
     test (`test_detector_restart_property`: 30 random fold chains with weak/strong follow-ups,
     raids, busier chat before the first burst; immediate restarts, an outage, a handover; time
     windows scaled down, ~18 s): every uninterrupted candidate exists after the restart with
     its status or is folded into the candidate it followed, no moment exists twice, other
     differences only where the replay's baseline differed. Measured: 67 candidates per run;
     immediate restarts and the handover identical; the outage run 66 identical, 1 missed and 2
     new, all in the replay's short-baseline region (the ± 60 s versions fail it). A live episode peaks after
     every earlier episode's end, so the span test never hits a new moment. Fold-ins append to
     `features.followups` idempotently (by chat peak). Accepted risk (§18.1's in-memory state):
     a replay's shorter baseline can open episodes the first run never opened (seen with a
     raised early baseline: a restart 1000 s after a chain found 3 bursts the first run hadn't);
     those are new detections, not duplicates.
   - **Explicit signals** (§18.4, needed here; tunables added to `DetectorParams`, hashed:
     `mention_*`, `clip_*`, `signal_attach_s`): a signal attaches to a candidate or episode
     within ± 60 s (chat time), boosting once per kind; an episode decided with a signal that
     stage 2 or the budget suppressed is **emitted with its reason kept** (the decision-time form
     of the re-emit); a signal nothing claims becomes a candidate of its own (`trigger` =
     `clip_mentions`/`viewer_clips`, score = the boost, moment type `unknown`, no cooldown, own
     cap of 4 per stream-hour) once the scorer is 60 s past it with no episode open; the chat
     budget counts chat candidates only (`PriorCandidate.trigger`). One mention signal per 60 s
     per channel; the mention rule and clip links as in §18.4 (review); a chat link is a clip made
     when posted, 30 s long, by its poster; a viewer-clip signal sits at the middle of the clips'
     common span. Helix Get Clips: per held channel in a live stream with a `platform_user_id`,
     every 60 s, clips created in the last 10 min, without clips cut from the VOD later or from
     another VOD (§18.4). Raw chat is scanned from the emit window (now − 15 min) on a channel's
     first build. Signals taken by a decision that fails go back to pending (the replay decides
     again with them); between streams they wait up to 15 min.
   - **Sweep**: every detector process sweeps all detector requests (idempotent; no leader).
   - **Heartbeat** `info`: versions (detector, scorer, params, lexicon), channels held with stream
     and lag (incl. the 10 s final lag), `max_lag_s`, `late_rows`, `last_hour` (emitted,
     suppressed by reason, re-emitted, folded, duplicate, late rows, signals by kind),
     `viewer_clips`, `offline: "not built"`. With `JUDGE_JOBS=0` emitted rows have no judge job.
   - **Scaling**: leases are per worker id; `--scale detector=2` with the compose service's fixed
     `CLIPPER_WORKER_ID` would make both share one owner: one service (and id) per process.
   - Open: the draft dedupe (± 60 s on the video peak, §18.5) also drops a *stronger* live
     episode within 60 s of an emitted candidate that `plan_candidate` would emit with
     `prev_candidate_id`; explicit-signal timing and thresholds are guesses (tune with build
     items 4/5); the
     raw-chat scan reads every message of held channels every 5 s (fine at 10 channels; at
     hundreds, chat-ingest's proposed `clip_mentions` column); the Helix poll is untested against
     the real API (no credentials in tests).
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
| 2026-11-20 | No scoring until the own baseline holds 300 values (≈ 6 min); the prior only raises the bar, it never stands in for the own baseline | a median of the first seconds of a start ramp, or a prior from a quieter stream alone, made ordinary chat a spike (5 of 10 synthetic ramps at 60 values, 0 at 180+) |
| 2026-11-20 | Stage-2 shares and moment-type votes count the burst `[onset, peak + 5 s]`, not `[onset − 20 s, peak]`; raw chat is read through the strongest second's activity window | with 20 s of ordinary chat a 15 s ×4 copypasta burst had a dup share of 0.28–0.42 (threshold 0.4), on the burst 0.60–0.73; `clean_fraction` needs the whole 10 s window of the strongest second |
| 2026-11-20 | New suppress reason `weak`: no fake-spike rule matched, but `z_clean` is below the emit bar | such spikes (bots, commands, deleted messages mixed) are stored for the audit like the others instead of vanishing |
| 2026-11-20 | Rule order raid, subs, bits, mode_toggle, spam_wave, commands, vote, copypasta, bots, greeting (first = `suppress_reason`, all in `features.rules`) | event causes are the clearest explanation; "1"/"2" floods are polls, not copypasta |
| 2026-11-20 | `SuppressReason` and `DelaySource` in `clipper_core.contracts` (columns stay plain text) | shared value sets of `candidates` that the judge, review and backtest read (§19, §21) |
| 2026-11-20 | `detector_version` = scorer + scoring-params hash + lexicon version and file hash; live-loop fields not hashed | an edited but unbumped lexicon shows; tuning the live loop doesn't start a new offline pass |
| 2026-11-20 | Live service: the final cutoff comes from the service's clock; seconds are assigned to streams by time; a failed channel is replayed (rebuilt), never patched | one code path for live, restart and tests (injected clock); replay + the ± 60 s dedupe are idempotent |
| 2026-11-20 | An explicit signal attached at decision time emits a candidate that stage 2 or the budget suppressed (reason kept); an unclaimed signal becomes its own candidate 60 s later | the re-emit rule of §18.4, whichever comes first; a "clip it" wave without a chat spike is still a moment |
| 2026-11-20 | Candidate writes of a stream are serialised by a transaction-level advisory lock | a lease handover can leave two processes deciding the same stream for up to one sync |
| 2026-11-20 | Replay dedupe: an episode inside a candidate's chat span (extended by its follow-ups), or a replayed one whose burst `[onset − 10 s, end + 10 s]` holds an unclaimed peak or follow-up peak an earlier process decided (matched one to one, passed peaks dropped), is that candidate's | a restart's shorter baseline scored folded follow-ups above their candidate and re-emitted them (review probe: 2 extra candidates); a plain ± 60 s test swallowed bursts the earlier process never decided (re-review: chains split, a stronger burst lost) |
| 2026-11-20 | Final lag 10 s plus a data watermark (newest row + 1 s, within 10 s), late rows counted | 8 s left ~1 s of margin over chat-ingest's grace and flush; a lagging flush must not turn chat into zeros |
| 2026-11-20 | The chat budget counts chat candidates only; signal-only candidates have their own cap (4 per stream-hour) | a signal-only candidate's score is just its boost: as the budget's "lowest" it let every chat candidate through |

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
  copypasta, vote, bots, greeting, mode_toggle, spam_wave, weak, budget: `contracts.SuppressReason`,
  added with `weak` in build item 2; CHECK `suppress_reason`:
  `status <> 'suppressed' OR suppress_reason IS NOT NULL`, one-way so a re-emitted row keeps it);
  `trigger` text (chat, clip_mentions, viewer_clips, audio, transcript; no CHECK, it grows);
  `peak_utc` timestamptz (video time); `chat_peak_utc` timestamptz null; `chat_delay_s` float;
  `delay_source` text (override, learned, default: `contracts.DelaySource`); `moment_from`, `moment_to` timestamptz (CHECK
  `moment_to >= moment_from`); `window_from`, `window_to` timestamptz (CHECK `window_to >
  window_from`; CHECK `nesting`: `window_from <= moment_from AND moment_to <= window_to AND
  peak_utc BETWEEN window_from AND window_to`); `score` float; `moment_type` text default
  'unknown' (CHECK IN the `MomentType` values); `features` jsonb default '{}' (z, ρ, baseline,
  uniques, viewers, shares, categories, top emotes, events, explicit signals, audio, transcript
  hits, followups); `detector_version` text; `media_request_id` FK media_requests null (no index
  yet); `created_at`, `updated_at`. UNIQUE (`stream_id`, `peak_utc`, `source`) (also serves
  lookups by stream and peak); INDEX (`channel_id`, `created_at`).
- **`ChatDelayEstimate`** (`chat_delay_estimates`), writer: detector. `id` bigint PK; `channel_id`
  FK; `stream_id` FK; `method` text ('audio_events'); `delay_s` float null; `events` int (CHECK
  `>= 0`); `strength` float null (peak above the median over lags); `valid` bool (CHECK
  `valid_has_delay`: `NOT valid OR (delay_s IS NOT NULL AND strength IS NOT NULL)`);
  `detector_version` text; `computed_at` default now(). UNIQUE (`stream_id`, `method`); INDEX
  (`channel_id`, `computed_at`).
- `viewer_clips`: unchanged from §17 (writer: backfill tool only); the detector only reads it.
- Docstrings only: `Channel.chat_delay_s` = manual override by the operator (learned values in
  `chat_delay_estimates`); `MediaRequest`: detector requests are linked from
  `candidates.media_request_id`, `clip_id` stays for the judge.

Outside core: controller `maintenance.STALE_AFTER` += `JUDGE: 10 min`, `DETECT_OFFLINE: 30 min`;
the detector uses `clipper_twitch.helix.get_clips` (added by §17) with `TwitchSettings`.
No new notify channel: the jobs NOTIFY (`clipper_jobs`) wakes the judge.

## 19. Judge (phase 2) — proposed 2026-11-19, not yet agreed

**Status:** proposal (not yet agreed); build item 6a (contracts + migration 0005 +
`clipper_core.lifecycle`) built 2026-11-20, not on the live DB yet; build item 6b (input builder
+ resolver, pure) built 2026-11-21; the service (6c) not built. The service can be built now with a deterministic fake model; provider and model are the **user's decision** (cost),
made with the eval harness (6d). Prompt, thresholds and clip lengths get tuned on real candidates.

**Ownership (§19–§22; replaces §8's "later" rows once agreed).** One writer per table; `jobs` is
shared (anyone enqueues). §7's state is never stored: one helper, `clipper_core.lifecycle`, derives it.

| Service | Writes (migration) | Enqueues | Runs |
|---|---|---|---|
| detector §18 | `candidates`, `chat_delay_estimates`, own `media_requests` (0004) | `judge`, `detect_offline` | `detect_offline` |
| judge §19 | `clips`, `clip_plans`, `judge_calls`, own `media_requests` (0005); `clips/{id}/judge/` | `judge` (rounds), `render`, `notify` | `judge`, `revise_plan` |
| renderer §20 | `renders` (0006); `clips/{id}/pinned/`, `clips/{id}/renders/` | `review`, `notify` | `render` |
| review §21 | `clip_reviews`, `review_events` (0007) | `revise_plan`, `render` (re-render), `publish`, `publication_action` | `review`, `notify` |
| publisher §22 | `platform_accounts`, `publish_routes`, `publications`, `publication_stats` (0008) | `sync_publication`, `notify` | `publish`, `sync_publication`, `publication_action` |

**Role.** `services/judge` (slim): N `jobs.JobRunner`s on `judge` (ref = candidate) and `revise_plan`
(ref = review event, §21) + a waiter loop. `clips.status`: `waiting_for_media → judging → rejected |
planned` + `failed`; the rest of §7 comes from §20–§22's tables, and §7 gains the side states
`failed` and `unrenderable`. `candidate` = an emitted candidate with no clip row yet.

**A judge job = "advance this clip"** (idempotent; never blocks a worker while media is missing):
1. Upsert the clip by `candidate_id`: context = the candidate's window, `anchor_utc` = T, round 0.
   No video over `[moment_from − 30 s, moment_to + 30 s]` (audio_only) and `judge_audio_only` off →
   `rejected`/`no_video` without an LLM call (§20 can't render it).
2. `coverage(context)` complete → step 4 now; else the clip stays `waiting_for_media`, job done.
3. **Waiter** (each judge process; clips claimed `FOR UPDATE SKIP LOCKED`): LISTEN
   `clipper_transcripts` + a 30 s sweep. Ready → enqueue `judge` with `dedupe_key(JUDGE, cand,
   f"r{round}")` at the clip's priority (the detector's first job is `judge:{cand}`, §18). Ready =
   coverage complete; or past the **soft deadline** (`max(waiting_since, context_to) + 4 min`, wall
   clock) with nothing pending in the core `[moment_from − 5 s, moment_to + 5 s]` (flag
   `partial_context`); or past the **hard deadline** (+30 min) with any of the core transcribed
   (else `rejected`/`no_media`). Stream end: `coverage()` makes time after the last chunk a gap.
4. Compare-and-set `waiting_for_media → judging` (same round), commit; input, cost guard, cache, call,
   resolve + repair; then **one transaction**: `clip_plans` row, clip status, `mark_fulfilled` of the
   clip's own media requests (the judge's rows, `clip_id` = the clip; never the detector's, which
   its own sweep fulfils) and, for `clip`, the `render` job (`dedupe_key(RENDER, plan_id)`; off with
   `CLIPPER_JUDGE_RENDER_JOBS=0`). Provider errors retry; the job's last attempt marks it `failed`.

**Context extension.** Answer `need_context` (`before_s`, `after_s`, why) → context = union, clamped
to `[T − 10 min, T + 2 min]` and the stream; round + 1; `request_media(..., Priority.JUDGE_CONTEXT,
requested_by="judge", clip_id)`; back to `waiting_for_media`: one transaction. At most 2
extensions (rounds 0 and 1 may ask), so at most 3 calls per clip (plus one retry each); the last
round's schema has no `need_context`. Each round is a `clip_plans` row (analytics see the asks).

**Revisions** (`revise_plan` from review, §21, at `JUDGE_CONTEXT`: a human waits). Refused if
`base_plan_id` isn't the clip's latest plan or the video is gone (chunk video and the render's ±30 s
pins). A `PlanPatch` goes through the resolver (`author = operator`; a time with no word nearby stays,
repair `unsnapped`); an `instruction` is one model call on the plan + the same input (`author =
judge`; counted by the cost guard, never blocked). One transaction: `clip_plans` v+1 (`parent_id`,
`review_event_id`, `instruction`), `clips.plan_version`, the `render` job; or `notify(REVISION_REFUSED)`.

**LLM input** (stable order, no wall-clock values: cacheable, golden-file tested; seconds relative to
T; **no speaker labels**: streamer, guests, game, TTS, lyrics mixed):
- Meta: channel, stream title + category, moment type, detector score/trigger, explicit signals.
- Transcript, one line per sentence: `S14 [-12.4→-8.1] so I went in there and he just`;
  low-confidence words marked `?`; holes as `[-40→-25 no audio: ad]`.
- Chat on video time (shifted by `chat_delay_s`), per 5 s: messages, uniques, top 3 emotes, top 3
  normalised messages (the detector's filters, `clipper_core.chat`), clip mentions. Audio: notable
  seconds (a sound group ≥ 0.5, loudness ≥ +6 dB over the window median); music ≥ 0.6 = Content ID risk.
- Chat and transcript are **untrusted** (injection via chat or TTS): fenced as data; schema +
  validator bound the output; nothing publishes without review. **Text only in v1**; keyframes
  (~4 × 0.5k tokens) when the eval shows misses.

**Answer vs plan.** The model fills a judge-internal `JudgeAnswer` (verdict, `in_s`/`out_s`, hook,
title, hashtags, caption style, emphasis as `(sentence id, word)`, zooms, SFX tags, music mood, layout
hint, reasons, confidence); code resolves it into the **`EditPlan`** (libs/core, versioned, UTC) that
renderer, review and publisher read. **Resolve + repair** (pure; each change a code in `plan.repairs`):
- Times → UTC, clamped to context and video: a cut across a gap (ad) is split into segments or
  shrunk to the side holding T; holes ≤ `CHUNK_JITTER` bridged.
- Snap: in → sentence start within 1.5 s, else word start, − 0.15 s (not before the previous word's
  end); out → sentence end within 1.5 s, else word end, + 0.3 s. Never inside a word.
- Duration into `[min_s, max_s]` (default 10–60 s; contract 3–180 s): extend to sentence edges around
  T, or trim the side farther from T.
- Emphasis resolved to real words (≤ 1 per 3 words); zooms clipped and merged, ≤ 1.5×; SFX tags must
  exist in the asset manifest (§20), ≥ 2 s apart, ≤ 6; title ≤ 100 chars, blocklist → `sensitive`.
- Whatever `EditPlan` would reject is repaired, not failed: touching segments merged; hashtags
  stripped of `#`, tags with whitespace dropped, capped at 5; hook text trimmed to 40 chars (empty →
  mode `none`); a teaser clamped into its segment to ≤ 3 s, else mode `none`; points (emphasis, word
  fixes, SFX, thumbnail) at/after the out point or outside the segments dropped (the thumbnail
  falls back to the default); zooms split at cuts (pieces < 0.5 s dropped); a blank title → the
  first sentence of the core, cut to 100 chars.
- Invalid JSON or an unrepairable plan: one retry with the error stated, then `failed`.

**`JudgeModel` Protocol**: `name`; `async judge(JudgeRequest) -> JudgeResponse` (system prompt, user
content, JSON schema, max tokens → JSON text, usage in/cached/out, latency, request id). Adapters:
Anthropic (`output_config.format`), OpenAI (strict Structured Outputs), Ollama (`format` = schema),
each stripping keywords its provider rejects. Fakes: `FakeJudgeModel` (rules), `ScriptedJudgeModel`.

**Provider options** (user's pick; per clip at ~4k uncached + 3k cached input, 1–3k output incl.
reasoning; list prices 2026-11-19, verify; a context round adds ~50 %):

| option | $/1M in / out (cached in) | ~$ per clip | ~latency | notes |
|---|---|---|---|---|
| Claude Opus 5.5 | 4 / 20 (0.20) | 0.05–0.08 | 10–40 s | effort knob trades depth for time |
| Claude Sonnet 5.5 | 2 / 10 (0.10) | 0.02–0.04 | 8–25 s | |
| Claude Haiku 5.5 | 0.10 / 0.50 | < 0.002 | 2–8 s | screening, or everything |
| OpenAI flagship (gpt-6.2-astra) / gpt-5.4-mini / -nano | 8 / 40 (0.40); 0.75 / 4.50; 0.20 / 1.25 | 0.10–0.15 / 0.01–0.02 / ~0.003 | 3–40 s | gpt-5 family reportedly retired Dec 2026 |
| Local via Ollama (container exists on this PC) | 0 | 0 (power) | 8B ~30–60 s, 3–4B ~15–30 s | beside the transcriber (~2.8 GB) + NVENC only 3–4B Q4 fits; 8B stalls transcription; quality unmeasured |

~200 calls/day (60 stream-hours × ~3) ≈ $12/day Opus, $6 Sonnet, < $1 Haiku/nano (§18's caps: ≤ ~5×).
Batch APIs (−50 %, hours) suit only backfill; a cascade (small screens, large plans) is to be measured.

**Cache, idempotency, cost.**
- **`judge_calls`** row per call: `request_hash` = sha256(model, prompt version, schema version,
  rendered request), usage, `cost_usd` (price table in settings), latency, answer or error; full I/O
  in S3. Committed **alone, right after the call**: a crash or retry reuses the paid answer. Unique:
  clip per candidate, plan version per clip, render job per plan, waiter job per round.
- **Cost guard** on top of §18's candidate budget: estimated cost per call vs
  `budget_usd_per_stream_hour` (rolling 60 min) and `daily_usd`; over the stream budget only candidates
  above that hour's median score proceed, over the daily cap `rejected`/`budget` + heartbeat warning.
- **Prompt versions** `v{N}-{sha8}` (`clipper_judge/prompts/v{N}/`, schema from `JudgeAnswer`; in the
  hash, every plan, the heartbeat; stable text first). **Shadow runs** (`CLIPPER_JUDGE_SHADOW`): extra
  model/prompt pairs on the same input, `shadow = true`, never rendered, inside the cost guard.

**Analytics later.** Plans + calls join `review_events` and `publication_stats` by `clip_id`: approval
rate, edit distance, $ per approved clip by model/prompt; eval set = backfill + `viewer_clips` + labels.

**Build plan.**
1. ✅ **6a Contracts + 0005 + `clipper_core.lifecycle`** (built 2026-11-20; not on the live DB yet).
   *Done:* `test_migrations` passes; `EditPlan` rejects naive times, unordered segments, bad
   durations/versions; lifecycle table-tested. Built as the contract block below: migration
   `0005_judge` (+ `db/tests/test_migration_judge.py`: links, uniques, downgrade to 0004 drops the
   tables and the `media_requests.clip_id` FK), contracts, models, `storage.keys.judge_io`,
   `clipper_core.lifecycle`, controller `STALE_AFTER[REVISE_PLAN] = 10 min`; tests
   `libs/core/tests/test_judge_contract.py`, `test_lifecycle.py`. Deviations and details (folded
   into the block below where they change it):
   - **Two extra CHECKs**: `clips.anchor_in_context` (`anchor_utc BETWEEN context_from AND
     context_to`) and `clip_plans.context_request` (`(verdict = 'need_context') = (context_request
     IS NOT NULL)`). `clips.reject_reason` values have no CHECK (like `suppress_reason`). Columns
     not marked "null" are NOT NULL without defaults unless given (`waiting_since`, `priority`, the
     `judge_calls` usage/cost/latency columns: an error row writes 0).
   - `MediaRequest.clip_id` is `ForeignKey("clips.id", use_alter=True)`: clips → candidates →
     media_requests → clips is a cycle, so `create_all` adds the FK after the tables, as 0005 does.
   - **§21's code-only contract, the part judge and renderer need**, is in now: `JobKind.REVIEW`,
     `REVISE_PLAN`, `NOTIFY`, `NoticeKind` (`revision_refused`, `render_failed`), `PlanPatch`,
     `ReviewPayload`, `RevisePlanPayload`, `NotifyPayload`. Left for 21a: `ReviewStatus`,
     `ReviewAction`, `ReviewLabel`, `PostMetadata`, `REVIEW_TELEGRAM_LEASE`; `STALE_AFTER` for
     `render`/`review`/`notify` comes with 7a/21a.
   - **`EditPlan` validator details**: points (emphasis, word fixes, SFX, thumbnail) lie in a
     segment's half-open `[start, end)`; zooms and a teaser lie inside *one* segment; segments
     must not touch (the resolver merges them, 6b); one word fix per word; hashtags are bare words
     (no `#`, no whitespace); the title and a text hook's text are not blank; `TEASER_MAX = 3 s`
     (a teaser needs `start_utc`/`end_utc`; fields of other modes are not rejected, for forward
     compatibility); `duration` = the segments (a teaser plays on top); non-UTC offsets are
     converted, naive times and offset-less ISO strings rejected (`contracts.UtcDatetime`).
   - **`PlanPatch` is operator input, not a stored file**: a plain frozen model with
     `extra="forbid"`, unknown fields also rejected inside `hook`, `word_fixes`, `layout`, `music`
     (an older judge refuses a newer patch: revision refused + notice); title 1–100 chars, not
     blank; `music` and `no_music` not both.
   - `judge_calls` also has INDEX (`clip_id`). `storage.keys.judge_io(request_hash, clip_id=None)`
     → `clips/{clip_id}/judge/{request_hash}.json` (`judge/{request_hash}.json` outside a clip,
     evals): keyed on the request hash, so the I/O is uploaded before the `judge_calls` row exists.
   - `contracts.judge_round_key(candidate, round)` = `dedupe_key(JUDGE, candidate, f"r{round}")`.
   - **Lifecycle**: `lifecycle_state()` already takes all four facts (review status and
     publication statuses too), and `lifecycle_states()` already reads `renders`, `clip_reviews`
     and `publications` when they exist (catalog lookup with `to_regclass`, then one query): 7a,
     21a and 22a only need tests against their real tables, as long as they keep the columns read
     (`renders.id/clip_id/status/created_at`, `clip_reviews.clip_id/status`,
     `publications.clip_id/status`). Latest render = `created_at DESC, id DESC`. Statuses are
     matched as text (the enums come with 7a/21a/22a); a value added later matches nothing, so the
     earlier stages decide. An unknown `clips.status` (a newer judge) raises in the pure
     `lifecycle_state()`; `lifecycle_states()` logs a warning and leaves that clip out, as it does
     ids without a clip row (`candidate` only from the pure function). A bare string where a
     collection is expected (`publication_statuses`, `clip_ids`) is a `TypeError`.
2. ✅ **6b Input builder + resolver (pure)** (built 2026-11-21). *Done:* byte-stable input for a
   fixture candidate (golden file); ≥ 20 repair cases (snap, gap split, durations, no mid-word cut,
   unknown SFX dropped, touching segments merged, `#`/whitespace hashtags and > 5 tags, long/empty
   hook text, teaser out of its segment or > 3 s, points at the out point or in a cut, a zoom across
   a cut, blank title); every resolver output validates as `EditPlan`. Built as `services/judge`
   (`clipper-judge`, module `clipper_judge`, no Dockerfile/compose yet): `params.JudgeParams` (all
   tunables of builder and resolver in one frozen object, so the input states the limits the
   resolver enforces), `inputs.JudgeInputs` (one round in memory; rows as protocols, ORM rows fit;
   `media_ranges(chunks)`, `bridge_jitter`), `transcript` (sentences by the transcriber's rule,
   ids `S1…` over the context; snapping lookups), `answer` (`JudgeAnswer`, `FinalJudgeAnswer`,
   `parse_answer`, `AnswerError`, `ANSWER_SCHEMA_VERSION = 1`), `prompt` (`build_request` →
   `JudgeRequest`, `request_hash(model, request)`, `load_prompt`; `prompts/v1/system.md` +
   `user.md`), `resolver` (`resolve`, `resolve_patch`, `extend_context`; outcomes `Planned`,
   `Rejected`, `NeedsContext`, `Unrepairable`; codes `Repair`), `samples` (the fixture candidate,
   also for 6c's fakes). Tests: golden user content + full request JSON (rounds 0 and 2) in
   `services/judge/tests/fixtures/` (`CLIPPER_JUDGE_UPDATE_GOLDEN=1` rewrites them), 63 resolver
   cases, 13 context-request cases, a seeded property test (300 random contexts/videos/words/answers
   plus a random operator patch each; once run with 4,500 more cases, all valid), ~2.5 s in all.
   Decisions and deviations:
   - **Answer shape**: `{reasons, verdict, confidence, need_context, plan}` with the plan fields
     nested in `plan` (null for reject/need_context), reasons first (the model explains before it
     decides), seconds relative to T. Lenient where the resolver repairs (title/hook length, `#`
     tags, unknown SFX tags, any zoom scale, out-of-range confidence/focus), strict on types, enum
     values and finite numbers (NaN/inf, bad enums: `AnswerError`, one retry). Unknown keys are
     ignored; the schema still says `additionalProperties: false`. The schema drops pydantic's
     titles and class docstrings (editing a core enum's docstring must not change the prompt
     version). A clip without a plan takes the core moment (`default_plan`).
   - **Prompt version** `v1-<sha8>` over `system.md`, `user.md`, the code's line format
     (`INPUT_FORMAT = 1`) and both answer schemas; the request hash covers model, prompt version,
     schema version, system, user, schema and max tokens (canonical JSON). Line endings are
     normalised to LF when the prompt is loaded.
   - **Input document**: low-confidence words (< 0.5) marked `(?)` before trailing punctuation
     (`go(?).`; a bare `?` reads as a question); holes in the transcript `[a→b no audio: <gap
     reason>]`, `[a→b no transcript yet]` (pending), `[a→b no speech]` (≥ 5 s without words); a
     Media section (context, video ranges with jitter holes bridged, labelled holes); chat per 5 s
     bucket aligned on T: messages and distinct users without bots and deleted messages, top 3
     emotes (from the messages' emote lists), top 3 normalised messages (links stripped,
     `!commands` and emote-only messages left out), distinct users asking for a clip; audio runs of
     notable seconds (a group other than speech ≥ 0.5, or loudness ≥ median + 6 dB) with
     "Content ID risk" at music ≥ 0.6. Untrusted text sits in `<data>` fences, one line per item,
     control characters removed and angle brackets replaced by single angle quotation marks; chat
     and the title are also JSON-quoted (the plan's own texts are not escaped: "I <3 this"
     stays; both cleaners also drop zero-width and bidi characters).
   - **Resolver details** beyond the list above: a cut with no part in the context takes the core
     moment; an edge in a video hole moves to the nearest video (`clamp_video`), a cut wholly in a
     hole to the nearest video range and grows from there (`move_to_media`); holes longer than
     `split_gap_max_s` (10 s) are not jumped (T's side is kept, its new edge snapped inwards),
     shorter ones split the cut, pieces < 1 s are dropped. Extension prefers the side with less
     room around T, sentence edge by sentence edge, then seconds, never above the maximum; trimming
     cuts the farther side until both are even, then both (1 ms spare for µs rounding); less video
     than `min_s` is kept (`short_media`) down to CLIP_MIN, below that `rejected`/`no_video`.
     Teasers need ≥ 1 s (`teaser_min_s`); emphasis words resolve by normalised text to the first
     unused word of the named sentence (`S14`, `s14`, `14`), the model's order deciding which
     survive the cap; overlapping zooms merge (union, max scale, the first's target); a blank title
     falls back to the first sentence overlapping the core, then `<channel> clip`; the blocklist
     (empty by default) also covers the hook text; `copyright_music_risk` is set when music ≥ 0.6
     in a second inside the segments; `sensitive`/`copyright_music_risk` are recomputed on every
     resolve, other flags (`partial_context`) copied. Repair codes are recorded once each, in the
     order first applied.
   - **Context requests** (`extend_context`): `before_s`/`after_s` clamped to `[0, 600]`/`[0, 120]`
     and the stream (`started_at`, `ended_at`); the context only grows (union; one already past
     the caps is kept); nothing new to see, or `round >= max_rounds` (rounds are 0-based: 0 and 1
     may ask, 2 must decide), gives `Unrepairable(final=True)`: the retry states the error and
     uses the final round's schema (`build_request(..., final=True)`). A `need_context` without
     its object asks for the caps. `NeedsContext.context_request()` = the granted edges relative
     to T.
   - **`resolve_patch`** (operator revisions, §21) is in too: only the edges the patch moves are
     snapped (none nearby: `unsnapped_*`), lengths use the contract's 3–180 s, word fixes are kept
     only for existing words (start within 1 ms, one per word, inside the segments), the base's
     emphasis/zooms/SFX/thumbnail are re-checked against the new segments; `model = "operator"`,
     the base's prompt version.
   - **Review fixes (2026-11-21)**:
     - **Chat filters in core**: `clipper_core.chat` now holds `strip_invisible`,
       `normalised_text`, `is_command`, `strip_links`, `is_mention`, `user_key`, `BotFilter`,
       `NoBots` and the bot list (`load_bots().for_channel(login)` from the packaged
       `clipper_core/bots.toml`; not under a `data/` dir); the patterns moved byte for byte, the
       detector re-exports its old names and behaves as before (its tests unchanged). The
       detector's `lexicon.toml` is v2 without bots; `Lexicon.load` adds the shared list and its
       file hash goes into the lexicon tag (`detector_version` changes once). The judge depends on
       clipper-core only.
     - **Never inside speech**: words merged where they overlap or touch are speech blocks; an
       edge inside one moves to the nearer block edge that fits (one step, no loop). Each edge is
       snapped inside its own video range; a final pass (`_settle`) checks every segment edge
       (also of a single segment and at holes), drops segments without a clean cut or (with
       several) shorter than `min_segment_s`, and snaps a new outer edge inwards; a cut the pass
       shortened below `min_s` is extended once more. The property test now generates words over
       holes, over video edges and overlapping: before the fixes it found 43 of 277 plans and 35
       of 277 patched plans cutting inside speech, after them none (3,000 extra cases too; the
       reviewer's probes: 89 and 68 mid-word cuts, 2 and 2 tiny segments, now 0).
     - **No cut**: a cut whose video can't hold CLIP_MIN (a small island) falls back to the core
       moment (`core_fallback`); across a long hole the side whose video ranges can hold CLIP_MIN
       wins over the side nearer T; still no cut → `rejected`/`no_video` only when the video over
       `[moment_from − 30 s, moment_to + 30 s]` is shorter than CLIP_MIN, else `Unrepairable`
       ("choose in_s/out_s inside a video range"; 1.5 % of the adversarial property cases).
     - **`resolve_patch`** returns `Planned` or `PatchRefused(error)`, never a reject: an operator
       edit can't change the clip's verdict (6c sends `notify(REVISION_REFUSED)`).
     - **Inputs**: `JudgeInputs.words_interval` (required) must cover the context ± 5 s
       (`words_interval_for(context)`), so the service reads the words cut by the context's edges.
     - **Hash and versions**: `request_hash(model, request, options)` also covers the provider
       options that change the answer (temperature, reasoning effort, ...). Tests pin the v1
       prompt files' sha256 and, per `INPUT_FORMAT`, the sample document's: a used prompt is
       frozen (edit by adding `prompts/v2/`), a format change needs an `INPUT_FORMAT` bump.
   - Open (for 6c): the title blocklist's words; `split_gap_max_s` and `teaser_min_s` are guesses;
     whether operator patches may jump a long hole (they can't now).
3. **6c Service with the fake.** *Done (integration):* partial transcripts → waiting; transcript +
   NOTIFY → `planned` + render job in one transaction; `need_context` → `request_media(100)` bumps
   queued transcribe jobs, round 2 must decide; crash between call and commit → no second call;
   soft/hard deadlines; budget → `rejected`/`budget`; a patch → v+1 + render job, a stale base refused.
4. **6d Real adapters + `clipper-judge eval`** (user's choice, keys, spend). *Done:* ≥ 50 labelled
   candidates through 2–3 models (agreement, repairs/clip, p50/p95 latency, $/clip); the user picks.

### Proposed decision-log rows

| Date | Decision | Why |
|---|---|---|
| 2026-11-19 | The judge writes `clips` (statuses up to `planned`/`rejected`/`failed`), `clip_plans` and `judge_calls`; later §7 states come from `renders`, `clip_reviews` and `publications`, combined by one helper, `clipper_core.lifecycle.lifecycle_state()` | one writer per table; §7 stays one readable state |
| 2026-11-19 | A judge job only advances a clip; waiting clips are re-checked on `clipper_transcripts` NOTIFY + a 30 s sweep, and a ready clip gets a new judge job per round (`judge:{cand}:r{n}`) | no worker blocks while media is missing; durable, prioritised, deduped wake-ups |
| 2026-11-19 | Deadlines: soft 4 min after the context end (judge with what exists if the core moment is covered), hard 30 min; stream end handled by `coverage()` | campaigns reward speed; a busy GPU must not hold clips forever |
| 2026-11-19 | Context extension: ≤ T − 10 min / T + 2 min, ≤ 2 extensions (≤ 3 calls), the last round's schema can't ask again; `request_media(JUDGE_CONTEXT)` in the round's transaction | bounded cost and latency; §6 priorities do the rest |
| 2026-11-19 | The model answers in a judge-internal `JudgeAnswer` (seconds relative to T, sentence ids); deterministic code resolves it into the versioned `EditPlan` (UTC, snapped to words, clamped to video) in libs/core | prompts change often, the renderer contract rarely; LLM times are never trusted raw |
| 2026-11-19 | LLM behind a `JudgeModel` Protocol (Anthropic, OpenAI, Ollama adapters, deterministic fake); provider and model chosen by the user from `clipper-judge eval` numbers | cost is the user's call; the service and tests don't depend on a provider |
| 2026-11-19 | Every call is logged in `judge_calls` with a request hash and committed right after the call; retries reuse it | crash-safe, never pay twice; cost and latency data for analytics |
| 2026-11-19 | Cost guard in USD per stream-hour and per day, on top of §18's candidate budget; operator revisions are counted but never blocked | token counts vary with context rounds; a candidate cap alone doesn't bound spend; a human is waiting |
| 2026-11-19 | Judge input is text only in v1 (transcript, chat per 5 s, audio events, meta); frames deferred | cheap, works with every provider; add frames when the eval shows misses |
| 2026-11-19 | Clips without video (audio_only) are rejected as `no_video` without an LLM call unless `judge_audio_only` | they can't be rendered (§20); spend only where output is possible |
| 2026-11-19 | The judge enqueues the `render` job in the plan's transaction (first plans and revisions) | jobs are enqueued with their data (rule); no extra wake-up channel |

### Contract changes

Hand-written migration **`0005_judge`** (after `0004_detector`): three new tables and one FK on an
existing nullable column (`media_requests.clip_id`, all NULL so far); nothing else changes. CHECK
texts are repeated in the models, as in 0002. Uses §18's `dedupe_key(kind, ref_id, *parts)`. The
revision handler uses §21's code-only contract (`REVISE_PLAN`, `NOTIFY`, `PlanPatch`, payloads).

**`contracts.py`**

```python
UtcDatetime = Annotated[datetime, AfterValidator(ensure_utc)]  # naive datetimes rejected

class JobKind(StrEnum):
    ...  # TRANSCRIBE, JUDGE, DETECT_OFFLINE (§18)
    RENDER = "render"  # ref_id = clip_plans.id; judge (plans), review (re-render, §21)

class ClipStatus(StrEnum):  # clips.status (writer: judge)
    WAITING_FOR_MEDIA = "waiting_for_media"
    JUDGING = "judging"
    REJECTED = "rejected"  # see ClipRejectReason
    PLANNED = "planned"
    FAILED = "failed"  # model/validation kept failing; clips.error

class ClipRejectReason(StrEnum):  # clips.reject_reason (review's labels: ReviewLabel, §21)
    MODEL = "model"  # the model said no
    NO_VIDEO = "no_video"  # nothing renderable; no LLM call
    NO_MEDIA = "no_media"  # hard deadline passed without a transcribed core
    BUDGET = "budget"  # cost guard

class Verdict(StrEnum):
    CLIP = "clip"
    REJECT = "reject"
    NEED_CONTEXT = "need_context"

class PlanAuthor(StrEnum):  # clip_plans.author
    JUDGE = "judge"  # the model (first plans, instructed revisions)
    OPERATOR = "operator"  # a review PlanPatch, resolved

class LifecycleState(StrEnum):  # §7, derived by clipper_core.lifecycle.lifecycle_state()
    CANDIDATE = "candidate"
    WAITING_FOR_MEDIA = "waiting_for_media"
    JUDGING = "judging"
    REJECTED = "rejected"
    PLANNED = "planned"
    RENDERING = "rendering"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    DISCARDED = "discarded"  # review rejected, expired or withdrawn
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"  # judge or renderer gave up
    UNRENDERABLE = "unrenderable"  # §20: no video / video expired

class LayoutKind(StrEnum):
    AUTO = "auto"  # renderer decides from the channel's layout config
    FACECAM_GAMEPLAY = "facecam_gameplay"
    FULL_CROP = "full_crop"
    BLURRED_FIT = "blurred_fit"

class CaptionStyle(StrEnum):  # presets defined in the renderer
    BOLD_POP = "bold_pop"
    CLEAN = "clean"
    KARAOKE = "karaoke"

class MusicMood(StrEnum):
    HYPE = "hype"
    FUNNY = "funny"
    CHILL = "chill"
    SUSPENSE = "suspense"
    SAD = "sad"
    EPIC = "epic"
    WHOLESOME = "wholesome"

class HookMode(StrEnum):
    NONE = "none"  # the in-point itself is the hook
    TEXT = "text"  # overlay text over the first seconds
    TEASER = "teaser"  # the hook range (1-3 s, inside the clip) plays first, then the clip

class RenderPayload(Payload):
    plan_id: int
    profile: str = "short_1080x1920"

# --- edit plan (clip_plans.plan; StoredModel: unknown fields ignored, `version` gates breaks)

class PlanSegment(StoredModel):
    start_utc: UtcDatetime
    end_utc: UtcDatetime  # > start_utc

class Hook(StoredModel):
    mode: HookMode = HookMode.NONE
    start_utc: UtcDatetime | None = None  # TEASER only
    end_utc: UtcDatetime | None = None
    text: str | None = Field(default=None, max_length=40)  # TEXT only

class EmphasisWord(StoredModel):
    start_utc: UtcDatetime  # start of the word as transcripts.words() returns it
    text: str
    kind: Literal["highlight", "big"] = "highlight"

class WordFix(StoredModel):  # caption text fix (review editor); the renderer shows `text`
    start_utc: UtcDatetime  # the word's start as transcripts.words() returns it
    text: str = Field(min_length=1, max_length=40)

class Zoom(StoredModel):
    start_utc: UtcDatetime
    end_utc: UtcDatetime
    scale: float = Field(ge=1.0, le=1.6)
    target: Literal["face", "gameplay", "center"] = "center"

class SfxCue(StoredModel):
    at_utc: UtcDatetime
    tag: str  # a tag of the asset manifest (clipper_core.assets, §20)
    gain_db: float = Field(default=0.0, ge=-30, le=6)

class MusicPlan(StoredModel):
    mood: MusicMood
    gain_db: float = Field(default=-20.0, ge=-40, le=-6)
    duck: bool = True  # duck under the stream audio

class LayoutHint(StoredModel):
    kind: LayoutKind = LayoutKind.AUTO
    focus_x: float | None = Field(default=None, ge=0, le=1)  # full_crop centre, 0 = left

EDIT_PLAN_VERSION = 1
CLIP_MIN = timedelta(seconds=3)
CLIP_MAX = timedelta(seconds=180)
TEASER_MAX = timedelta(seconds=3)

def judge_round_key(candidate_id: int, round_: int) -> str:  # "judge:{cand}:r{round}"
    return dedupe_key(JobKind.JUDGE, candidate_id, f"r{round_}")

class EditPlan(StoredModel):
    """What to render. Written by the judge (resolved, snapped to words, clamped to video); read
    by the renderer, review and publisher. All times are UTC video time; the renderer maps them to
    output time. Validators: segments sorted, disjoint, total length within CLIP_MIN..CLIP_MAX;
    emphasis, word fixes, zooms, sfx, thumbnail and a teaser hook (<= 3 s) lie inside the
    segments; zooms disjoint."""

    version: Literal[1] = EDIT_PLAN_VERSION
    clip_id: int
    stream_id: int
    anchor_utc: UtcDatetime  # T, the candidate's peak
    segments: list[PlanSegment] = Field(min_length=1)  # v1 judge writes one; jump cuts later
    hook: Hook = Field(default_factory=Hook)
    title: str = Field(min_length=1, max_length=100)  # title band + default post title (§22)
    hashtags: list[str] = Field(default_factory=list, max_length=5)  # without '#'; §22 merges
    caption_style: CaptionStyle = CaptionStyle.BOLD_POP
    emphasis: list[EmphasisWord] = Field(default_factory=list)
    word_fixes: list[WordFix] = Field(default_factory=list)
    zooms: list[Zoom] = Field(default_factory=list)
    sfx: list[SfxCue] = Field(default_factory=list)
    music: MusicPlan | None = None
    layout: LayoutHint = Field(default_factory=LayoutHint)
    thumbnail_utc: UtcDatetime | None = None
    moment_type: MomentType = MomentType.UNKNOWN
    confidence: float = Field(ge=0, le=1)
    reasons: list[str] = Field(default_factory=list)  # reasons[0] = the one-liner review shows
    flags: list[str] = Field(default_factory=list)  # partial_context, copyright_music_risk, sensitive
    repairs: list[str] = Field(default_factory=list)  # resolver changes: snap_in, split_gap, ...
    model: str
    prompt_version: str

    # properties: in_utc (segments[0].start_utc), out_utc (segments[-1].end_utc), duration
```

**`models.py`** (and the same in the migration):
- **`Clip`** (`clips`), writer: judge. `id` bigint PK; `candidate_id` FK candidates UNIQUE;
  `stream_id` FK streams; `channel_id` FK channels; `status` text default 'waiting_for_media' CHECK
  IN ('waiting_for_media','judging','rejected','planned','failed'); `reject_reason` text null
  (CHECK `(status = 'rejected') = (reject_reason IS NOT NULL)`); `round` int default 0 (CHECK ≥ 0);
  `anchor_utc` timestamptz; `context_from`, `context_to` timestamptz (CHECK `context_to >
  context_from`; CHECK `anchor_in_context`: `anchor_utc BETWEEN context_from AND context_to`); `priority` int (of its judge/render jobs); `waiting_since` timestamptz (wall clock,
  deadlines); `plan_version` int null (latest non-shadow `clip` plan, revisions included); `flags`
  jsonb default '{}'; `error` text null; `judged_at` null; `created_at`, `updated_at`. INDEX
  (`stream_id`, `anchor_utc`); partial INDEX `ix_clips_waiting` (`stream_id`) WHERE `status =
  'waiting_for_media'`. Score and moment type are read from the candidate.
- **`ClipPlan`** (`clip_plans`), writer: judge, rows immutable. `id` bigint PK; `clip_id` FK clips;
  `version` int; `round` int; `verdict` text CHECK IN ('clip','reject','need_context'); `plan` jsonb
  null (`EditPlan`; CHECK `(verdict = 'clip') = (plan IS NOT NULL)`); `context_request` jsonb null
  (`{"before_s", "after_s", "why"}`; CHECK `(verdict = 'need_context') = (context_request IS NOT
  NULL)`); `confidence` float null; `reasons`, `repairs` jsonb default
  '[]'; `model`, `prompt_version` text; `shadow` bool default false; `judge_call_id` FK judge_calls
  null; `author` text default 'judge' CHECK IN ('judge','operator'); `parent_id` FK clip_plans null
  (the plan a revision changed); `review_event_id` bigint null (FK added by 0007, §21);
  `instruction` text null; `created_at`. UNIQUE (`clip_id`, `version`).
- **`JudgeCall`** (`judge_calls`), writer: judge. `id` bigint PK; `clip_id` FK clips null;
  `stream_id` FK streams; `model`, `prompt_version`, `request_hash` text; `input_tokens`,
  `cached_input_tokens`, `output_tokens` int; `cost_usd`, `latency_s` float; `answer` jsonb null;
  `error` text null; `io_key` text null; `provider_request_id` text null; `shadow` bool default
  false; `created_at`. INDEX (`request_hash`); INDEX (`clip_id`); INDEX (`stream_id`,
  `created_at`); INDEX (`created_at`).
- `MediaRequest.clip_id`: `ForeignKey("clips.id", use_alter=True)` (migration: `create_foreign_key`,
  the "FK added when clips table exists" of 0001; `use_alter` breaks the FK cycle for `create_all`). Docstring: detector rows link from `candidates`, judge rows
  carry `clip_id`.

**New `clipper_core.lifecycle`** (the one §7 helper; §20–§22 read it, nobody stores the result):
`lifecycle_state(clip_status, render_status=None, review_status=None, publication_statuses=())
-> LifecycleState` (pure) and `async lifecycle_states(session, clip_ids) -> dict[int,
LifecycleState]` (two round trips: a catalog lookup of which later tables exist yet, then one
query; clips without a row or with a status this code doesn't know are left out, the latter with
a warning). First match wins: any publication `live` → `published`; any
`scheduled|uploading|processing|unknown` → `publishing`; review `approved` → `approved`,
`rejected|expired|withdrawn` → `discarded`, `pending|revising` → `in_review`; the clip's latest
render `rendering` → `rendering`, `done` → `in_review` (review job queued), `failed` → `failed`,
`unrenderable` → `unrenderable`; else `clips.status` (no clip row → `candidate`). Built in 6a with
all four arguments; `lifecycle_states()` reads `renders`, `clip_reviews` and `publications` as
soon as their migrations ran (7a, 21a, 22a only add tests against the real tables).

**`storage.keys`**: `judge_io(request_hash, clip_id=None)` → `clips/{clip_id}/judge/{request_hash}.json`
(`judge/{request_hash}.json` outside a clip; request + raw response, uploaded before the call's row;
kept for evals and prompt work).

**Outside core.** `services/judge` (`CLIPPER_JUDGE_*`: provider, model, api key `SecretStr`, prices
per 1M, `budget_usd_per_stream_hour`, `daily_usd`, `soft_wait_s` 240, `hard_wait_s` 1800,
`max_rounds` 2, `max_before_s` 600, `max_after_s` 120, `min_clip_s` 10, `max_clip_s` 60,
`judge_audio_only` false, `render_jobs` true, `shadow`, `concurrency` 4); compose service (assets
volume read-only, for the SFX tag list); controller `STALE_AFTER[JUDGE] = 10 min` (§18),
`STALE_AFTER[REVISE_PLAN] = 10 min`. (Built in 6b: the filters below are in `clipper_core.chat`.) The judge reads chat through §17's `clipper_core.chat` and the
detector's filter helpers (bots, commands, dup normalisation), moved there so both read chat alike.

---

## 20. Renderer (phase 2) — proposed 2026-11-19, not yet agreed

**Status:** proposal, nothing built. Time mapping, layouts, captions, the filter graph and the render
loop can be built and tested now with libx264 on synthetic chunks. NVENC inside Docker Desktop/WSL2
is **UNVERIFIED** (smoke test 7c; x264 keeps everything working without it). Per-channel layouts,
caption taste and the SFX/music library need the user.

**Role.** `services/renderer`, one process per GPU, one `render` job at a time (ref = `clip_plans.id`,
from the judge or review's re-render). Writer of `renders` (new) and `clips/{id}/pinned/`, `/renders/`;
enqueues `review`, `notify` (§21). Reads `clip_plans`, chunks, stream_gaps, `words()`, layouts,
assets. Image: slim Debian + a **pinned static ffmpeg with `--enable-nvenc --enable-libass`** (BtbN
or jellyfin-ffmpeg; URL + sha256) + bundled OFL fonts; compose reserves the GPU with
`NVIDIA_DRIVER_CAPABILITIES=compute,video,utility` (NVENC comes from the driver, no CUDA toolkit).

**Per job (plan P):**
1. Load P's `EditPlan`. A segment without video (`video_key` null: audio_only) → `renders` row
   `unrenderable`/`no_video`, job done (the judge normally doesn't enqueue these). Video object gone
   (48 h retention, never pinned) → `unrenderable`/`video_expired`.
2. **Pin**: S3 server-side copy of each chunk video overlapping `[in − 30 s, out + 30 s]` to
   `clips/{clip_id}/pinned/{seq}.ts` (existing ones skipped). The chunk owner's 48 h sweep stays
   clip-agnostic, and review can still move a cut by up to 30 s and re-render days later (§21).
3. **Time map** (pure `timemap.build(segments, chunks)`): per segment and overlapping chunk, trim
   `[a − chunk.start_utc, b − chunk.start_utc)` (exact inside a chunk, §5) with accurate seeking
   (`-ss` before `-i` while transcoding, then `trim`/`atrim` + `setpts`). Jitter holes ≤
   `CHUNK_JITTER` between contiguous chunks are filled (last frame held + silence) and overlaps cut
   from the later chunk, so inside a segment **output time = UTC − segment start + offset**. A gap
   inside a segment (the judge splits, so it shouldn't happen) → hard cut + warning. Segments join
   with hard cuts. The same map converts every plan time (captions, zooms, SFX, hook).
4. **Layout** to 1080×1920, then **captions** as an ASS file burned in with the `ass` filter
   (`fontsdir` = bundled fonts only, no system fallback), **zooms**, hook (teaser range first, or
   overlay text) and a title band (`plan.title`; post texts are separate, §22).
5. **Audio**: the stream audio from the chunk `.ts` (sample-aligned with its video), SFX + music,
   two-pass `loudnorm` to **−14 LUFS, −1 dBTP** (measure, then `linear=true` with the measured values).
6. **Encode**, one ffmpeg run, two outputs: `clip.mp4` 1080×1920 at source fps (≤ 60), H.264 High,
   `h264_nvenc` (p5, VBR ~8 Mbps / max 12, 2 s GOP) or `libx264` (`medium`, CRF 20) by
   `CLIPPER_RENDERER_ENCODER=auto|nvenc|x264` (`auto` falls back when NVENC init fails);
   `preview.mp4` 540×960, ~1.5 Mbps capped so the file stays ≤ 20 MB (Telegram bots upload ≤ 50 MB);
   AAC 48 kHz 192 kbps, `+faststart`. Then `thumb.jpg` (at `plan.thumbnail_utc`, else the hook
   start), `captions.ass`, `render.json` (`RenderManifest`).
7. Upload to `storage.keys.render_file(clip_id, render_id, name)`; **one transaction**: the `renders`
   row `done` + keys + `video_sha256` + measured duration/loudness + the `review` job
   (`dedupe_key(REVIEW, render_id)`, §21). One render row per job (`job_id` unique): a retry resumes
   it; the job's last failed attempt → `failed` + `notify(RENDER_FAILED)` in one transaction.

**Layouts** (normalised source rects, so 720p and 1080p sources share one config):
- `facecam_gameplay`: facecam rect → top panel 1080×760 (cropped to that aspect); gameplay → bottom
  1080×1160 (centre crop at its focus); split ratio configurable.
- `full_crop`: a 9:16 crop at `focus_x` → 1080×1920 (1.78× upscale from 1080p): Just Chatting, cam.
- `blurred_fit`: the whole frame at 1080×608 over a cover-scaled, blurred, darkened copy; title
  above, captions below. Used when nothing is configured.
- **Facecam: manual config first.** `layouts.toml` on a mounted volume (edits need no redeploy; its
  sha256 goes into each render): `[channels.<login>] facecam = {x, y, w, h}`, `default =
  "facecam_gameplay"`, `[channels.<login>.categories."Just Chatting"] layout = "full_crop"`,
  `focus_x = 0.5`. The plan's hint wins where the config allows it; `auto` → facecam_gameplay if a
  rect exists, else blurred_fit. `clipper-renderer layout-preview <login> --at <utc>` draws the rects
  on a frame. **Detection deferred** (ONNX face detector on keyframes) until configs go stale.

**Captions** (pure `captions.build_ass(words, plan, style, timemap)`, golden-file tested). Words from
`transcripts.words(stream, [in, out))` with the plan's `word_fixes` applied (review's text fixes),
mapped to output time (words across a cut dropped). Pages of 1–3 words (≤ 16 chars, break at
punctuation or pauses > 0.35 s, ≥ 0.4 s on screen); one Dialogue event per word slice with the active
word coloured and scaled (`\c`, `\fscx/\fscy`, an 80 ms `\t` pop), not `\k` (fill sweeps only);
emphasis words larger in the accent colour. `PlayResX/Y` = 1080×1920; text kept in the platform safe
zone (about x 80–1000, y 1150–1450: Shorts/TikTok UI covers the bottom and the right). `CaptionStyle`
presets (font, size, outline, colours, position) live in the renderer. Profanity masking deferred.

**Punch-ins.** v1 = hard punch-ins (the short-form look): a static zoomed crop of the panel (1.1–1.5×
on face/gameplay/centre) switched in with `overlay=enable='between(t,a,b)'`. Smooth zooms deferred.

**Assets** (the user supplies files; nothing ships in git). `assets/` volume + `manifest.toml`, per
asset: `id`, `file`, `sha256`, `kind` (sfx|music), `tags` (vine_boom, whoosh, record_scratch, …) or
`moods` (`MusicMood`), `duration_s`, licence `{name, source_url, licensee, proof, platforms,
attribution, expires}`. `clipper-renderer assets check` verifies hashes, measures loudness and
rejects entries without a licence, expired, or missing a target platform; only checked assets are
used. Choice is deterministic: matching assets sorted by id, index = hash(clip_id, cue) mod n. Mix:
SFX by `adelay` at output times (plan gain, per-asset peak normalisation); a music bed at −20 dB,
ducked by `sidechaincompress` keyed on the stream audio, 1 s fade-out; **no music** when the plan
flags `copyright_music_risk`. The judge reads the tag/mood list from the same manifest.

**Reproducibility.** `graph_hash` = sha256 of the plan JSON, filter script, ASS, inputs (chunk ids,
pinned keys, trims), layout config, asset and font hashes, renderer version and `ffmpeg -version`.
Same inputs → same graph; libx264 output is byte-identical, NVENC only visually (driver-dependent).

**GPU sharing** (RTX 4060, 8 GB). NVENC is a separate encoder block (~0.2–0.4 GB beside the
transcriber's ~2.8 GB); decode and the filter graph run on the 8 CPU cores, the expected bottleneck;
a local LLM (§19) is the real conflict. OBS/ShadowPlay also use NVENC (`auto` falls back to x264).
Heartbeat: encoder, renders done, p50 render time, queue.

**Retention.** Pinned copies and outputs belong to the renderer; a daily sweep reads
`lifecycle.lifecycle_states()` (§19): rejected/discarded/failed/unrenderable > 7 days → delete
pinned + outputs (review can reopen until then); published → keep outputs, pinned 30 days (user
decision). §9 becomes "video ~48 h; a clip pins the chunks it uses (`clips/{id}/pinned/`)".

**Audio-only channels.** Not rendered in v1 (`unrenderable`/`no_video`; the judge skips them by
default). User options: switch the channel to `full`, or a later audio-only format (waveform + captions).

**Interfaces + fakes.** `MediaSource` (chunk/pinned video; fake = temp dir), `AssetLibrary`,
`LayoutConfig`, `Ffmpeg` runner (real ffmpeg in tests; already on PATH for the recorder's). Synthetic
chunks from lavfi: frame index encoded as luma (`geq`), a 1 kHz beep at known UTC seconds, two
contiguous chunks with a 0.3 s jitter hole. Test profile: 270×480, libx264 ultrafast.

**Build plan.**
1. **7a Contracts + 0006 + pure parts** (timemap, layout geometry, ASS builder, graph builder, asset
   manifest + picker). *Done:* golden tests; generated ASS renders through ffmpeg's `ass` filter
   without warnings; `test_migrations` passes.
2. **7b Render loop, libx264, synthetic chunks.** *Done:* frames at a chunk boundary and at segment
   cuts match the time map ±1 frame (luma index); the beep lands within ±1 frame; −14 ± 1 LUFS,
   TP ≤ −1 dB; the same plan twice → the same `graph_hash` and identical bytes; audio_only →
   `unrenderable` without running ffmpeg; a crash + retry reuses the row; done → `review` job in the
   same transaction; S3 CopyObject works on SeaweedFS.
3. **7c GPU + real stream** (smoke on this PC). *Done:* NVENC works in the container (else x264-only
   is documented); a 30 s 1080p60 clip renders in ≤ 30 s p50 while the transcriber runs and its
   realtime factor drops < 10 % (numbers in §20); ohnepixel's layout fitted with `layout-preview`
   and approved by the user.
4. **7d Assets + polish** (needs the user's licensed library). *Done:* `assets check` rejects a
   missing licence and a hash mismatch; a clip with 2 SFX + music meets the loudness targets; the
   user signs off 5 real renders (captions, layout, hook).

### Proposed decision-log rows

| Date | Decision | Why |
|---|---|---|
| 2026-11-19 | Renderer image = slim + pinned static ffmpeg (NVENC + libass) with GPU video capability; `h264_nvenc` by default, libx264 fallback (`auto`) | NVENC is separate silicon and fast; x264 keeps rendering alive when NVENC is busy or unavailable (WSL2 unverified) |
| 2026-11-19 | Renders map UTC exactly: per-chunk accurate trims, jitter holes padded (held frame + silence), gaps become hard cuts; one time map drives captions, zooms and SFX | §5's chunk model; captions stay in sync across chunk boundaries |
| 2026-11-19 | A render pins the chunk video it uses (S3 copy to `clips/{id}/pinned/`, ±30 s); chunk video retention stays a blind 48 h rule | no cross-service holds; re-renders after review still work |
| 2026-11-19 | A finished render enqueues the `review` job (ref = render) in its own transaction; a render that gives up sends `notify(RENDER_FAILED)`; no `clipper_renders` NOTIFY channel | durable, deduped wake-up even while review is down; jobs are enqueued with their data |
| 2026-11-19 | Layouts from a per-channel `layouts.toml` (normalised rects, per-category overrides) on a volume; facecam detection deferred | correct from day one for a handful of channels; detection needs real data |
| 2026-11-19 | Captions = ASS generated by pure code from `words()` + the plan's `word_fixes`, one event per word slice, bundled OFL fonts via `fontsdir` | deterministic, golden-testable, full control over highlight, emphasis and fixes |
| 2026-11-19 | v1 punch-ins are hard static zooms; smooth zooms later | simple and deterministic; it's the common short-form look |
| 2026-11-19 | SFX/music only from a user-supplied local library with a manifest (sha256 + licence + platforms), checked by `assets check`; deterministic choice by hash | §2: licensed audio only, provable per render |
| 2026-11-19 | Loudness: two-pass `loudnorm`, −14 LUFS, −1 dBTP | platform normalisation targets for shorts; two-pass is deterministic |
| 2026-11-19 | Reproducibility = `graph_hash` over plan, inputs, layout config, assets, fonts and versions; byte-identical only with x264 | NVENC output may differ across drivers; the graph is what we control |
| 2026-11-19 | Each render writes final 1080×1920 (+ sha256), a 540×960 preview ≤ 20 MB, thumbnail, ASS and a manifest | the phone review needs a small file; the publisher checks the upload; the manifest makes renders auditable |
| 2026-11-19 | audio_only channels are not rendered in v1 | no video; an audio-only format is a later product decision |

### Contract changes

Hand-written migration **`0006_renderer`** (after `0005_judge`): one new table; nothing else changes.
`JobKind.RENDER` and `RenderPayload` come with 0005 (§19); `JobKind.REVIEW`/`NOTIFY`,
`ReviewPayload`, `NotifyPayload` and `NoticeKind` are §21's code-only contract (the renderer enqueues).

**`contracts.py`**

```python
class RenderStatus(StrEnum):  # renders.status (writer: renderer)
    RENDERING = "rendering"
    DONE = "done"
    FAILED = "failed"
    UNRENDERABLE = "unrenderable"  # see UnrenderableReason; job done, no retry

class UnrenderableReason(StrEnum):
    NO_VIDEO = "no_video"  # audio_only chunks (or no chunk) under a segment
    VIDEO_EXPIRED = "video_expired"  # retention deleted the video before it was pinned

RENDER_MANIFEST_VERSION = 1

class RenderSource(StoredModel):
    chunk_id: int
    seq: int
    key: str  # pinned copy that was read
    start_utc: UtcDatetime  # chunk start (time map origin)
    trim_from_s: float  # offsets inside the chunk
    trim_to_s: float
    out_at_s: float  # where it lands in the output
    pad_after_s: float = 0.0  # jitter fill after it

class RenderAsset(StoredModel):
    id: str
    kind: Literal["sfx", "music", "font"]
    sha256: str
    licence: str | None = None
    at_s: float | None = None  # output time (sfx)

class RenderManifest(StoredModel):
    """`render.json` next to each render: everything needed to re-run it."""

    version: Literal[1] = RENDER_MANIFEST_VERSION
    render_id: int
    clip_id: int
    plan_id: int
    profile: str
    renderer_version: str
    ffmpeg_version: str
    encoder: str  # h264_nvenc | libx264
    graph_hash: str
    layout: dict[str, Any]  # resolved kind + pixel rects
    layout_config_sha256: str | None
    sources: list[RenderSource]
    assets: list[RenderAsset] = Field(default_factory=list)
    width: int
    height: int
    fps: float
    duration_s: float
    loudness_lufs: float
    true_peak_db: float
    video_sha256: str
    warnings: list[str] = Field(default_factory=list)
```

**`models.py`**: **`Render`** (`renders`), writer: renderer. `id` bigint PK; `job_id` bigint UNIQUE
(no FK: one row per render job, a retry resumes it); `clip_id` FK clips; `plan_id` FK clip_plans;
`profile` text; `status` text default 'rendering' CHECK IN ('rendering','done','failed',
'unrenderable'); `unrenderable_reason` text null (CHECK `(status = 'unrenderable') =
(unrenderable_reason IS NOT NULL)`); `layout`, `encoder`, `renderer_version`, `ffmpeg_version`,
`graph_hash` text null; `pinned_keys` jsonb default '[]'; `video_key`, `preview_key`, `thumb_key`,
`captions_key`, `manifest_key` text null; `video_sha256` text null; `duration_s`, `fps`,
`loudness_lufs`, `render_s` float null; `width`, `height` int null; `bytes`, `preview_bytes` bigint
null; `warnings` jsonb default '[]'; `error` text null; `created_at`; `finished_at` null. INDEX
(`clip_id`, `created_at`); INDEX (`plan_id`).

**`storage`**
- `keys.clip_pinned_video(clip_id, seq)` → `clips/{clip_id}/pinned/{seq:06d}.ts`.
- `keys.render_file(clip_id, render_id, name)` → `clips/{clip_id}/renders/{render_id}/{name}`
  (`clip.mp4`, `preview.mp4`, `thumb.jpg`, `captions.ass`, `render.json`).
- `ObjectStore.copy(src_key, dst_key)` (S3 CopyObject, server side; for pinning).

**New `clipper_core.assets`** (shared by judge and renderer): `AssetEntry`/`AssetManifest` pydantic
models of `manifest.toml` (licence fields required), `load_manifest(path)`, `tags()`, `moods()`;
`pick(entries, tag, clip_id, cue_index)` (deterministic). No DB table: the library is files.

**`clipper_core.lifecycle`**: the render mapping is part of §19's helper (built in 6a; it reads
`renders.id/clip_id/status/created_at`, served by INDEX (`clip_id`, `created_at`)).

**Outside core.** `services/renderer` (`CLIPPER_RENDERER_*`: encoder, layouts path, assets path,
fonts dir, temp dir, `pin_margin_s` 30); compose service with the GPU reservation, `assets` and
`layouts.toml` volumes read-only; controller `STALE_AFTER[RENDER] = 20 min`. §9 wording: "video ~48 h;
a clip pins the chunks it uses (`clips/{id}/pinned/`)".

---

## 21. Review (phase 2) — proposed 2026-11-19, not yet agreed

**Status:** proposal, nothing built. All of it can be built and tested with fakes, no credentials;
the real bot needs the user's bot token and Telegram user id, remote web access needs Tailscale.
Depends on the *contracts* of §19 and §20 (0005/0006), not on their services.

**Role.** `services/review` (slim), one process: Telegram bot (long polling), web UI (FastAPI on
127.0.0.1:8080), `jobs.JobRunner` for `review` (from the renderer) and `notify` (from anyone) jobs,
a 30 s sweep (deadlines, reminders). Writer of `clip_reviews`, `review_events` (new); enqueues
`revise_plan`, `render` (re-render), `publish`, `publication_action`; never writes `clips`,
`clip_plans`, `renders`, `publications`. The only service that talks to Telegram.
**Reads:** `clips` (+ the candidate's score and moment type), the shown render's plan (`EditPlan`:
title, hashtags, `reasons[0]`), `renders` (`preview_key`, `thumb_key`, `duration_s`,
`preview_bytes`), `audio_metrics_1s` (music share), `words()`, and §22's `publish_routes`,
`publications`, `publication_stats`. §7's state comes from `lifecycle.lifecycle_state()` (§19).

**Review state.** `clip_reviews`, one row per clip (made by its first `review` job): `status`
`pending | revising | approved | rejected | expired | withdrawn`, the render shown, `deadline`,
metadata overrides, labels, Telegram message ids. `review_events`, append-only audit: who
(`telegram:<user id>`, `web:<name>`, `system`), when, what (action, labels, note, the render the
actor saw; `details`: patch, instruction, metadata before → after, targets). An action is one
transaction: lock the row, check the transition and that the actor saw the current render (stale
buttons are refused), insert the event, update the row, enqueue jobs. `external_id` (Telegram
callback/message id, web form token) is unique: redeliveries and double taps do nothing.

**Actions.**
- **Approve** (`pending`): one `publish` job per selected route (§22; pre-selected: routes with
  `auto_select`), `dedupe_key(PUBLISH, clip_id, account_id)`, same transaction. A channel without a
  route gets no Approve button (default deny, §2). Until §22 exists, `CLIPPER_REVIEW_PUBLISH_JOBS=0`:
  approve records the decision only.
- **Reject**: one or more `ReviewLabel`s (one tap each) + optional note.
- **Edit metadata** (post title, description, hashtags): overrides in `clip_reviews.post_metadata`;
  no plan, no render (§22 merges plan → overrides → route template). The burned-in title band
  changes only by Revise.
- **Revise** (in/out, title band, overlay text, caption text, layout, music). Review never writes
  plans: the event holds `details.patch` (`PlanPatch`) or `details.instruction` (free text: "start
  3 s earlier, punchier hook"); status `revising`; a `revise_plan` job (ref = event, dedupe
  `revise_plan:{event}`, priority `JUDGE_CONTEXT`: a human waits). The judge (§19) writes
  `clip_plans` v+1 + the `render` job; the new render's `review` job makes the clip `pending` again
  (Telegram replaces the message's media). `notify(REVISION_REFUSED | RENDER_FAILED)` → `pending`
  on the previous render, with the reason.
- **Re-render** (same plan, glitch): a `render` job, `dedupe_key(RENDER, plan_id, f"e{event_id}")`.
  **Withdraw** (after approve): `publication_action(delete)` per live post; publish jobs not started
  yet cancel themselves (§22). **Reopen** (expired/rejected → `pending`, +30 min; ≤ 7 days, while
  §20 keeps the files). **Submitted** (campaign link sent, §22).

**Deadlines.** `deadline = render ready + review window`: the smallest `RouteRules.review_window_s`
of the clip's routes (campaign speed; default 2 h, also without routes), capped at `clips.anchor_utc
+ max_age` (12 h). A revision keeps it but leaves ≥ 10 min after the new preview. A reminder reply
15 min before (edits don't ring); at the deadline the sweep writes `expire` (actor `system`) and
removes the buttons. Quiet hours are UTC (`CLIPPER_REVIEW_QUIET_UTC=23:00-07:00`), mute only.

**Telegram bot.** Own minimal Bot API client on httpx (~10 methods, like our IRC/Helix clients). Long
polling (`getUpdates`, 50 s), no webhook: no public endpoint on the home PC. One poller (lease
`review:telegram`). Updates left unconfirmed by a crash come again and are dropped by `external_id`.
- **Auth**: only `CLIPPER_REVIEW_TELEGRAM_USER_IDS` (numeric) are served; others are logged with their
  id (the user finds theirs by sending `/start`). Messages go to `CLIPPER_REVIEW_TELEGRAM_CHAT_ID`.
- **Clip message**: `sendVideo` (the render's `preview.mp4`, ≤ 20 MB, `supports_streaming`, `file_id`
  kept) + caption ≤ 1024 chars: channel, moment type, length, score, title, the judge's one-line
  reason, music share (Content ID risk), deadline, web link. Buttons ✅ Approve · ❌ Reject · ✏️ Title
  · 🔁 Change · 🌐 Web; callback data (≤ 64 bytes) = action + render id (`ap:5812`). Reject opens the
  labels; Title/Change ask via `ForceReply` (Change also offers ±3 s start/end buttons as patches);
  TikTok targets add its privacy choice + consent (§22).
- `/queue`, `/login` (web magic link), `/mute 2h`. After a decision all the clip's messages are edited
  ("✅ by web 12:03", buttons gone). ≤ 1 message/s per chat; 429 `retry_after` honoured.

**Web UI.** FastAPI + Jinja2 + htmx (vendored, no build step), calling the bot's action functions.
- **Pages**: queue (by deadline); clip (player, thumbnail, plan, metadata, history, publications and
  views from §22); labels per week by detector/judge/renderer; health (heartbeats, queue depths).
- **In/out editor**: the transcript (`transcripts.words()`) within ± 30 s of the current cut (§20's
  pin margin, so re-renders work for days) as clickable words (cut before/after a word), ± 0.5 s
  nudges, caption text fixes (`word_fixes`). Later: an hls.js scrubber over the pinned `.ts` files.
- **Media** is streamed by the service with HTTP Range (SeaweedFS on 127.0.0.1 isn't reachable from
  the phone); presigned URLs once storage is R2 (`CLIPPER_REVIEW_MEDIA=proxy|presigned`).
- **Auth**: 127.0.0.1 only, remote via **Tailscale Serve** (tailnet-only HTTPS, never Funnel).
  Password (argon2id hash in env) or a one-time `/login` link (5 min). Signed session cookie
  (HttpOnly, Secure, SameSite=Strict, 14 days) + a CSRF token on POSTs. A `reviewers` table later.

**Feedback labels** (`contracts.ReviewLabel`; no DB CHECK, it grows): detector `not_a_moment`,
`fake_spike`, `duplicate`; judge `weak_moment`, `bad_start`, `bad_end`, `missing_context`,
`too_long`, `bad_title`; renderer `bad_layout`, `bad_captions`, `bad_audio`; risk `copyright_music`,
`sensitive`, `no_rights`; `too_late`, `other`; `great` on approve. Operator edits are signal too
(`clip_plans` with `author = 'operator'` vs the judge's first plan).

**Interfaces and tests.** `TelegramApi` Protocol: real httpx client; `FakeTelegram` records sends,
injects updates, simulates 429 and "message is not modified". `MediaSource` (S3 ranges; fake dict),
injected clock, fixture rows for judge/renderer output, FastAPI TestClient. No test sends a message.

**Risks.** A leaked bot token lets someone pose as the bot but not act (allowlist): rotate it in
BotFather. Previews sit on Telegram's servers (private chat). Telegram down → the web UI still works.

**Build plan.**
1. **21a Contracts + 0007 + lifecycle (review part).** *Done:* `test_migrations` passes; state
   mapping and transitions tested.
2. **21b Core**: actions, `review`/`notify` handlers, sweep. *Done:* approve → one `publish` job per
   route in the same transaction; concurrent approve/reject → one wins; stale render refused; revise →
   event + `revise_plan`; refused revision → `pending`; expiry and reopen.
3. **21c Telegram**: client, poller + lease, messages, callbacks, ForceReply. *Done:* a FakeTelegram
   test per flow; foreign user ignored; redelivered update → no second event; 429 obeyed.
4. **21d Web UI**: login, queue, clip page, word picker, edits, Range proxy. *Done:* TestClient: no
   session → login, no CSRF → 403, edit → event + job, Range → 206; checked by hand on a phone.

### Proposed decision-log rows

| Date | Decision | Why |
|---|---|---|
| 2026-11-19 | Review state in review-owned `clip_reviews` (one row per clip) + append-only `review_events` (who/when/what); §7's later states are derived by §19's `lifecycle.lifecycle_state()` | one writer per table; the audit trail is complete and never rewritten |
| 2026-11-19 | Review never writes plans: a change is stored as an event (`PlanPatch` or free-text instruction) and sent as a `revise_plan` job; the judge resolves it, writes the new `clip_plans` version (`author`, `parent_id`) and the render job, or refuses via `notify` | one writer for plans; validation (word snapping, video) and the LLM live in one place |
| 2026-11-19 | Post title/description/hashtag edits are review overrides (`clip_reviews.post_metadata`), no new plan or render; the burned-in title band changes only by a revision | metadata isn't in the video; a re-render would cost a minute for nothing |
| 2026-11-19 | Only review talks to Telegram; the other services send `notify` jobs (`dedupe_key(NOTIFY, ref_id, kind)`) | one place holds the bot token; durable, deduped notices |
| 2026-11-19 | Telegram: own minimal Bot API client (httpx), long polling, one instance (lease), user-id allowlist, buttons carry the render id, updates deduped by id | no public endpoint on the home PC; stale buttons and redeliveries are harmless; fakeable |
| 2026-11-19 | Web UI: FastAPI + Jinja2 + htmx, bound to 127.0.0.1, remote via Tailscale Serve only; password or bot magic link, signed cookie, CSRF | no build step; never on the public internet; one operator now |
| 2026-11-19 | In/out edits by clicking transcript words (`transcripts.words()`) within the render's ±30 s pin margin | sentence-accurate cuts on a phone; re-renders work after chunk video expires |
| 2026-11-19 | Review deadline per clip from the routes' `review_window_s` (default 2 h), capped at moment + 12 h; expired = discarded, reopenable for 7 days; quiet hours (UTC) only mute | campaigns reward speed; a stale clip should not block the queue |
| 2026-11-19 | Fixed, attributed `ReviewLabel`s (detector/judge/renderer/risk) + note | feedback for tuning is queryable without parsing text |
| 2026-11-19 | Approve enqueues one `publish` job per route in the same transaction; a channel without a route can't be approved | default deny on rights (§2); nothing gets lost between approval and upload |

### Contract changes

Migration **`0007_review`** (after `0006_renderer`; additive: two new tables, FKs to `clips` (0005)
and `renders` (0006), and the FK `clip_plans.review_event_id → review_events` that 0005 left out).
The enums and payloads below are code only (no DDL): whichever of 6a, 7a, 21a is built first adds
them, since the judge and renderer enqueue these jobs before review exists. (6a, 2026-11-20, added
the job kinds, `NoticeKind`, `PlanPatch` and the three payloads; the rest is left for 21a.)

**`contracts.py`**
```python
class JobKind(StrEnum):
    ...
    REVIEW = "review"            # ref_id = renders.id; the renderer enqueues it with the render
    REVISE_PLAN = "revise_plan"  # ref_id = review_events.id; review enqueues, the judge runs it
    NOTIFY = "notify"            # ref_id = what the notice is about; anyone enqueues, review runs it

class ReviewStatus(StrEnum):  # clip_reviews.status
    PENDING = "pending"; REVISING = "revising"; APPROVED = "approved"
    REJECTED = "rejected"; EXPIRED = "expired"; WITHDRAWN = "withdrawn"

class ReviewAction(StrEnum):  # review_events.action
    RENDER_READY = "render_ready"; APPROVE = "approve"; REJECT = "reject"
    EDIT_METADATA = "edit_metadata"; REVISE = "revise"; RERENDER = "rerender"; EXPIRE = "expire"
    REOPEN = "reopen"; WITHDRAW = "withdraw"; SUBMITTED = "submitted"; NOTE = "note"

class ReviewLabel(StrEnum):  # review_events.labels; grows (no DB CHECK)
    NOT_A_MOMENT = "not_a_moment"; FAKE_SPIKE = "fake_spike"; DUPLICATE = "duplicate"     # detector
    WEAK_MOMENT = "weak_moment"; BAD_START = "bad_start"; BAD_END = "bad_end"             # judge
    MISSING_CONTEXT = "missing_context"; TOO_LONG = "too_long"; BAD_TITLE = "bad_title"   # judge
    BAD_LAYOUT = "bad_layout"; BAD_CAPTIONS = "bad_captions"; BAD_AUDIO = "bad_audio"     # renderer
    COPYRIGHT_MUSIC = "copyright_music"; SENSITIVE = "sensitive"; NO_RIGHTS = "no_rights"  # risk
    TOO_LATE = "too_late"; OTHER = "other"; GREAT = "great"  # GREAT: positive, on approve

class NoticeKind(StrEnum):
    REVISION_REFUSED = "revision_refused"  # judge; ref = review event
    RENDER_FAILED = "render_failed"  # renderer; ref = render
    # §22 adds PUBLISH_FAILED, PUBLICATION_UNKNOWN, TAKEDOWN, NEEDS_REAUTH,
    # SUBMIT_TO_CAMPAIGN, PUBLISH_CANCELLED

class PostMetadata(StoredModel):  # jsonb; review overrides (None = keep) and publisher's final
    title: str | None = None
    description: str | None = None
    hashtags: list[str] | None = None

class PlanPatch(BaseModel):  # review_events.details.patch; None = keep, lists replace whole
    # operator input (built in 6a): frozen, extra="forbid" also in the nested models, title not
    # blank, music and no_music not both
    in_utc: UtcDatetime | None = None  # moves segments[0].start_utc
    out_utc: UtcDatetime | None = None  # moves segments[-1].end_utc
    hook: Hook | None = None
    title: str | None = None  # the burned-in title band (post texts: PostMetadata)
    caption_style: CaptionStyle | None = None
    word_fixes: list[WordFix] | None = None
    layout: LayoutHint | None = None
    music: MusicPlan | None = None
    no_music: bool = False

class ReviewPayload(Payload):
    render_id: int

class RevisePlanPayload(Payload):
    review_event_id: int
    clip_id: int
    base_plan_id: int  # the plan of the render the reviewer saw

class NotifyPayload(Payload):
    kind: NoticeKind
    text: str
    clip_id: int | None = None
    publication_id: int | None = None
    severity: Literal["info", "warning", "error"] = "info"

REVIEW_TELEGRAM_LEASE = "review:telegram"  # the single getUpdates poller
```
Dedupe: `review:{render}`, `revise_plan:{event}`, `dedupe_key(NOTIFY, ref_id, kind)` (ref = the
render, review event, publication or account; account notices add the UTC date as a part).

**`models.py`** (CHECK texts repeated in the migration, as in 0002)
- **`ClipReview`** (`clip_reviews`), writer: review. `clip_id` bigint PK FK clips; `status` text
  default 'pending' CHECK IN ('pending','revising','approved','rejected','expired','withdrawn');
  `render_id` FK renders null (the render shown); `deadline` timestamptz null; `first_shown_at`,
  `decided_at` timestamptz null; `decided_by` text null; `post_metadata` jsonb default '{}'
  (`PostMetadata` overrides; not `metadata`, which SQLAlchemy reserves); `labels` jsonb default '[]';
  `telegram` jsonb default '{}' (`chat_id`, `message_ids`, `file_id`, open `ForceReply` prompts);
  `created_at`, `updated_at`. INDEX (`status`, `deadline`).
- **`ReviewEvent`** (`review_events`), writer: review, append-only. `id` bigint PK; `clip_id` FK
  clips; `action` text (no CHECK: `ReviewAction` grows); `actor` text; `render_id` FK renders null;
  `labels` jsonb default '[]'; `note` text null; `details` jsonb default '{}'; `external_id` text
  UNIQUE null; `created_at`. INDEX (`clip_id`, `created_at`); INDEX (`action`, `created_at`).
- `ClipPlan.review_event_id`: `ForeignKey("review_events.id")` (migration: `create_foreign_key`).

**`clipper_core.lifecycle`**: the review mapping is already in §19's helper (6a: `review_status`
argument, `lifecycle_states()` reads `clip_reviews.clip_id/status` once 0007 ran); 21a adds tests
against the real table.

**Outside core**: controller `STALE_AFTER` += `REVIEW: 5 min`, `NOTIFY: 5 min` (`REVISE_PLAN` with
§19); `services/review` (pyproject, Dockerfile, workspace member), compose `review` with
`127.0.0.1:8080:8080`; `.env.example`: `CLIPPER_REVIEW_TELEGRAM_BOT_TOKEN`, `_TELEGRAM_USER_IDS`,
`_TELEGRAM_CHAT_ID`, `_WEB_PASSWORD_HASH`, `_SESSION_SECRET`, `_PUBLIC_URL`, `_QUIET_UTC`, `_MEDIA`,
`_PUBLISH_JOBS`.

---

## 22. Publisher (phase 2) — proposed 2026-11-19, not yet agreed

**Status:** proposal. Pipeline, scheduler, token crypto and a fake platform can be built now. Real
adapters wait for the user's §2 decision (platforms, accounts) and app credentials; TikTok and
Instagram also for an approval. Platform facts re-checked 2026-11-19 (table): **§3 update proposed**.

**Role.** `services/publisher` (slim), `jobs.JobRunner` for `publish`, `sync_publication`,
`publication_action` (the first and last from review, §21), plus a daily token sweep. Writer of
`platform_accounts`, `publish_routes`, `publications`, `publication_stats` (new). CLI
`clipper-publisher` (part of the service, like `clipper-channels`): `connect <platform>`, `accounts`,
`route add|list|disable`, `pause|resume`, `gen-key`, `rotate-key`, `smoke`. Reaches the operator only
through `notify` jobs (§21). Reads `clip_reviews`, the approved render (`video_key`, `video_sha256`)
and its plan.

| Platform | Facts (official docs, 2026-11-19) |
|---|---|
| YouTube | `videos.insert` costs 1 unit of its own Video Uploads bucket: 100 calls/day per project; `videos.list` 1, `videos.delete` 50 units; quotas reset at midnight PT. The page now says "Videos uploaded from unverified API projects are not restricted to private viewing mode" (§3's private-lock note is outdated). OAuth consent screen in "Testing" → refresh tokens expire after 7 days. Resumable upload, ≤ 256 GB. |
| TikTok | Direct Post `POST /v2/post/publish/video/init/` (`video.publish`): FILE_UPLOAD (chunked PUT; upload URL valid 1 h) or PULL_FROM_URL (verified domain). `privacy_level` must be one of the `creator_info` options. 6 requests/min per user token; a daily cap per creator (reportedly ~15, shared across clients). Unaudited clients: private viewing only, and only private accounts can post. Status `PROCESSING_UPLOAD/DOWNLOAD → PUBLISH_COMPLETE` or `FAILED`. Flags `brand_content_toggle`, `is_aigc`. No delete endpoint found. |
| Instagram | Professional account; `media_type=REELS` container → `media_publish`; container `IN_PROGRESS/FINISHED/PUBLISHED/ERROR/EXPIRED` (24 h), check once a minute ≤ 5 min. 100 API posts per rolling 24 h (the same docs say 50 elsewhere: read `content_publishing_limit`); 400 containers/24 h. Instagram Login (`instagram_business_content_publish`) needs a public `video_url`; resumable upload of a local file only with Facebook Login for Business (Page token). A delete-media endpoint was announced 2025-12 (UNVERIFIED). |
| Whop | Content Rewards: clippers post on their own accounts and submit the link in Whop ("Submit Video"); views verified via linked accounts (OAuth or a bio code); the owner approves in the dashboard; pay per 1,000 views until the budget ends. No Content Rewards API found; Whop's API has bounty submissions (needs a user credential) — whether campaigns use it is UNVERIFIED. |

**Interfaces.** One `Publisher` per platform (in the service; `httpx`, no Google/Meta SDKs):
```python
class Publisher(Protocol):
    platform: Platform
    limits: PlatformLimits  # text lengths, max duration/size, default caps
    async def start(self, auth: AccountAuth, video: VideoFile, meta: PostMetadata) -> Handle
    async def upload(self, auth: AccountAuth, handle: Handle, video: VideoFile) -> Handle  # resumes
    async def finish(self, auth: AccountAuth, handle: Handle) -> PostRef | Pending
    async def recover(self, auth: AccountAuth, handle: Handle) -> Recovery  # Posted|InProgress|NotPosted|Unknown
    async def status(self, auth: AccountAuth, ref: PostRef) -> PostStatus  # + visibility, reason
    async def stats(self, auth: AccountAuth, refs: Sequence[PostRef]) -> dict[str, PostStats]
    async def delete(self, auth: AccountAuth, ref: PostRef) -> bool  # False: no API, by hand
    async def refresh(self, auth: AccountAuth) -> TokenSet
```
Errors: `Transient` (job retry, backoff), `RateLimited(until)`/`QuotaExceeded(until)` →
`jobs.DeferJob`, `AuthExpired` (refresh once, then `needs_reauth`), `Refused(reason)` (permanent).
`FakePlatform` (in memory; faults at every step: error before/after the post exists, crash between
steps, 429, quota, slow processing, later removal) + **one contract test suite** every adapter passes
(the fake directly, real ones on `httpx.MockTransport` with documented response shapes). `Submitter`:
`ManualSubmitter` now (a `notify` "submit this link"); an API one only if Whop exposes campaigns.

**Publish job** (ref = clip; one per account, `publish:{clip}:{account}`, enqueued by review's approve):
1. Get or create the `publications` row (`scheduled`). Cancel (+ `notify(PUBLISH_CANCELLED)`) if
   `clip_reviews.status` is no longer `approved` or the slot would be after `max_publish_delay_s`.
2. **Slot** (below) in the future → `scheduled_for` + `DeferJob(slot)` (no attempt used).
3. Lease `publish:publication:{id}` (renewed while uploading: a stale-requeued job can't run in
   parallel); download the render's `clip.mp4`, check `renders.video_sha256`.
4. `start()` → **the handle is stored (encrypted) and committed before the first byte** (YouTube
   resumable session URI, TikTok `publish_id` + upload URL, Instagram container id); `uploading`.
5. `upload()`, `finish()` → `platform_post_id`, `post_url`, `posted_at`, `processing` (Instagram: the
   container is polled via `DeferJob(60 s)`, ≤ 5 min, then `media_publish`); enqueue `sync_publication`.
6. **A retry with a handle** asks `recover()`: posted → store ids; in progress → continue; not posted
   → new `start()`; **unknown** (handle expired, platform can't tell) → `unknown` +
   `notify(PUBLICATION_UNKNOWN)`: the operator checks and answers in review ("posted: <url>" / "not
   posted"), sent back as `publication_action`. Never a blind re-upload.

**Never double-post** = unique `(clip_id, platform_account_id)` over rows not `failed`/`cancelled`/
`deleted` + handle first + lease + `unknown`. `failed` = surely not posted; a retry makes a new row.

**Scheduling** (ours; TikTok has no API scheduling, so one rule for all): the earliest time with the
account `active`; < `daily_cap` posts in the last 24 h (defaults under platform caps: YouTube 10,
TikTok 5, Instagram 10 per account); ≥ `min_gap_s` since its last post (default 20 min); inside its
posting windows (UTC); YouTube: < 100 inserts by all accounts of the project since the last midnight
PT (zoneinfo, stored UTC). Oldest approval first; one account per platform per clip by default.

**Metadata.** `PostMetadata` = the plan's `title` + `hashtags` (§19) → review overrides
(`clip_reviews.post_metadata`, §21) → route template, clamped per platform (YouTube title ≤ 100
chars without `<>`, description ≤ 5000 bytes, tags ≤ 500 chars, `categoryId` 20,
`selfDeclaredMadeForKids=false`; TikTok/Instagram caption ≤ 2200, IG ≤ 30 hashtags). **Credit is
mandatory**: a template must contain `{channel_url}` ("🎮 {streamer} live on Twitch:
https://twitch.tv/{login}"), checked when the route is added. Route rules add required hashtags/
mentions and disclosure (TikTok `brand_content_toggle`; YouTube's paid-promotion flag via API:
UNVERIFIED). `containsSyntheticMedia`/`is_aigc` false (real footage). What was sent is stored.

**Status, stats, takedowns.** One `sync_publication` job per publication defers itself: every 30 s
while `processing` (≤ 30 min, then `unknown`), then +15 min, 1 h, 3 h, 6 h, 12 h, 24 h, 48 h, daily to
30 days, weekly to 90 days, then done. Each run stores visibility and a `publication_stats` row (views,
likes, comments, shares; extras jsonb). **Rejected/removed** (YouTube `uploadStatus=rejected` +
`rejectionReason`, a vanished post, TikTok `FAILED`) → status + reason, `notify(TAKEDOWN)` (review
labels the clip), and the **account paused** until the operator resumes it (3 YouTube strikes end a
channel). Content ID claims aren't in the Data API for non-partners (UNVERIFIED): reported in review.
**Deletion** (`publication_action(delete)`, from withdraw): YouTube `videos.delete` (`youtube` scope);
Instagram if its endpoint works; TikTok: a notice, the operator deletes by hand and confirms.

**Tokens.** **AES-256-GCM** (`cryptography`) in `platform_accounts`, AAD = `platform_accounts:{id}:
{field}` (no moving a ciphertext to another row), key-id prefix for rotation; keys in
`CLIPPER_PUBLISHER_TOKEN_KEYS` (`.env` now, later a Docker secret file or the server's secret
manager: only the key moves; Vault-style infra isn't worth it for one operator). Only the publisher
decrypts; backups hold ciphertext; never logged. **Refresh** when < 5 min are left, under `SELECT …
FOR UPDATE` (TikTok/Instagram rotate tokens; the new ones commit before use); a daily sweep for
expiry < 7 days (IG 60 d, TikTok refresh 365 d: UNVERIFIED); `invalid_grant` → `needs_reauth` +
`notify(NEEDS_REAUTH)`, posts defer. **Connect**: YouTube via loopback redirect
(`http://127.0.0.1:<port>`); TikTok/Meta need a registered HTTPS redirect (the Tailscale HTTPS name,
or paste the URL back).

**Legal / ToS (brief).** Rights first (§2): **no route, no post**; each route records its basis
(`streamer_account`, `campaign` + URL, `written_permission`); backfilled clips only if the user says
so (§17). YouTube: one API project (extra projects to bypass quota break the ToS), more quota via the
audit form; spam/repetitive content, "reused content" demonetisation. TikTok: the audit checks the
posting UX (preview, privacy choice, consent, music confirmation, disclosure); unaudited = private
only. Instagram: Platform Terms, app review for others' accounts. Campaigns: own accounts, no view
botting; paid clips may need an ad disclosure (FTC/ASA). Stream music → Content ID.

**Build plan.**
1. **22a Contracts + migration 0008 + `jobs.DeferJob` + crypto + CLI** (fake platform). *Done:*
   `test_migrations` passes; wrong AAD/key fails, rotation works; a deferred job keeps its attempts;
   lifecycle maps publications (§19).
2. **22b Pipeline on `FakePlatform`**: publish job, scheduler, recovery, sync + stats, actions, circuit
   breaker, notices. *Done:* random fault sequences (crash/error at every step, then retries) never
   give two posts; quota/429 defer; removal pauses the account; withdraw deletes.
3. **22c YouTube adapter** (the user's Google project only for the smoke). *Done:* contract suite on
   MockTransport; the user runs `clipper-publisher smoke` (one test clip, `private`, then deleted).
4. **22d TikTok + Instagram + manual campaign submission** (after §2 and app approvals). *Done:*
   contract suite passes for both; a live post → "submit" notice → `submitted` event in review.

### Proposed decision-log rows

| Date | Decision | Why |
|---|---|---|
| 2026-11-19 | One `Publisher` Protocol per platform (httpx, no SDKs), a `FakePlatform` with fault injection and one contract test suite for all adapters; YouTube first | platforms are the user's decision (§2); everything else is testable now |
| 2026-11-19 | Never double-post: partial unique `(clip, account)`, the row before the upload, the platform handle committed before the first byte, a lease while uploading, `recover()` on retry, `unknown` goes to the operator | a crash between "posted" and "stored" must not cause a second post |
| 2026-11-19 | OAuth tokens AES-256-GCM-encrypted in Postgres (AAD = row + field, key ids), key from env/secret file, only the publisher decrypts | backups and other services never see usable tokens; no extra infra for one operator |
| 2026-11-19 | Accounts and routes are written only by the publisher (its CLI does OAuth connect and route setup) | one writer per table, like `clipper-channels` for the controller |
| 2026-11-19 | Rights default deny: a channel is publishable only through a `publish_routes` row with a rights basis; credit to the streamer is mandatory in every template | §2: DMCA/strikes; campaigns require attribution |
| 2026-11-19 | The publisher schedules itself (per-account caps, min gap, UTC windows, YouTube project quota until midnight PT) and waits with a generic `jobs.DeferJob` | TikTok has no API scheduling; waiting for a slot or quota must not burn retry attempts |
| 2026-11-19 | Post text = plan title/hashtags → review overrides → route template, clamped per platform; what was sent is stored | one merge order; the judge suggests, the operator decides, the route enforces credit and rules |
| 2026-11-19 | Status and stats from one self-deferring `sync_publication` job per publication on a decaying schedule; `publication_stats` from day one | one durable timer per post; views feed the analytics loop early |
| 2026-11-19 | A platform rejection/removal pauses the account and labels the clip | repeated copyright hits end a channel (3 YouTube strikes) |
| 2026-11-19 | Campaign submission is manual (Telegram task + `submitted` event) until Whop exposes Content Rewards via API | no submission API found |
| 2026-11-19 | §3 update proposed: YouTube unverified projects no longer private-locked (videos.insert page, re-checked 2026-11-19); insert = 1 unit of a 100/day bucket; Instagram 100 (or 50) per 24 h via `content_publishing_limit`; TikTok 6 req/min per token, unaudited = private viewing and private accounts only | official docs re-checked 2026-11-19; §3 itself changes only when this is agreed |

### Contract changes

Migration **`0008_publisher`** (after `0007_review`; additive: four new tables; FKs to `channels`,
`clips`, `renders`, `review_events`).

**`contracts.py`**
```python
class JobKind(StrEnum):
    ...
    PUBLISH = "publish"  # ref_id = clips.id, dedupe part = account id; review enqueues on approve
    SYNC_PUBLICATION = "sync_publication"  # ref_id = publications.id; publisher, self-deferring
    PUBLICATION_ACTION = "publication_action"  # ref_id = publications.id; review enqueues

class Platform(StrEnum):
    YOUTUBE = "youtube"; TIKTOK = "tiktok"; INSTAGRAM = "instagram"
    FAKE = "fake"  # tests only; the service enables it only when set explicitly

class AccountStatus(StrEnum):
    ACTIVE = "active"; PAUSED = "paused"; NEEDS_REAUTH = "needs_reauth"; DISABLED = "disabled"

class PublicationStatus(StrEnum):
    SCHEDULED = "scheduled"; UPLOADING = "uploading"; PROCESSING = "processing"; LIVE = "live"
    UNKNOWN = "unknown"  # may or may not exist on the platform: the operator decides
    FAILED = "failed"  # surely not posted
    CANCELLED = "cancelled"; REJECTED = "rejected"; REMOVED = "removed"; DELETED = "deleted"

class Visibility(StrEnum):
    PUBLIC = "public"; UNLISTED = "unlisted"; LIMITED = "limited"; PRIVATE = "private"

class RightsBasis(StrEnum):
    STREAMER_ACCOUNT = "streamer_account"; CAMPAIGN = "campaign"
    WRITTEN_PERMISSION = "written_permission"

class PublicationActionKind(StrEnum):
    DELETE = "delete"; MARK_POSTED = "mark_posted"; MARK_NOT_POSTED = "mark_not_posted"
    RETRY = "retry"; CANCEL = "cancel"

class NoticeKind(StrEnum):  # added to §21's enum
    PUBLISH_FAILED = "publish_failed"; PUBLICATION_UNKNOWN = "publication_unknown"
    TAKEDOWN = "takedown"; NEEDS_REAUTH = "needs_reauth"
    SUBMIT_TO_CAMPAIGN = "submit_to_campaign"; PUBLISH_CANCELLED = "publish_cancelled"

class RouteRules(StoredModel):  # publish_routes.rules; review reads review_window_s, max_age_s
    review_window_s: int = 7200
    max_age_s: int = 43200
    max_publish_delay_s: int = 21600
    required_hashtags: list[str] = []
    required_mentions: list[str] = []
    disclosure: Literal["none", "paid_partnership"] = "none"
    submission: Literal["none", "manual"] = "none"
    max_duration_s: float | None = None

class PostTemplate(StoredModel):  # publish_routes.template; must contain {channel_url}
    title: str = "{title}"
    description: str = "{title}\n\n🎮 {streamer} live on Twitch: {channel_url}\n{hashtags}"
    hashtags: list[str] = ["{login}", "twitch"]

class PublishPayload(Payload):
    clip_id: int
    render_id: int  # clip_reviews.render_id at approve
    platform_account_id: int
    route_id: int
    review_event_id: int
    tiktok_privacy: str | None = None  # chosen by the operator at approve (TikTok audit UX)

class SyncPublicationPayload(Payload):
    publication_id: int

class PublicationActionPayload(Payload):
    publication_id: int
    action: PublicationActionKind
    review_event_id: int
    post_url: str | None = None  # MARK_POSTED

def publication_lease(publication_id: int) -> str:
    return f"publish:publication:{publication_id}"
```
`publish` dedupe: `dedupe_key(PUBLISH, clip_id, account_id)`; a retry from review adds a part
(`retry{n}`), since a `done` job with the same key never runs again.

**`jobs.py`** (generic; any handler may use it)
```python
class DeferJob(Exception):
    """Raised by a handler that must wait (a slot, a rate limit, a quota reset): the job goes back
    to `queued` with `run_after = until` (UTC) and the attempt is not counted."""
    def __init__(self, until: datetime, reason: str) -> None: ...

async def defer(session: AsyncSession, job: ClaimedJob, until: datetime, reason: str) -> None:
    """UPDATE jobs SET status='queued', run_after=:until, attempts=attempts-1, locked_by=NULL,
    last_error=:reason, updated_at=now() WHERE id=:id"""
```
`JobRunner.run_once` catches `DeferJob` before `PermanentJobError` and logs "job deferred" with
`until` and `reason`. The handler owns the overall limit (`max_publish_delay_s`).

**`models.py`**
- **`PlatformAccount`** (`platform_accounts`), writer: publisher. `id` bigint PK; `platform` text
  (no CHECK: platforms grow); `external_id` text (channel id / open_id / IG user id); `handle` text;
  `status` text default 'active' CHECK IN ('active','paused','needs_reauth','disabled');
  `status_reason` text null; `scopes` jsonb default '[]'; `key_id` text null; `access_token_enc`,
  `refresh_token_enc` bytea null; `access_expires_at`, `refresh_expires_at`, `refreshed_at`
  timestamptz null; `capabilities` jsonb default '{}' (audited, privacy options, max duration);
  `policy` jsonb default '{}' (`daily_cap`, `min_gap_s`, `windows_utc`); `created_at`, `updated_at`.
  UNIQUE (`platform`, `external_id`).
- **`PublishRoute`** (`publish_routes`), writer: publisher. `id` bigint PK; `channel_id` FK channels;
  `platform_account_id` FK platform_accounts; `active`, `auto_select` bool default true;
  `rights_basis` text CHECK IN ('streamer_account','campaign','written_permission'); `rights_ref`
  text not null (campaign URL, permission note); `campaign_name`, `campaign_url` text null; `rules`
  jsonb default '{}' (`RouteRules`); `template` jsonb default '{}' (`PostTemplate`); `created_at`,
  `updated_at`. UNIQUE (`channel_id`, `platform_account_id`).
- **`Publication`** (`publications`), writer: publisher. `id` bigint PK; `clip_id` FK clips;
  `render_id` FK renders; `platform_account_id` FK; `route_id` FK publish_routes; `review_event_id`
  FK review_events; `status` text default 'scheduled' CHECK IN ('scheduled','uploading',
  'processing','live','unknown','failed','cancelled','rejected','removed','deleted'); `visibility`
  text null; `scheduled_for` timestamptz; `handle_enc` bytea null (session URI / publish_id /
  container id: a bearer capability, so encrypted); `handle_expires_at` null; `platform_post_id`,
  `post_url` text null; `post_metadata` jsonb (what was sent); `render_sha256` text;
  `upload_started_at`, `posted_at`, `live_at`, `ended_at`, `status_checked_at` timestamptz null;
  `status_detail` jsonb default '{}'; `error` text null; `created_at`, `updated_at`.
  UNIQUE INDEX `ux_publications_clip_account_active` (`clip_id`, `platform_account_id`) WHERE
  `status NOT IN ('failed','cancelled','deleted')`; UNIQUE (`platform_account_id`,
  `platform_post_id`); INDEX (`platform_account_id`, `posted_at`); INDEX (`status`, `scheduled_for`).
- **`PublicationStat`** (`publication_stats`), writer: publisher. PK (`publication_id`, `ts`);
  `views`, `likes`, `comments`, `shares` bigint null; `extra` jsonb default '{}' (watch time,
  engaged views, saves).

**`clipper_core.lifecycle`**: the publication mapping is already in §19's helper (6a:
`publication_statuses` argument, `lifecycle_states()` reads `publications.clip_id/status` once 0008
ran); 22a adds tests against the real table. **0008 needs INDEX (`clip_id`) on `publications`** for
lifecycle's per-clip subquery (the partial unique index leaves out failed/cancelled/deleted rows).

**Outside core**: controller `STALE_AFTER` += `PUBLISH: 30 min` (the lease protects a slow upload),
`SYNC_PUBLICATION: 10 min`, `PUBLICATION_ACTION: 10 min`; `services/publisher` (deps `httpx`,
`cryptography`; `PublisherSettings`, `CLIPPER_PUBLISHER_*`: `TOKEN_KEYS`, `YOUTUBE_CLIENT_ID/SECRET`,
`TIKTOK_CLIENT_KEY/SECRET`, `META_APP_ID/SECRET`, `INSTAGRAM_LOGIN=facebook|instagram`), compose
`publisher` (no ports).

**Sources checked 2026-11-19:** YouTube [quota costs](https://developers.google.com/youtube/v3/determine_quota_cost),
[videos.insert](https://developers.google.com/youtube/v3/docs/videos/insert),
[OAuth 2.0 (Testing → 7-day tokens)](https://developers.google.com/identity/protocols/oauth2);
TikTok [Direct Post](https://developers.tiktok.com/doc/content-posting-api-reference-direct-post),
[post status](https://developers.tiktok.com/doc/content-posting-api-reference-get-video-status);
Meta [Content Publishing](https://developers.facebook.com/docs/instagram-platform/content-publishing/),
[API updates 2025-12-03](https://developers.facebook.com/blog/post/2025/12/03/instragram-api-updates/);
Telegram [Bot FAQ (limits, 50 MB upload)](https://core.telegram.org/bots/faq);
Whop [Content Rewards](https://docs.whop.com/memberships-and-access/third-party-apps/content-rewards),
[Content Rewards FAQ](https://contentrewards.com/faqs),
[bounty program API](https://docs.whop.com/developer/bounties/run-a-bounty-program).

### Decisions and credentials needed from the user (§19–§22)

- **Rights model (§2)**: which channels may be clipped and published, on which platforms and
  accounts; per route the rights basis, campaign, rules, template and caps; whether backfilled clips
  (§17) may be published.
- **Judge model**: provider + model, picked from `clipper-judge eval` numbers (approve the eval spend),
  and its API key.
- **Judge budgets**: USD per stream-hour and per day (e.g. $0.50 / $10); judge audio_only channels or
  switch them to `full` (audio_only can't be rendered in v1).
- **Taste**: titles, clip lengths (default 10–60 s), caption style presets; sign off 5 real renders.
- **Layouts**: facecam rects per channel in `layouts.toml` (ohnepixel first, via `layout-preview`).
- **Licensed SFX/music library** with licence proof per asset (nothing ships without it).
- **Retention**: discarded clips 7 days, pinned video of published clips 30 days.
- **Review**: Telegram bot token (@BotFather `/newbot`, `/setjoingroups` off), your numeric user
  id(s) and chat id; web password; Tailscale on PC + phone (optional); review window and max age per
  campaign; quiet hours.
- **YouTube**: Google Cloud project, Data API v3, OAuth client (Desktop), consent screen "In
  production", scopes `youtube.upload` + `youtube.readonly` (+ `youtube` to delete); run the smoke.
- **TikTok**: developer app (Login Kit + Content Posting API), redirect URI, client key/secret, the
  audit. **Instagram**: professional account (+ Facebook Page for local-file upload), Meta app
  id/secret, app review beyond accounts with a role on the app.
- **Whop**: account and the campaigns to join (submission stays manual).
