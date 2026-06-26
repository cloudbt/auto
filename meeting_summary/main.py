from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence, TypeVar


DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_FALLBACK_MODEL = "gemini-3.5-flash"
SCRIPT_DIR = Path(__file__).resolve().parent
LOG_FILE = SCRIPT_DIR / "meeting_summary.log"
DEFAULT_CHUNK_MINUTES = 25.0
DEFAULT_MAX_HOURS = 2.0
DEFAULT_MAX_OUTPUT_TOKENS = 65536
GENERATION_TEMPERATURE = 0.3
REQUEST_TIMEOUT_MS = 180_000
MEDIA_CACHE_VERSION = "v1-mono16k-64k"
DEFAULT_COPY_OUTPUT_DIR = Path(
    r"C:\Users\whz\iCloudDrive\iCloud~md~obsidian\work\work\MeetingSummary"
)
DEFAULT_PUBLISH_GITHUB_REPO = "https://github.com/cloudbt/dev.git"
DEFAULT_PUBLISH_GITHUB_BRANCH = "main"
DEFAULT_PUBLISH_GITHUB_DIR = "meeting"

T = TypeVar("T")


class MeetingSummaryError(Exception):
    """Raised for expected user-facing failures."""


class EmptyResponseError(Exception):
    """Gemini returned no usable text (e.g. finishReason=MALFORMED_RESPONSE)."""


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Transcribe and summarize meeting audio/video files with Gemini."
        )
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("input_file", type=Path, help="Audio or video file path.")
    common.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Markdown output path. Defaults to meeting_summary/output/.",
    )
    common.add_argument(
        "--model",
        help=f"Gemini model name. Defaults to GEMINI_MODEL or {DEFAULT_MODEL}.",
    )
    common.add_argument(
        "--max-hours",
        type=float,
        default=DEFAULT_MAX_HOURS,
        help="Reject inputs longer than this many hours. Default: 2.",
    )
    common.add_argument(
        "--chunk-minutes",
        type=float,
        default=DEFAULT_CHUNK_MINUTES,
        help=f"Audio chunk size in minutes. Default: {DEFAULT_CHUNK_MINUTES:g}.",
    )
    common.add_argument(
        "--max-output-tokens",
        type=int,
        default=DEFAULT_MAX_OUTPUT_TOKENS,
        help="Gemini max output tokens per request. Default: 65536.",
    )
    common.add_argument(
        "--retries",
        type=int,
        default=3,
        help="Retry count for upload/generation calls. Default: 3.",
    )
    common.add_argument(
        "--no-cache",
        action="store_true",
        help="Do not reuse the local MP3/chunk cache next to the input file.",
    )
    common.add_argument(
        "--copy-to",
        type=Path,
        default=DEFAULT_COPY_OUTPUT_DIR,
        help=(
            "Also copy the final Markdown file to this directory. "
            f"Default: {DEFAULT_COPY_OUTPUT_DIR}"
        ),
    )
    common.add_argument(
        "--no-copy",
        action="store_true",
        help="Do not copy the final Markdown file to the Obsidian/iCloud folder.",
    )
    common.add_argument(
        "--publish-github-repo",
        default=DEFAULT_PUBLISH_GITHUB_REPO,
        help=(
            "Git repository to push the final Markdown file to. "
            f"Default: {DEFAULT_PUBLISH_GITHUB_REPO}"
        ),
    )
    common.add_argument(
        "--publish-github-branch",
        default=DEFAULT_PUBLISH_GITHUB_BRANCH,
        help=(
            "Branch to push the final Markdown file to. "
            f"Default: {DEFAULT_PUBLISH_GITHUB_BRANCH}"
        ),
    )
    common.add_argument(
        "--publish-github-dir",
        default=DEFAULT_PUBLISH_GITHUB_DIR,
        help=(
            "Directory in the publish repository for the final Markdown file. "
            f"Default: {DEFAULT_PUBLISH_GITHUB_DIR}"
        ),
    )
    common.add_argument(
        "--publish-checkout",
        type=Path,
        help="Local checkout path used for GitHub publishing.",
    )
    common.add_argument(
        "--no-publish",
        action="store_true",
        help="Do not push the final Markdown file to GitHub.",
    )

    subparsers.add_parser(
        "transcript",
        parents=[common],
        help="Output only a timestamped transcript.",
    )
    subparsers.add_parser(
        "meeting",
        parents=[common],
        help="Output summary, action items, and transcript.",
    )
    subparsers.add_parser(
        "compact",
        parents=[common],
        help="Output only summary and action items.",
    )
    return parser


def load_local_env() -> None:
    env_path = SCRIPT_DIR / ".env"
    if not env_path.exists():
        return

    try:
        from dotenv import load_dotenv
    except ImportError:
        load_dotenv = None

    if load_dotenv is not None:
        load_dotenv(env_path, override=False)
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def resolve_api_key() -> str:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise MeetingSummaryError(
            "GEMINI_API_KEY is not set. Create meeting_summary/.env from "
            ".env.example or set the environment variable."
        )
    return api_key


def resolve_model(cli_model: str | None) -> str:
    return (
        cli_model
        or os.environ.get("GEMINI_MODEL", "").strip()
        or DEFAULT_MODEL
    )


def resolve_fallback_model(model: str) -> str | None:
    fallback = (
        os.environ.get("GEMINI_FALLBACK_MODEL", "").strip()
        or DEFAULT_FALLBACK_MODEL
    )
    if not fallback or fallback == model:
        return None
    return fallback


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


def log(message: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] [pid {os.getpid()}] {message}"
    print(line, file=sys.stderr)
    # Also append to a persistent file so the full pipeline is inspectable in
    # real time (tail -f) and after the fact, success or failure. Best-effort:
    # never let a logging hiccup abort the job.
    try:
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def format_bytes(size: int) -> str:
    units = ("B", "KB", "MB", "GB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


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


def make_client(api_key: str):
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise MeetingSummaryError(
            "Missing Python dependency. Run: "
            "python -m pip install -r meeting_summary/requirements.txt"
        ) from exc
    log("Gemini client initialized.")
    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS),
    )
    return client, types


def generation_config(types, max_output_tokens: int):
    return types.GenerateContentConfig(
        temperature=GENERATION_TEMPERATURE,
        max_output_tokens=max_output_tokens,
    )


def retry(operation: Callable[[], T], description: str, attempts: int) -> T:
    attempts = max(1, attempts)
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            log(f"{description}: attempt {attempt}/{attempts}.")
            return operation()
        except Exception as exc:  # SDK exceptions vary by transport.
            last_error = exc
            if attempt == attempts:
                break
            delay = min(2 ** (attempt - 1), 8)
            log(
                f"{description} failed on attempt {attempt}/{attempts}; "
                f"retrying in {delay}s... ({exc})"
            )
            time.sleep(delay)
    raise MeetingSummaryError(
        f"{description} failed after {attempts} attempt(s): {last_error}"
    ) from last_error


def file_state_name(file_obj) -> str | None:
    state = getattr(file_obj, "state", None)
    if state is None:
        return None
    name = getattr(state, "name", None) or str(state)
    return name.rsplit(".", 1)[-1].upper()


def wait_until_active(client, file_obj, timeout_seconds: int = 300):
    deadline = time.monotonic() + timeout_seconds
    current = file_obj
    while True:
        state_name = file_state_name(current)
        if state_name in (None, "ACTIVE"):
            log(f"Uploaded file is ready: {getattr(current, 'name', 'unknown')}")
            return current
        if state_name in ("FAILED", "ERROR"):
            raise MeetingSummaryError(f"Uploaded file processing failed: {state_name}")
        if time.monotonic() >= deadline:
            raise MeetingSummaryError(
                f"Timed out waiting for uploaded file to become ACTIVE: {state_name}"
            )
        log(
            "Waiting for uploaded file processing: "
            f"{getattr(current, 'name', 'unknown')} state={state_name}"
        )
        time.sleep(5)
        current = client.files.get(name=current.name)


def finish_reason_label(response) -> str | None:
    candidates = getattr(response, "candidates", None)
    if not candidates:
        return None
    reason = getattr(candidates[0], "finish_reason", None)
    if reason is None:
        return None
    return getattr(reason, "name", None) or str(reason)


GOOD_FINISH_REASONS = {"STOP", "FINISH_REASON_UNSPECIFIED"}


def extract_response_text(response) -> str:
    reason = finish_reason_label(response)
    try:
        text = getattr(response, "text", None)
    except Exception:  # SDK may raise when the candidate carries no text parts.
        text = None
    # A non-STOP reason means the output was truncated/degenerate (e.g.
    # MAX_TOKENS repeat-loop) or malformed, even when some text came back.
    if reason and reason.upper() not in GOOD_FINISH_REASONS:
        raise EmptyResponseError(
            f"Gemini stopped with finishReason={reason} "
            "(truncated or degenerate output)."
        )
    if text and text.strip():
        return strip_outer_fence(text.strip())
    detail = f" (finishReason={reason})" if reason else ""
    raise EmptyResponseError(f"Gemini returned an empty response{detail}.")


def strip_outer_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```") or not stripped.endswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) >= 3:
        return "\n".join(lines[1:-1]).strip()
    return stripped


def should_failover_fast(exc: Exception) -> bool:
    """Transient capacity / hang errors where retrying the same model is futile,
    so we switch to the fallback model immediately instead of burning retries."""
    text = str(exc).upper()
    markers = (
        "503",
        "UNAVAILABLE",
        "429",
        "RESOURCE_EXHAUSTED",
        "OVERLOADED",
        "TIMEOUT",
        "DEADLINE",
    )
    return any(marker in text for marker in markers)


def generate_text(client, model: str, contents, config, retries: int) -> str:
    models_to_try = [model]
    fallback = resolve_fallback_model(model)
    if fallback is not None:
        models_to_try.append(fallback)
    attempts = max(1, retries)

    last_error: Exception | None = None
    for index, current_model in enumerate(models_to_try):
        has_next_model = index < len(models_to_try) - 1
        if index > 0:
            log(
                f"Falling back to model {current_model} after "
                f"model {models_to_try[index - 1]} failed."
            )
        for attempt in range(1, attempts + 1):
            try:
                log(
                    f"Gemini content generation ({current_model}): "
                    f"attempt {attempt}/{attempts}."
                )
                response = client.models.generate_content(
                    model=current_model,
                    contents=contents,
                    config=config,
                )
                return extract_response_text(response)
            except Exception as exc:  # SDK exceptions vary by transport.
                last_error = exc
                # An overloaded/unresponsive model won't recover by retrying it;
                # jump straight to the fallback model if one is left.
                if has_next_model and should_failover_fast(exc):
                    log(
                        f"{current_model} is overloaded/unresponsive ({exc}); "
                        "switching to fallback model now."
                    )
                    break
                if attempt == attempts:
                    break
                delay = min(2 ** (attempt - 1), 8)
                log(
                    f"Gemini content generation ({current_model}) failed on "
                    f"attempt {attempt}/{attempts}; retrying in {delay}s... ({exc})"
                )
                time.sleep(delay)

    raise MeetingSummaryError(
        f"Gemini content generation failed for model(s) "
        f"{', '.join(models_to_try)}: {last_error}"
    )


def generate_from_file(
    client,
    types,
    model: str,
    chunk_path: Path,
    prompt: str,
    max_output_tokens: int,
    retries: int,
) -> str:
    uploaded = None
    try:
        log(
            f"Uploading chunk file: {chunk_path.name} "
            f"({format_bytes(chunk_path.stat().st_size)})"
        )
        uploaded = retry(
            lambda: client.files.upload(file=str(chunk_path)),
            "Gemini file upload",
            retries,
        )
        uploaded = wait_until_active(client, uploaded)
        config = generation_config(types, max_output_tokens)
        log(f"Generating content with model {model} from uploaded chunk...")
        text = generate_text(client, model, [prompt, uploaded], config, retries)
        log(f"Gemini returned {len(text):,} character(s) for chunk file.")
        return text
    finally:
        if uploaded is not None:
            try:
                client.files.delete(name=uploaded.name)
                log(f"Deleted uploaded Gemini file: {uploaded.name}")
            except Exception as exc:
                log(
                    f"Warning: could not delete uploaded file {uploaded.name}: {exc}",
                )


def generate_from_text(
    client,
    types,
    model: str,
    prompt: str,
    max_output_tokens: int,
    retries: int,
) -> str:
    config = generation_config(types, max_output_tokens)
    log(
        f"Generating content with model {model} from text prompt "
        f"({len(prompt):,} character(s))..."
    )
    text = generate_text(client, model, prompt, config, retries)
    log(f"Gemini returned {len(text):,} character(s) from text prompt.")
    return text


def transcript_prompt(chunk: AudioChunk) -> str:
    return f"""
You are transcribing a meeting audio chunk.

Context:
- The meeting is mainly Japanese, with occasional Chinese or Japanese/Chinese mixed speech.
- This chunk range in the original recording is {format_time(chunk.start_seconds)} to {format_time(chunk.end_seconds)}.

Output only Markdown transcript content.

Rules:
- Use timestamped paragraphs like: [HH:MM:SS] transcript text.
- Use absolute timestamps relative to the original recording. Do not reset timestamps to 00:00 for this chunk.
- Preserve the original spoken language. Do not translate.
- Segment naturally every 2-5 minutes or when the topic changes.
- Do not invent speaker names. Include a person name only if it is clearly spoken.
- If a short phrase is unclear, write [unclear] instead of guessing.
- Do not include a summary or action items.
""".strip()


def chunk_compact_prompt(chunk: AudioChunk) -> str:
    return f"""
You are extracting meeting notes from one audio chunk.

Context:
- The meeting is mainly Japanese, with occasional Chinese or Japanese/Chinese mixed speech.
- This chunk range in the original recording is {format_time(chunk.start_seconds)} to {format_time(chunk.end_seconds)}.

Output only Markdown with these exact sections:

### Chunk Summary

### Action Item Candidates
| Owner | Action | Due | Context/Timestamp |
|---|---|---|---|

Rules:
- Do not output a full transcript.
- Follow the dominant spoken language for summary/action wording.
- If Japanese and Chinese are genuinely mixed, keep mixed wording naturally.
- Use absolute timestamps relative to the original recording.
- Use TBD for unknown owner or due date.
- Include only concrete action items. If there are none, keep the table header and add one row: | TBD | None identified | TBD | TBD |
""".strip()


def final_notes_prompt(source_label: str, source_text: str) -> str:
    return f"""
Create final meeting notes from the {source_label} below.

Output only Markdown with these exact sections:

## Summary

## Action Items
| Owner | Action | Due | Context/Timestamp |
|---|---|---|---|

Rules:
- Follow the meeting's dominant spoken language.
- If the meeting is genuinely mixed Japanese/Chinese, keep mixed wording naturally.
- Summary should be concise but include key topics, decisions, risks, and open questions.
- Action Items must be concrete tasks only.
- Use TBD for unknown owner or due date.
- Context/Timestamp should include the most relevant timestamp when available.
- If there are no action items, keep the table header and add one row: | TBD | None identified | TBD | TBD |

Source:
{source_text}
""".strip()


def generate_transcript(
    client,
    types,
    model: str,
    chunks: list[AudioChunk],
    max_output_tokens: int,
    retries: int,
    response_cache_dir: Path | None,
) -> str:
    parts: list[str] = []
    for chunk in chunks:
        log(
            f"Transcribing chunk {chunk.index}/{chunk.total} "
            f"({format_time(chunk.start_seconds)}-{format_time(chunk.end_seconds)})..."
        )
        prompt = transcript_prompt(chunk)
        cached = read_cached_response(
            response_cache_dir,
            model,
            "transcript",
            chunk,
            prompt,
        )
        if cached is not None:
            parts.append(cached)
            continue

        text = generate_from_file(
            client,
            types,
            model,
            chunk.path,
            prompt,
            max_output_tokens,
            retries,
        )
        write_cached_response(
            response_cache_dir,
            model,
            "transcript",
            chunk,
            prompt,
            text,
        )
        parts.append(
            text
        )
    transcript = "\n\n".join(part.strip() for part in parts if part.strip())
    log(f"Combined transcript length: {len(transcript):,} character(s).")
    return transcript


def generate_compact_notes(
    client,
    types,
    model: str,
    chunks: list[AudioChunk],
    max_output_tokens: int,
    retries: int,
    response_cache_dir: Path | None,
) -> str:
    chunk_notes: list[str] = []
    for chunk in chunks:
        log(
            f"Summarizing chunk {chunk.index}/{chunk.total} "
            f"({format_time(chunk.start_seconds)}-{format_time(chunk.end_seconds)})..."
        )
        prompt = chunk_compact_prompt(chunk)
        cached = read_cached_response(
            response_cache_dir,
            model,
            "compact_chunk",
            chunk,
            prompt,
        )
        if cached is not None:
            chunk_notes.append(cached)
            continue

        text = generate_from_file(
            client,
            types,
            model,
            chunk.path,
            prompt,
            max_output_tokens,
            retries,
        )
        write_cached_response(
            response_cache_dir,
            model,
            "compact_chunk",
            chunk,
            prompt,
            text,
        )
        chunk_notes.append(
            text
        )

    combined = "\n\n".join(
        f"### Chunk {index + 1}\n{note.strip()}"
        for index, note in enumerate(chunk_notes)
        if note.strip()
    )
    log(
        "Creating final compact meeting notes from "
        f"{len(combined):,} character(s) of chunk notes..."
    )
    return generate_from_text(
        client,
        types,
        model,
        final_notes_prompt("chunk notes", combined),
        max_output_tokens,
        retries,
    )


def build_markdown(
    mode: str,
    input_file: Path,
    model: str,
    duration_seconds: float,
    chunk_count: int,
    chunk_minutes: float,
    transcript: str | None,
    notes: str | None,
) -> str:
    title = {
        "transcript": "Meeting Transcript",
        "meeting": "Meeting Summary",
        "compact": "Compact Meeting Summary",
    }[mode]
    created = datetime.now().astimezone().isoformat(timespec="seconds")

    lines = [
        f"# {title}",
        "",
        "## Metadata",
        "",
        f"- Source: `{input_file}`",
        f"- Mode: `{mode}`",
        f"- Model: `{model}`",
        f"- Created: `{created}`",
        f"- Duration: `{format_time(duration_seconds)}`",
        f"- Chunks: `{chunk_count}`",
        f"- Chunk minutes: `{chunk_minutes:g}`",
        "",
    ]

    if mode == "transcript":
        lines.extend(["## Transcript", "", transcript or ""])
    elif mode == "meeting":
        lines.extend([notes or "", "", "## Transcript", "", transcript or ""])
    elif mode == "compact":
        lines.append(notes or "")
    else:
        raise MeetingSummaryError(f"Unsupported mode: {mode}")

    return "\n".join(lines).rstrip() + "\n"


def default_output_path(input_file: Path, mode: str) -> Path:
    output_dir = SCRIPT_DIR / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    stem = sanitize_filename(input_file.stem)
    candidate = output_dir / f"{stem}_{mode}_{timestamp}.md"
    if not candidate.exists():
        return candidate
    for counter in range(2, 1000):
        next_candidate = output_dir / f"{stem}_{mode}_{timestamp}_{counter}.md"
        if not next_candidate.exists():
            return next_candidate
    raise MeetingSummaryError("Could not create a unique output path.")


def copy_markdown_output(output_path: Path, copy_to_dir: Path | None) -> Path | None:
    if copy_to_dir is None:
        return None

    destination_dir = copy_to_dir.expanduser().resolve()
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination_path = destination_dir / output_path.name

    if output_path.resolve() == destination_path.resolve():
        log(f"Markdown copy skipped; destination is the primary output: {output_path}")
        return destination_path

    shutil.copy2(output_path, destination_path)
    log(
        "Markdown copied: "
        f"{destination_path} ({format_bytes(destination_path.stat().st_size)})"
    )
    return destination_path


def publish_markdown_to_git(
    output_path: Path,
    repo_url: str,
    branch: str,
    publish_dir: str,
    checkout_path: Path | None,
) -> Path:
    repo_url = repo_url.strip()
    branch = branch.strip()
    if not repo_url:
        raise MeetingSummaryError("--publish-github-repo must not be empty.")
    if not branch:
        raise MeetingSummaryError("--publish-github-branch must not be empty.")

    relative_publish_dir = validate_publish_dir(publish_dir)
    checkout = resolve_publish_checkout(repo_url, checkout_path)
    ensure_publish_checkout(repo_url, branch, checkout)

    destination_dir = checkout / relative_publish_dir
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination_path = destination_dir / output_path.name
    shutil.copy2(output_path, destination_path)
    log(
        "Markdown staged for GitHub publish: "
        f"{destination_path} ({format_bytes(destination_path.stat().st_size)})"
    )

    relative_destination = destination_path.relative_to(checkout).as_posix()
    status = run_git(["status", "--short", "--", relative_destination], checkout, "status")
    if not status.strip():
        log(f"GitHub publish skipped; no changes for {relative_destination}.")
        return destination_path

    run_git(["add", "--", relative_destination], checkout, "add")
    run_git(
        ["commit", "-m", f"Add meeting summary {output_path.name}"],
        checkout,
        "commit",
    )
    try:
        run_git(["push", "origin", branch], checkout, "push")
    except MeetingSummaryError:
        log("GitHub push failed; pulling with rebase once before retrying.")
        run_git(["pull", "--rebase", "origin", branch], checkout, "pull --rebase")
        run_git(["push", "origin", branch], checkout, "push")

    log(f"Markdown pushed to {repo_url} {branch}:{relative_destination}")
    return destination_path


def validate_publish_dir(publish_dir: str) -> Path:
    value = publish_dir.strip()
    if not value:
        raise MeetingSummaryError("--publish-github-dir must not be empty.")

    path = Path(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise MeetingSummaryError(
            "--publish-github-dir must be a relative path inside the publish repo."
        )
    return path


def resolve_publish_checkout(repo_url: str, checkout_path: Path | None) -> Path:
    if checkout_path is not None:
        return checkout_path.expanduser().resolve()

    base_dir = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base_dir:
        cache_dir = Path(base_dir)
    else:
        cache_dir = Path.home() / ".cache"

    repo_slug = sanitize_filename(repo_url.removesuffix(".git"))
    return (cache_dir / "meeting_summary" / "publish" / repo_slug).resolve()


def ensure_publish_checkout(repo_url: str, branch: str, checkout: Path) -> None:
    if shutil.which("git") is None:
        raise MeetingSummaryError("git was not found on PATH.")

    git_dir = checkout / ".git"
    if git_dir.exists():
        current_origin = run_git(["remote", "get-url", "origin"], checkout, "remote check")
        if current_origin.strip() != repo_url:
            raise MeetingSummaryError(
                f"Publish checkout {checkout} uses origin {current_origin}, "
                f"expected {repo_url}."
            )
        run_git(["fetch", "origin", branch], checkout, "fetch")
        run_git(["checkout", branch], checkout, "checkout")
        run_git(["pull", "--ff-only", "origin", branch], checkout, "pull")
        return

    if checkout.exists() and any(checkout.iterdir()):
        raise MeetingSummaryError(
            f"Publish checkout path exists but is not a git checkout: {checkout}"
        )

    checkout.parent.mkdir(parents=True, exist_ok=True)
    run_git(
        [
            "clone",
            "--branch",
            branch,
            "--single-branch",
            repo_url,
            str(checkout),
        ],
        None,
        "clone",
    )


def run_git(args: Sequence[str], cwd: Path | None, label: str) -> str:
    log(f"Running git {label}...")
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd is not None else None,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise MeetingSummaryError("git was not found on PATH.") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        if not detail:
            detail = f"exit code {exc.returncode}"
        raise MeetingSummaryError(f"git {label} failed: {detail}") from exc
    return completed.stdout.strip()


def read_cached_response(
    cache_dir: Path | None,
    model: str,
    label: str,
    chunk: AudioChunk,
    prompt: str,
) -> str | None:
    if cache_dir is None:
        return None
    path = response_cache_path(cache_dir, model, label, chunk, prompt)
    if not path.exists() or path.stat().st_size == 0:
        return None
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return None
    log(
        f"Reusing cached Gemini response for {label} chunk "
        f"{chunk.index}/{chunk.total}: {path}"
    )
    return text


def write_cached_response(
    cache_dir: Path | None,
    model: str,
    label: str,
    chunk: AudioChunk,
    prompt: str,
    text: str,
) -> None:
    if cache_dir is None:
        return
    path = response_cache_path(cache_dir, model, label, chunk, prompt)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.strip() + "\n", encoding="utf-8")
    log(f"Cached Gemini response: {path}")


def response_cache_path(
    cache_dir: Path,
    model: str,
    label: str,
    chunk: AudioChunk,
    prompt: str,
) -> Path:
    cache_source = "|".join(
        [
            MEDIA_CACHE_VERSION,
            model,
            label,
            chunk.path.name,
            str(chunk.path.stat().st_size),
            format_time(chunk.start_seconds),
            format_time(chunk.end_seconds),
            prompt,
        ]
    )
    digest = hashlib.sha1(cache_source.encode("utf-8")).hexdigest()[:12]
    filename = f"{label}_chunk_{chunk.index:04d}_{digest}.md"
    return cache_dir / "gemini_responses" / filename


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
    return input_file.parent / ".meeting_summary_cache" / f"{sanitize_filename(input_file.stem)}_{digest}"


def sanitize_filename(stem: str) -> str:
    invalid = '<>:"/\\|?*'
    sanitized = "".join("_" if char in invalid else char for char in stem)
    sanitized = sanitized.strip(" .")
    return sanitized or "meeting"


def format_time(seconds: float) -> str:
    total_seconds = max(0, int(round(seconds)))
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    secs = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def validate_args(args: argparse.Namespace) -> Path:
    input_file = args.input_file.expanduser().resolve()
    if not input_file.exists():
        raise MeetingSummaryError(f"Input file does not exist: {input_file}")
    if not input_file.is_file():
        raise MeetingSummaryError(f"Input path is not a file: {input_file}")
    if args.max_hours <= 0:
        raise MeetingSummaryError("--max-hours must be greater than 0.")
    if args.chunk_minutes <= 0:
        raise MeetingSummaryError("--chunk-minutes must be greater than 0.")
    if args.max_output_tokens <= 0:
        raise MeetingSummaryError("--max-output-tokens must be greater than 0.")
    if args.retries <= 0:
        raise MeetingSummaryError("--retries must be greater than 0.")
    return input_file


def run(args: argparse.Namespace) -> Path:
    input_file = validate_args(args)
    log(f"Starting mode: {args.mode}")
    log(f"Input file: {input_file}")
    load_local_env()
    api_key = resolve_api_key()
    model = resolve_model(args.model)
    log(f"Model: {model}")
    require_media_tools()

    duration = get_duration_seconds(input_file)
    log(
        f"Input duration: {format_time(duration)}; "
        f"size: {format_bytes(input_file.stat().st_size)}"
    )
    max_seconds = args.max_hours * 3600
    if duration > max_seconds:
        raise MeetingSummaryError(
            f"Input duration {format_time(duration)} exceeds "
            f"the configured limit {format_time(max_seconds)}."
        )

    client, types = make_client(api_key)

    temp_context = None
    if args.no_cache:
        temp_context = tempfile.TemporaryDirectory(prefix="meeting_summary_")
        work_dir = Path(temp_context.name)
        response_cache_dir = None
        log(f"Media cache disabled; using temporary work directory: {work_dir}")
    else:
        work_dir = media_cache_dir(input_file, args.chunk_minutes)
        work_dir.mkdir(parents=True, exist_ok=True)
        response_cache_dir = work_dir
        log(f"Media cache directory: {work_dir}")

    try:
        normalized = normalize_to_mp3(input_file, work_dir)

        chunk_paths = split_audio(normalized, args.chunk_minutes, work_dir)
        chunks = build_chunk_metadata(chunk_paths)
        log(f"Prepared {len(chunks)} chunk(s) for Gemini processing.")

        transcript: str | None = None
        notes: str | None = None
        if args.mode == "transcript":
            transcript = generate_transcript(
                client,
                types,
                model,
                chunks,
                args.max_output_tokens,
                args.retries,
                response_cache_dir,
            )
        elif args.mode == "meeting":
            transcript = generate_transcript(
                client,
                types,
                model,
                chunks,
                args.max_output_tokens,
                args.retries,
                response_cache_dir,
            )
            log("Creating final meeting summary and action items from transcript...")
            notes = generate_from_text(
                client,
                types,
                model,
                final_notes_prompt("transcript", transcript),
                args.max_output_tokens,
                args.retries,
            )
        elif args.mode == "compact":
            notes = generate_compact_notes(
                client,
                types,
                model,
                chunks,
                args.max_output_tokens,
                args.retries,
                response_cache_dir,
            )
        else:
            raise MeetingSummaryError(f"Unsupported mode: {args.mode}")
    finally:
        if temp_context is not None:
            temp_context.cleanup()

    output_path = args.output or default_output_path(input_file, args.mode)
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    markdown = build_markdown(
        mode=args.mode,
        input_file=input_file,
        model=model,
        duration_seconds=duration,
        chunk_count=len(chunks),
        chunk_minutes=args.chunk_minutes,
        transcript=transcript,
        notes=notes,
    )
    output_path.write_text(markdown, encoding="utf-8")
    log(f"Markdown written: {output_path} ({format_bytes(output_path.stat().st_size)})")
    if args.no_copy:
        log("Markdown copy disabled by --no-copy.")
    else:
        copy_markdown_output(output_path, args.copy_to)
    if args.no_publish:
        log("GitHub publish disabled by --no-publish.")
    else:
        publish_markdown_to_git(
            output_path,
            args.publish_github_repo,
            args.publish_github_branch,
            args.publish_github_dir,
            args.publish_checkout,
        )
    return output_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        output_path = run(args)
    except MeetingSummaryError as exc:
        log(f"Error: {exc}")
        return 1
    except KeyboardInterrupt:
        log("Interrupted.")
        return 130
    print(f"Wrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
