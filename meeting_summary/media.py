"""Media handling: ffmpeg normalization, chunk splitting, and local caching.

Provider-agnostic. Produces a normalized mono 16 kHz MP3 plus (optionally)
time-aligned chunks that speech-to-text providers consume.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from common import MeetingSummaryError, log

# Bump when the normalization/chunking scheme changes so stale caches are not
# reused across incompatible encodings.
MEDIA_CACHE_VERSION = "v1-mono16k-64k"


@dataclass(frozen=True)
class AudioChunk:
    path: Path
    index: int
    total: int
    start_seconds: float
    duration_seconds: float

    @property
    def end_seconds(self) -> float:
        return self.start_seconds + self.duration_seconds


def require_media_tools() -> None:
    missing = [tool for tool in ("ffmpeg", "ffprobe") if shutil.which(tool) is None]
    if missing:
        raise MeetingSummaryError(
            "Missing required media tool(s): "
            + ", ".join(missing)
            + ". Install ffmpeg and ensure it is on PATH."
        )


def run_tool(args: Sequence[str], label: str) -> str:
    log(f"Running {label}...")
    try:
        completed = subprocess.run(
            list(args),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise MeetingSummaryError(f"{label} was not found on PATH.") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        if not detail:
            detail = f"exit code {exc.returncode}"
        raise MeetingSummaryError(f"{label} failed: {detail}") from exc
    return completed.stdout.strip()


def format_bytes(size: int) -> str:
    units = ("B", "KB", "MB", "GB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def format_time(seconds: float) -> str:
    total_seconds = max(0, int(round(seconds)))
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    secs = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def get_duration_seconds(path: Path) -> float:
    output = run_tool(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        "ffprobe duration check",
    )
    try:
        duration = float(output.splitlines()[-1])
    except (IndexError, ValueError) as exc:
        raise MeetingSummaryError(
            f"Could not read media duration for {path}."
        ) from exc
    if duration <= 0:
        raise MeetingSummaryError(f"Media duration must be positive: {path}")
    return duration


def normalize_to_mp3(input_path: Path, work_dir: Path) -> Path:
    output_path = work_dir / "normalized.mp3"
    if output_path.exists() and output_path.stat().st_size > 0:
        log(
            "Reusing cached normalized MP3: "
            f"{output_path} ({format_bytes(output_path.stat().st_size)})"
        )
        return output_path

    log("Converting source media to mono 16 kHz / 64 kbps MP3...")
    run_tool(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(input_path),
            "-vn",
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-b:a",
            "64k",
            str(output_path),
        ],
        "ffmpeg audio normalization",
    )
    if not output_path.exists():
        raise MeetingSummaryError("ffmpeg did not create the normalized MP3.")
    log(
        "Normalized MP3 created: "
        f"{output_path} ({format_bytes(output_path.stat().st_size)})"
    )
    return output_path


def split_audio(normalized_path: Path, chunk_minutes: float, work_dir: Path) -> list[Path]:
    chunk_seconds = int(round(chunk_minutes * 60))
    if chunk_seconds <= 0:
        raise MeetingSummaryError("--chunk-minutes must be greater than 0.")

    duration = get_duration_seconds(normalized_path)
    log(
        "Normalized audio duration: "
        f"{format_time(duration)}; chunk size: {format_time(chunk_seconds)}"
    )
    if duration <= chunk_seconds + 0.5:
        log("Audio fits in one chunk; using normalized MP3 directly.")
        return [normalized_path]

    chunks_dir = work_dir / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    cached_chunks = sorted(chunks_dir.glob("chunk_*.mp3"))
    if cached_chunks and chunk_cache_is_valid(cached_chunks, duration):
        log(
            f"Reusing {len(cached_chunks)} cached audio chunk(s) from {chunks_dir}."
        )
        return cached_chunks

    for stale_chunk in cached_chunks:
        stale_chunk.unlink()

    pattern = chunks_dir / "chunk_%04d.mp3"
    log(f"Splitting audio into {chunk_minutes:g}-minute chunks...")
    run_tool(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(normalized_path),
            "-f",
            "segment",
            "-segment_time",
            str(chunk_seconds),
            "-reset_timestamps",
            "1",
            "-c",
            "copy",
            str(pattern),
        ],
        "ffmpeg audio chunking",
    )

    chunks = sorted(chunks_dir.glob("chunk_*.mp3"))
    if not chunks:
        raise MeetingSummaryError("ffmpeg did not create audio chunks.")
    log(f"Created {len(chunks)} audio chunk(s) in {chunks_dir}.")
    return chunks


def chunk_cache_is_valid(chunks: list[Path], source_duration: float) -> bool:
    try:
        total_duration = sum(get_duration_seconds(chunk) for chunk in chunks)
    except MeetingSummaryError:
        return False
    valid = total_duration >= source_duration - 1.0
    if not valid:
        log(
            "Cached chunks are incomplete; "
            f"found {format_time(total_duration)} for source {format_time(source_duration)}."
        )
    return valid


def build_chunk_metadata(paths: list[Path]) -> list[AudioChunk]:
    chunks: list[AudioChunk] = []
    offset = 0.0
    total = len(paths)
    for index, path in enumerate(paths, start=1):
        duration = get_duration_seconds(path)
        log(
            f"Chunk {index}/{total}: {path.name}, "
            f"{format_time(offset)}-{format_time(offset + duration)}, "
            f"{format_bytes(path.stat().st_size)}"
        )
        chunks.append(
            AudioChunk(
                path=path,
                index=index,
                total=total,
                start_seconds=offset,
                duration_seconds=duration,
            )
        )
        offset += duration
    return chunks


def media_cache_dir(input_file: Path, chunk_minutes: float) -> Path:
    stat = input_file.stat()
    chunk_seconds = int(round(chunk_minutes * 60))
    cache_source = "|".join(
        [
            MEDIA_CACHE_VERSION,
            str(input_file).casefold(),
            str(stat.st_size),
            str(stat.st_mtime_ns),
            str(chunk_seconds),
        ]
    )
    digest = hashlib.sha1(cache_source.encode("utf-8")).hexdigest()[:12]
    return (
        input_file.parent
        / ".meeting_summary_cache"
        / f"{sanitize_filename(input_file.stem)}_{digest}"
    )


def sanitize_filename(stem: str) -> str:
    invalid = '<>:"/\\|?*'
    sanitized = "".join("_" if char in invalid else char for char in stem)
    sanitized = sanitized.strip(" .")
    return sanitized or "meeting"
