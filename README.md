# Clipper

Finds great moments in Twitch streams from chat activity, then records, transcribes, judges,
edits and publishes short vertical clips. Full design and roadmap: [DESIGN.md](DESIGN.md).

## Quick start

Needs Docker Desktop (with NVIDIA GPU support for the transcriber/renderer later) and
[uv](https://docs.astral.sh/uv/).

```sh
cp .env.example .env                 # optional: every value has a dev default
docker compose up -d                 # Postgres, SeaweedFS (S3), migrate (one-off)
docker compose logs migrate          # should end with "bucket clipper ready"

uv sync --all-packages               # local dev environment
uv run pytest                        # tests (integration tests use the compose Postgres)
```

| What | Where |
|---|---|
| Postgres | `127.0.0.1:5433`, db/user/password `clipper` |
| S3 API (SeaweedFS) | `http://127.0.0.1:8333`, bucket `clipper`, keys from `.env` |

## Repository

```
libs/core/      shared contract: settings, models, timeline, job queue, storage
db/             migrations + `migrate` container
services/       one app + Docker image per service (added step by step)
compose.yaml    the whole stack
DESIGN.md       idea, workflow, decisions, roadmap
```
