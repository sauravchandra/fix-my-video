import asyncio
import json
import logging
import os
import resource
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger("fixmyvideo")

PROBLEMATIC_CODECS = {
    "vp9", "vp8", "hevc", "h265", "av1", "theora",
    "mpeg4", "msmpeg4v3", "wmv3", "vc1", "rawvideo",
}


async def probe_video(input_path: Path) -> dict:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams", "-show_format",
        str(input_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError as exc:
        proc.kill()
        raise RuntimeError("Could not analyze video file (timed out).") from exc

    if proc.returncode != 0:
        raise RuntimeError("File is not a valid video or is corrupted.")

    data = json.loads(stdout)
    info: dict = {}

    for stream in data.get("streams", []):
        codec_type = stream.get("codec_type")
        if codec_type == "video" and "video_codec" not in info:
            info["video_codec"] = stream.get("codec_name", "unknown")
            w, h = stream.get("width", 0), stream.get("height", 0)
            if w and h:
                info["resolution"] = f"{w}×{h}"
        elif codec_type == "audio" and "audio_codec" not in info:
            info["audio_codec"] = stream.get("codec_name", "unknown")

    if "video_codec" not in info:
        raise RuntimeError("No video stream found in file.")

    dur = data.get("format", {}).get("duration")
    if dur:
        try:
            info["duration"] = float(dur)
        except (ValueError, TypeError):
            pass

    codec = info["video_codec"]
    info["needs_conversion"] = codec in PROBLEMATIC_CODECS or codec != "h264"
    logger.info("Probed %s: video=%s audio=%s dur=%s", input_path.name, codec, info.get("audio_codec", "none"), info.get("duration"))
    return info


ProgressCallback = Callable[[float], None]


async def convert_video(
    input_path: Path,
    output_path: Path,
    duration: float = 0,
    on_progress: ProgressCallback | None = None,
) -> None:
    threads = os.environ.get("FFMPEG_THREADS", "4")
    cmd = [
        "ffmpeg",
        "-i", str(input_path),
        "-c:v", "libx264",
        "-crf", "23",
        "-preset", "fast",
        "-pix_fmt", "yuv420p",
        "-threads", threads,
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        "-progress", "pipe:1",
        "-y",
        str(output_path),
    ]

    logger.info("FFmpeg starting: input=%s duration=%.1fs", input_path.name, duration)
    duration_us = duration * 1_000_000

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def _read_progress():
        assert proc.stdout
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode(errors="replace").strip()
            if text.startswith("out_time_us=") and duration_us > 0 and on_progress:
                try:
                    us = int(text.split("=", 1)[1])
                    pct = min(99.0, round(us / duration_us * 100, 1))
                    on_progress(pct)
                except (ValueError, ZeroDivisionError):
                    pass

    try:
        stderr_data = b""

        async def _read_stderr():
            nonlocal stderr_data
            assert proc.stderr
            stderr_data = await proc.stderr.read()

        await asyncio.wait_for(
            asyncio.gather(_read_progress(), _read_stderr()),
            timeout=600,
        )
        await proc.wait()
    except asyncio.TimeoutError as exc:
        proc.kill()
        raise RuntimeError("Conversion timed out (>10 min). File may be too large or complex.") from exc

    rss_after = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    logger.info("FFmpeg pid=%s exit=%s rss_peak=%sKB", proc.pid, proc.returncode, rss_after)

    if proc.returncode != 0:
        err = stderr_data.decode(errors="replace")
        logger.error("FFmpeg stderr:\n%s", err[-2000:])
        snippet = err.strip()[-300:]
        raise RuntimeError(f"FFmpeg failed (exit {proc.returncode}): {snippet}" if snippet else "FFmpeg crashed with no output.")
