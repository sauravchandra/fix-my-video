# FixMyVideo

Upload a video that won't play. Get back an MP4 that works everywhere — iPhone, Android, Mac, Windows, Chrome, Safari, WhatsApp, Instagram.

Re-encodes to H.264 + AAC with `movflags +faststart` using FFmpeg.

## Architecture

```
Browser ──POST /api/upload──▶ FastAPI ──stream-to-disk──▶ /tmp/fixmyvideo/{job_id}/
                                  │
                            asyncio.Queue
                                  │
                        ┌─────────┴─────────┐
                     Worker 0            Worker 1
                        │                    │
                    ffprobe              ffprobe
                    ffmpeg               ffmpeg
                        │                    │
Browser ◀──poll /api/status──────────────── done
                                  │
Browser ──GET /api/download──▶ FileResponse
```

Uploads stream to disk (never held in memory). Workers pull from an in-memory queue — no Redis, no database. Jobs don't need to outlive the request cycle.

## Run locally

Requires Python 3.11+ and FFmpeg.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

## Docker

```bash
docker build -t fixmyvideo .
docker run -p 8000:8000 fixmyvideo
```

## Deploy

Dockerfile works on any container platform. Push to GitHub, connect, deploy.

Works out of the box on [Railway](https://railway.app), [Render](https://render.com), [Fly.io](https://fly.io) — all have free tiers, auto-detect the Dockerfile, and provide TLS.

## Configuration

All optional. Defaults work without setting anything.

| Variable | Default | What it does |
|---|---|---|
| `MAX_WORKERS` | `2` | Concurrent FFmpeg processes |
| `MAX_QUEUE` | `20` | Pending jobs before 503 |
| `MAX_PER_IP` | `3` | Concurrent jobs per IP |
| `MAX_FILE_SIZE_MB` | `500` | Upload limit |
| `CLEANUP_MINUTES` | `30` | Time before job files are deleted |
| `DISK_MIN_FREE_MB` | `200` | Minimum free disk to accept uploads |

## API

| Endpoint | Description |
|---|---|
| `GET /` | Frontend |
| `GET /health` | `{"status":"ok", "queued":N, "processing":N, "workers":N}` |
| `POST /api/upload` | Multipart file → `{"job_id":"..."}` |
| `GET /api/status/{job_id}` | `queued` (with position) → `processing` → `done` or `error` |
| `GET /api/download/{job_id}` | Converted MP4 |

## FFmpeg commands

Probe:
```
ffprobe -v quiet -print_format json -show_streams -show_format input
```

Convert:
```
ffmpeg -i input -c:v libx264 -crf 22 -preset medium -pix_fmt yuv420p \
  -c:a aac -b:a 128k -movflags +faststart -y output.mp4
```

Handles VP9, VP8, HEVC, AV1, Theora, WMV, and anything else that isn't already H.264.
