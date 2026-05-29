from __future__ import annotations

import argparse
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


DEFAULT_MODEL = "gemini-3.5-flash"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CHUNK_MINUTES = 20.0
DEFAULT_MAX_HOURS = 2.0
DEFAULT_MAX_OUTPUT_TOKENS = 65536

T = TypeVar("T")


class MeetingSummaryError(Exception):
    """Raised for expected user-facing failures."""


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
        help="Audio chunk size in minutes. Default: 20.",
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


def require_media_tools() -> None:
    missing = [tool for tool in ("ffmpeg", "ffprobe") if shutil.which(tool) is None]
    if missing:
        raise MeetingSummaryError(
            "Missing required media tool(s): "
            + ", ".join(missing)
            + ". Install ffmpeg and ensure it is on PATH."
        )


def run_tool(args: Sequence[str], label: str) -> str:
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
    return output_path


def split_audio(normalized_path: Path, chunk_minutes: float, work_dir: Path) -> list[Path]:
    chunk_seconds = int(round(chunk_minutes * 60))
    if chunk_seconds <= 0:
        raise MeetingSummaryError("--chunk-minutes must be greater than 0.")

    duration = get_duration_seconds(normalized_path)
    if duration <= chunk_seconds + 0.5:
        return [normalized_path]

    chunks_dir = work_dir / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    pattern = chunks_dir / "chunk_%04d.mp3"
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
    return chunks


def build_chunk_metadata(paths: list[Path]) -> list[AudioChunk]:
    chunks: list[AudioChunk] = []
    offset = 0.0
    total = len(paths)
    for index, path in enumerate(paths, start=1):
        duration = get_duration_seconds(path)
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
    return genai.Client(api_key=api_key), types


def generation_config(types, max_output_tokens: int):
    return types.GenerateContentConfig(
        temperature=0.1,
        max_output_tokens=max_output_tokens,
    )


def retry(operation: Callable[[], T], description: str, attempts: int) -> T:
    attempts = max(1, attempts)
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as exc:  # SDK exceptions vary by transport.
            last_error = exc
            if attempt == attempts:
                break
            delay = min(2 ** (attempt - 1), 8)
            print(
                f"{description} failed on attempt {attempt}/{attempts}; "
                f"retrying in {delay}s...",
                file=sys.stderr,
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
            return current
        if state_name in ("FAILED", "ERROR"):
            raise MeetingSummaryError(f"Uploaded file processing failed: {state_name}")
        if time.monotonic() >= deadline:
            raise MeetingSummaryError(
                f"Timed out waiting for uploaded file to become ACTIVE: {state_name}"
            )
        time.sleep(5)
        current = client.files.get(name=current.name)


def response_text(response) -> str:
    text = getattr(response, "text", None)
    if text and text.strip():
        return strip_outer_fence(text.strip())
    raise MeetingSummaryError("Gemini returned an empty response.")


def strip_outer_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```") or not stripped.endswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) >= 3:
        return "\n".join(lines[1:-1]).strip()
    return stripped


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
        uploaded = retry(
            lambda: client.files.upload(file=str(chunk_path)),
            "Gemini file upload",
            retries,
        )
        uploaded = wait_until_active(client, uploaded)
        config = generation_config(types, max_output_tokens)
        response = retry(
            lambda: client.models.generate_content(
                model=model,
                contents=[prompt, uploaded],
                config=config,
            ),
            "Gemini content generation",
            retries,
        )
        return response_text(response)
    finally:
        if uploaded is not None:
            try:
                client.files.delete(name=uploaded.name)
            except Exception as exc:
                print(
                    f"Warning: could not delete uploaded file {uploaded.name}: {exc}",
                    file=sys.stderr,
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
    response = retry(
        lambda: client.models.generate_content(
            model=model,
            contents=prompt,
            config=config,
        ),
        "Gemini content generation",
        retries,
    )
    return response_text(response)


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
) -> str:
    parts: list[str] = []
    for chunk in chunks:
        print(
            f"Transcribing chunk {chunk.index}/{chunk.total} "
            f"({format_time(chunk.start_seconds)}-{format_time(chunk.end_seconds)})...",
            file=sys.stderr,
        )
        parts.append(
            generate_from_file(
                client,
                types,
                model,
                chunk.path,
                transcript_prompt(chunk),
                max_output_tokens,
                retries,
            )
        )
    return "\n\n".join(part.strip() for part in parts if part.strip())


def generate_compact_notes(
    client,
    types,
    model: str,
    chunks: list[AudioChunk],
    max_output_tokens: int,
    retries: int,
) -> str:
    chunk_notes: list[str] = []
    for chunk in chunks:
        print(
            f"Summarizing chunk {chunk.index}/{chunk.total} "
            f"({format_time(chunk.start_seconds)}-{format_time(chunk.end_seconds)})...",
            file=sys.stderr,
        )
        chunk_notes.append(
            generate_from_file(
                client,
                types,
                model,
                chunk.path,
                chunk_compact_prompt(chunk),
                max_output_tokens,
                retries,
            )
        )

    combined = "\n\n".join(
        f"### Chunk {index + 1}\n{note.strip()}"
        for index, note in enumerate(chunk_notes)
        if note.strip()
    )
    print("Creating final compact meeting notes...", file=sys.stderr)
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
    load_local_env()
    api_key = resolve_api_key()
    model = resolve_model(args.model)
    require_media_tools()

    duration = get_duration_seconds(input_file)
    max_seconds = args.max_hours * 3600
    if duration > max_seconds:
        raise MeetingSummaryError(
            f"Input duration {format_time(duration)} exceeds "
            f"the configured limit {format_time(max_seconds)}."
        )

    client, types = make_client(api_key)

    with tempfile.TemporaryDirectory(prefix="meeting_summary_") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        print("Normalizing media to mono MP3...", file=sys.stderr)
        normalized = normalize_to_mp3(input_file, temp_dir)

        print("Splitting audio into chunks...", file=sys.stderr)
        chunk_paths = split_audio(normalized, args.chunk_minutes, temp_dir)
        chunks = build_chunk_metadata(chunk_paths)

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
            )
        elif args.mode == "meeting":
            transcript = generate_transcript(
                client,
                types,
                model,
                chunks,
                args.max_output_tokens,
                args.retries,
            )
            print("Creating final meeting summary and action items...", file=sys.stderr)
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
            )
        else:
            raise MeetingSummaryError(f"Unsupported mode: {args.mode}")

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
    return output_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        output_path = run(args)
    except MeetingSummaryError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    print(f"Wrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
