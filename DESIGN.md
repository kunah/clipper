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
| Twitch HLS | 2 s MPEG-TS segments (H.264 + AAC 48 kHz + timed ID3), each with `EXT-X-PROGRAM-DATE-TIME`; the live playlist holds only ~30 s, so a recorder restarted within ~30 s loses nothing. Source 1080p60 ≈ 8.4 Mbps (~63 MB/min); audio ≈ 165 kbps (~1.2 MB/min). Ads are inserted for our recorder (a viewer): segments titled "Amazon…" or inside a `twitch-stitched-ad` DATERANGE; filtering them leaves gaps. Turbo token avoids most ads. VODs get muted for copyrighted music; live recordings don't. |
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
| transcriber | faster-whisper on GPU, audio features | chunk_transcripts, transcript_segments | CUDA |
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
- Docker Desktop's disk image moved to `D:\DockerDesktopWSL` (C: is small). Named volumes.
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
4. ⬜ transcriber: GPU, word timestamps, hot/cold priority, benchmark the 4060.
5. ⬜ backfill tool: past VODs + chat replay + top clips → detector development offline.

**Phase 2**: detector (live + offline, emote categories, fake-spike filter) → judge (LLM,
context requests) → renderer (9:16, captions) → review (Telegram + web) → publisher →
analytics loop. Then Kick/YouTube.

**Open items**: register a Twitch app (dev.twitch.tv/console → client ID/secret into `.env`
as `CLIPPER_TWITCH_CLIENT_ID/SECRET`), pick the real channels (`clipper-channels add ...`).
FFZ emote API was unreachable on 2026-11-18; its parser follows the documented format but is
unverified. chat-ingest is a single instance for now (use per-channel leases to shard later).
Recorder: ad detection follows streamlink's rules but hasn't been seen live yet (no ad during
the test); fMP4/HEVC renditions are not supported (we request H.264, which Twitch serves as TS);
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
