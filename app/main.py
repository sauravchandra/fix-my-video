import os
import uuid
import time
import shutil
import asyncio
import logging
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware

from app.converter import probe_video, convert_video

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "/tmp/fixmyvideo"))
MAX_FILE_SIZE = int(os.environ.get("MAX_FILE_SIZE_MB", "500")) * 1024 * 1024
CLEANUP_MINUTES = int(os.environ.get("CLEANUP_MINUTES", "30"))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "2"))
MAX_QUEUE = int(os.environ.get("MAX_QUEUE", "20"))
MAX_PER_IP = int(os.environ.get("MAX_PER_IP", "3"))
DISK_MIN_MB = int(os.environ.get("DISK_MIN_FREE_MB", "200"))
APP_VERSION = str(int(time.time()))

ALLOWED_EXTENSIONS = {
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".mpeg", ".mpg",
    ".wmv", ".flv", ".3gp", ".m4v", ".ts", ".mts", ".ogv",
}

BASE_DIR = Path(__file__).resolve().parent.parent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("fixmyvideo")

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

jobs: dict[str, dict] = {}
queue: asyncio.Queue[tuple[str, Path, Path]]
workers: list[asyncio.Task] = []

# ---------------------------------------------------------------------------
# Middleware — security headers + cache control (rate limiting removed)
# ---------------------------------------------------------------------------

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


class ProductionMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        for k, v in _SECURITY_HEADERS.items():
            response.headers[k] = v
        if request.headers.get("x-forwarded-proto") == "https":
            response.headers["Strict-Transport-Security"] = (
                "max-age=63072000; includeSubDomains"
            )
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "public, max-age=86400"
        return response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _sanitize(filename: str) -> str:
    name = Path(filename).name
    safe = "".join(c for c in name if c.isalnum() or c in ".-_")
    return safe[:200] or "video"


def _valid_job_id(jid: str) -> bool:
    return len(jid) == 32 and all(c in "0123456789abcdef" for c in jid)


def _disk_ok() -> bool:
    try:
        s = os.statvfs(UPLOAD_DIR)
        return (s.f_bavail * s.f_frsize) / (1024 * 1024) > DISK_MIN_MB
    except OSError:
        return True


def _cleanup_files():
    cutoff = time.time() - CLEANUP_MINUTES * 60
    removed = 0
    for item in UPLOAD_DIR.iterdir():
        if item.is_dir():
            try:
                if item.stat().st_mtime < cutoff:
                    shutil.rmtree(item, ignore_errors=True)
                    removed += 1
            except OSError:
                pass
    if removed:
        logger.info("Cleanup: removed %d expired job(s)", removed)


async def _periodic():
    while True:
        await asyncio.sleep(300)
        _cleanup_files()
        cutoff = time.time() - CLEANUP_MINUTES * 60
        for k in [k for k, v in jobs.items() if v.get("created_at", 0) < cutoff]:
            jobs.pop(k, None)


# ---------------------------------------------------------------------------
# Queue workers
# ---------------------------------------------------------------------------

async def _worker(n: int):
    logger.info("Worker %d ready", n)
    while True:
        job_id, in_path, out_path = await queue.get()
        try:
            await _process(job_id, in_path, out_path)
        finally:
            queue.task_done()


async def _process(job_id: str, input_path: Path, output_path: Path):
    job = jobs.get(job_id)
    if not job:
        return
    job["status"] = "processing"
    try:
        probe = await probe_video(input_path)
        job["video_codec"] = probe.get("video_codec", "unknown")
        job["audio_codec"] = probe.get("audio_codec", "none")
        job["resolution"] = probe.get("resolution", "")

        start = time.time()
        await convert_video(
            input_path, output_path,
            duration=probe.get("duration", 0),
            on_progress=lambda pct: job.update(progress=pct),
        )
        elapsed = round(time.time() - start, 1)

        job.update(status="done", conversion_time=elapsed, output_size=output_path.stat().st_size)
        logger.info("Job %s done in %ss (%s → h264)", job_id[:8], elapsed, job.get("video_codec"))
    except asyncio.CancelledError:
        job.update(status="error", error="Processing interrupted by server restart. Please retry.")
        raise
    except Exception as e:
        job.update(status="error", error=str(e)[:300])
        logger.error("Job %s failed: %s", job_id[:8], e)
    finally:
        input_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(_app: FastAPI):
    global queue
    queue = asyncio.Queue(maxsize=MAX_QUEUE)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    _cleanup_files()

    rc = await _exec("ffmpeg", "-version")
    if rc != 0:
        logger.error("ffmpeg not found — conversion will fail")
    rc = await _exec("ffprobe", "-version")
    if rc != 0:
        logger.error("ffprobe not found — probe will fail")

    await _log_exec("df", "-h", "/tmp")

    for i in range(MAX_WORKERS):
        workers.append(asyncio.create_task(_worker(i)))

    bg = asyncio.create_task(_periodic())
    logger.info("Started (workers=%d, queue_depth=%d, cleanup=%dm)", MAX_WORKERS, MAX_QUEUE, CLEANUP_MINUTES)
    yield

    # Graceful shutdown: stop workers, drain queue
    bg.cancel()
    for w in workers:
        w.cancel()
    await asyncio.gather(*workers, return_exceptions=True)

    while not queue.empty():
        try:
            job_id, in_path, _ = queue.get_nowait()
            job = jobs.get(job_id)
            if job:
                job.update(status="error", error="Server restarting. Please retry.")
            in_path.unlink(missing_ok=True)
        except asyncio.QueueEmpty:
            break

    logger.info("Stopped (%d worker(s) drained)", len(workers))
    workers.clear()


async def _exec(*cmd: str) -> int:
    p = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await p.wait()
    return p.returncode


async def _log_exec(*cmd: str):
    p = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await p.communicate()
    text = (out + err).decode(errors="replace")
    for line in text.splitlines():
        if any(k in line.lower() for k in ("h264", "x264", "libx264", "vp9", "libvpx", "aac", "/tmp", "/dev")):
            logger.info("[diag] %s", line.strip())


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="FixMyVideo", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=500)
app.add_middleware(ProductionMiddleware)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    pending = sum(1 for j in jobs.values() if j["status"] == "queued")
    active = sum(1 for j in jobs.values() if j["status"] == "processing")
    return {"status": "ok", "queued": pending, "processing": active, "workers": MAX_WORKERS}


@app.get("/robots.txt", response_class=PlainTextResponse)
async def robots():
    return "User-agent: *\nDisallow: /api/\n"


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"version": APP_VERSION})


@app.post("/api/upload")
async def upload_video(request: Request, file: UploadFile = File(...)):
    ct = file.content_type or ""
    ext = Path(file.filename or "").suffix.lower()
    if not (ct.startswith("video/") or ct == "application/octet-stream" or ext in ALLOWED_EXTENSIONS):
        raise HTTPException(400, detail="Only video files are accepted.")
    if not _disk_ok():
        raise HTTPException(503, detail="Server storage is temporarily full. Try again later.")

    ip = _client_ip(request)
    ip_active = sum(1 for j in jobs.values() if j.get("ip") == ip and j["status"] in ("queued", "processing"))
    if ip_active >= MAX_PER_IP:
        raise HTTPException(
            429,
            detail=f"You already have {ip_active} active jobs. Wait for one to finish.",
        )

    job_id = uuid.uuid4().hex
    job_dir = UPLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    input_path = job_dir / f"in_{_sanitize(file.filename or 'video')}"
    output_path = job_dir / "output.mp4"

    total = 0
    try:
        with open(input_path, "wb") as f:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_FILE_SIZE:
                    raise HTTPException(413, detail="File exceeds the 500 MB limit.")
                f.write(chunk)
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(500, detail="Upload failed.")

    if total == 0:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(400, detail="Empty file.")

    original_name = Path(file.filename or "video").stem
    jobs[job_id] = {"status": "queued", "created_at": time.time(), "input_size": total, "ip": ip, "name": original_name}

    try:
        queue.put_nowait((job_id, input_path, output_path))
    except asyncio.QueueFull:
        shutil.rmtree(job_dir, ignore_errors=True)
        jobs.pop(job_id, None)
        raise HTTPException(503, detail="Server is busy. Please try again in a moment.")

    logger.info("Job %s queued (%s, %d bytes)", job_id[:8], ext or ct, total)
    return {"job_id": job_id}


@app.get("/api/status/{job_id}")
async def job_status(job_id: str):
    if not _valid_job_id(job_id):
        raise HTTPException(400, detail="Invalid job ID.")
    if job_id not in jobs:
        raise HTTPException(404, detail="Job not found or expired.")
    job = jobs[job_id]
    result = {k: v for k, v in job.items() if k not in ("ip", "created_at", "input_size", "name")}
    if job["status"] == "queued":
        result["position"] = sum(
            1 for j in jobs.values()
            if j["status"] == "queued" and j["created_at"] < job["created_at"]
        )
    return result


@app.get("/api/download/{job_id}")
async def download_video(job_id: str):
    if not _valid_job_id(job_id):
        raise HTTPException(400, detail="Invalid job ID.")
    path = UPLOAD_DIR / job_id / "output.mp4"
    if not path.exists():
        raise HTTPException(404, detail="File not found or expired.")
    job = jobs.get(job_id)
    name = _sanitize(job["name"]) if job and job.get("name") else "video"
    return FileResponse(path, media_type="video/mp4", filename=f"{name}_fixed.mp4")
