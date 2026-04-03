import asyncio
import json
import logging
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

    codec = info["video_codec"]
    info["needs_conversion"] = codec in PROBLEMATIC_CODECS or codec != "h264"
    logger.info("Probed %s: video=%s audio=%s", input_path.name, codec, info.get("audio_codec", "none"))
    return info


async def convert_video(input_path: Path, output_path: Path) -> None:
    cmd = [
        "ffmpeg",
        "-i", str(input_path),
        "-c:v", "libx264",
        "-crf", "22",
        "-preset", "medium",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        "-y",
        str(output_path),
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
    except asyncio.TimeoutError as exc:
        proc.kill()
        raise RuntimeError("Conversion timed out (>10 min). File may be too large or complex.") from exc

    if proc.returncode != 0:
        err = (stdout + stderr).decode(errors="replace").strip()[-500:]
        logger.error("FFmpeg exit %d: %s", proc.returncode, err)
        raise RuntimeError(f"FFmpeg failed: {err}" if err else "FFmpeg failed with no output. Codec may not be supported.")
