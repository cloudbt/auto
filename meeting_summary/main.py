"""Meeting summary CLI: transcribe audio via pluggable STT providers, then
summarize with Gemini.

Thin entry point: argument parsing + orchestration. The heavy lifting lives in
config / media / providers / summarize / export.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path
from typing import Sequence

from common import MeetingSummaryError, log
# Re-exported for backward compatibility (telegram_bot.py imports these here).
from config import (  # noqa: F401
    load_local_env,
    resolve_gemini_keys,
    resolve_language,
    resolve_model,
    resolve_provider_order,
)
from export import (
    build_markdown,
    copy_markdown_output,
    default_output_path,
    publish_markdown_to_git,
)
from media import (  # noqa: F401  (sanitize_filename re-exported for telegram_bot)
    build_chunk_metadata,
    get_duration_seconds,
    media_cache_dir,
    normalize_to_mp3,
    require_media_tools,
    run_tool,
    sanitize_filename,
    split_audio,
)
from providers import (
    AudioSource,
    GeminiSettings,
    TranscriptResult,
    build_providers,
    transcribe_with_fallback,
)
from summarize import summarize_transcript
from usage import UsageTracker

DEFAULT_CHUNK_MINUTES = 25.0
DEFAULT_MAX_HOURS = 2.0
DEFAULT_MAX_OUTPUT_TOKENS = 65536
DEFAULT_BENCHMARK_SECONDS = 60
DEFAULT_COPY_OUTPUT_DIR = Path(
    r"C:\Users\whz\iCloudDrive\iCloud~md~obsidian\work\work\MeetingSummary"
)
DEFAULT_PUBLISH_GITHUB_REPO = "https://github.com/cloudbt/dev.git"
DEFAULT_PUBLISH_GITHUB_BRANCH = "main"
DEFAULT_PUBLISH_GITHUB_DIR = "meeting"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Transcribe meeting audio with auto-selected STT providers "
            "(Gladia -> Speechmatics -> Gemini) and summarize with Gemini."
        )
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("input_file", type=Path, help="Audio or video file path.")
    common.add_argument(
        "-o", "--output", type=Path,
        help="Markdown output path. Defaults to meeting_summary/output/.",
    )
    common.add_argument(
        "--model",
        help="Gemini summarizer model. Defaults to GEMINI_MODEL or gemini-2.5-flash.",
    )
    common.add_argument(
        "--stt-order",
        help=(
            "Comma-separated STT provider order. Defaults to STT_PROVIDER_ORDER "
            "or 'gladia,speechmatics,gemini'."
        ),
    )
    common.add_argument(
        "--language",
        help="Primary spoken language hint (ISO code). Defaults to MEETING_LANGUAGE or 'ja'.",
    )
    common.add_argument(
        "--max-hours", type=float, default=DEFAULT_MAX_HOURS,
        help="Reject inputs longer than this many hours. Default: 2.",
    )
    common.add_argument(
        "--chunk-minutes", type=float, default=DEFAULT_CHUNK_MINUTES,
        help=f"Audio chunk size in minutes. Default: {DEFAULT_CHUNK_MINUTES:g}.",
    )
    common.add_argument(
        "--max-output-tokens", type=int, default=DEFAULT_MAX_OUTPUT_TOKENS,
        help="Gemini max output tokens per request. Default: 65536.",
    )
    common.add_argument(
        "--retries", type=int, default=3,
        help="Retry count for upload/generation calls. Default: 3.",
    )
    common.add_argument(
        "--no-cache", action="store_true",
        help="Do not reuse the local MP3/chunk/transcript cache next to the input file.",
    )
    common.add_argument(
        "--copy-to", type=Path, default=DEFAULT_COPY_OUTPUT_DIR,
        help=f"Also copy the final Markdown to this directory. Default: {DEFAULT_COPY_OUTPUT_DIR}",
    )
    common.add_argument(
        "--no-copy", action="store_true",
        help="Do not copy the final Markdown to the Obsidian/iCloud folder.",
    )
    common.add_argument(
        "--publish-github-repo", default=DEFAULT_PUBLISH_GITHUB_REPO,
        help=f"Git repository to push the final Markdown to. Default: {DEFAULT_PUBLISH_GITHUB_REPO}",
    )
    common.add_argument(
        "--publish-github-branch", default=DEFAULT_PUBLISH_GITHUB_BRANCH,
        help=f"Branch to push to. Default: {DEFAULT_PUBLISH_GITHUB_BRANCH}",
    )
    common.add_argument(
        "--publish-github-dir", default=DEFAULT_PUBLISH_GITHUB_DIR,
        help=f"Directory in the publish repo. Default: {DEFAULT_PUBLISH_GITHUB_DIR}",
    )
    common.add_argument(
        "--publish-checkout", type=Path,
        help="Local checkout path used for GitHub publishing.",
    )
    common.add_argument(
        "--no-publish", action="store_true",
        help="Do not push the final Markdown to GitHub.",
    )

    subparsers.add_parser(
        "transcript", parents=[common], help="Output only a timestamped transcript."
    )
    subparsers.add_parser(
        "meeting", parents=[common],
        help="Output summary, action items (TODO), and transcript.",
    )
    subparsers.add_parser(
        "compact", parents=[common], help="Output only summary and action items (TODO)."
    )

    bench = subparsers.add_parser(
        "benchmark", help="Time every configured STT provider on a short sample."
    )
    bench.add_argument("input_file", type=Path, help="Audio or video file path.")
    bench.add_argument("--stt-order", help="Comma-separated STT provider order to test.")
    bench.add_argument("--language", help="Language hint (ISO code).")
    bench.add_argument(
        "--sample-seconds", type=int, default=DEFAULT_BENCHMARK_SECONDS,
        help=f"Seconds of audio to test per provider. Default: {DEFAULT_BENCHMARK_SECONDS}.",
    )
    return parser


def validate_args(args: argparse.Namespace) -> Path:
    input_file = args.input_file.expanduser().resolve()
    if not input_file.exists():
        raise MeetingSummaryError(f"Input file does not exist: {input_file}")
    if not input_file.is_file():
        raise MeetingSummaryError(f"Input path is not a file: {input_file}")
    if getattr(args, "max_hours", 1) <= 0:
        raise MeetingSummaryError("--max-hours must be greater than 0.")
    if getattr(args, "chunk_minutes", 1) <= 0:
        raise MeetingSummaryError("--chunk-minutes must be greater than 0.")
    if getattr(args, "max_output_tokens", 1) <= 0:
        raise MeetingSummaryError("--max-output-tokens must be greater than 0.")
    if getattr(args, "retries", 1) <= 0:
        raise MeetingSummaryError("--retries must be greater than 0.")
    return input_file


def gemini_settings(args: argparse.Namespace, *, required: bool) -> GeminiSettings:
    return GeminiSettings(
        keys=resolve_gemini_keys(required=required),
        model=resolve_model(args.model),
        max_output_tokens=args.max_output_tokens,
        retries=args.retries,
    )


def prepare_audio(
    input_file: Path, args: argparse.Namespace, duration: float
):
    """Normalize + split the input, returning (AudioSource, cache_dir, cleanup)."""
    if args.no_cache:
        temp_context = tempfile.TemporaryDirectory(prefix="meeting_summary_")
        work_dir = Path(temp_context.name)
        cache_dir = None
        log(f"Media cache disabled; using temporary work directory: {work_dir}")
    else:
        temp_context = None
        work_dir = media_cache_dir(input_file, args.chunk_minutes)
        work_dir.mkdir(parents=True, exist_ok=True)
        cache_dir = work_dir
        log(f"Media cache directory: {work_dir}")

    normalized = normalize_to_mp3(input_file, work_dir)
    chunk_paths = split_audio(normalized, args.chunk_minutes, work_dir)
    chunks = build_chunk_metadata(chunk_paths)
    log(f"Prepared {len(chunks)} chunk(s) for transcription.")
    audio = AudioSource(
        normalized_mp3=normalized,
        duration_s=duration,
        chunks=chunks,
        cache_dir=cache_dir,
    )
    return audio, cache_dir, temp_context


def load_cached_transcript(cache_dir: Path | None) -> TranscriptResult | None:
    if cache_dir is None:
        return None
    path = cache_dir / "transcript.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    result = TranscriptResult.from_dict(data)
    if not result.segments:
        return None
    log(f"Reusing cached transcript ({result.provider}) from {path}.")
    return result


def save_cached_transcript(cache_dir: Path | None, result: TranscriptResult) -> None:
    if cache_dir is None:
        return
    path = cache_dir / "transcript.json"
    try:
        path.write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log(f"Cached transcript: {path}")
    except OSError as exc:
        log(f"Warning: could not cache transcript {path}: {exc}")


def get_transcript(
    args: argparse.Namespace, input_file: Path, duration: float
) -> tuple[TranscriptResult, int, str]:
    """Return (transcript, chunk_count, language) using cache + provider fallback."""
    language = resolve_language(args.language)
    audio, cache_dir, temp_context = prepare_audio(input_file, args, duration)
    try:
        cached = load_cached_transcript(cache_dir)
        if cached is not None:
            return cached, len(audio.chunks), language

        order = resolve_provider_order(args.stt_order)
        log(f"STT provider order: {', '.join(order)}")
        providers = build_providers(order, gemini_settings(args, required=False))
        usage = UsageTracker()
        result = transcribe_with_fallback(
            audio, providers, usage, language=language, progress=log
        )
        save_cached_transcript(cache_dir, result)
        return result, len(audio.chunks), language
    finally:
        if temp_context is not None:
            temp_context.cleanup()


def run(args: argparse.Namespace) -> Path:
    input_file = validate_args(args)
    log(f"Starting mode: {args.mode}")
    log(f"Input file: {input_file}")
    load_local_env()
    require_media_tools()

    duration = get_duration_seconds(input_file)
    if duration > args.max_hours * 3600:
        raise MeetingSummaryError(
            f"Input duration {duration / 3600:.2f}h exceeds the limit "
            f"{args.max_hours:g}h."
        )

    transcript, chunk_count, language = get_transcript(args, input_file, duration)
    transcript_md = transcript.to_markdown()

    notes = None
    if args.mode != "transcript":
        settings = gemini_settings(args, required=True)
        log("Creating final meeting summary and action items (TODO)...")
        notes = summarize_transcript(
            transcript_md,
            keys=settings.keys,
            model=settings.model,
            max_output_tokens=settings.max_output_tokens,
            retries=settings.retries,
        )

    output_path = (args.output or default_output_path(input_file, args.mode))
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    markdown = build_markdown(
        mode=args.mode,
        input_file=input_file,
        stt_provider=transcript.provider,
        summarizer_model=resolve_model(args.model),
        language=language,
        duration_seconds=duration,
        chunk_count=chunk_count,
        chunk_minutes=args.chunk_minutes,
        transcript=transcript_md if args.mode != "compact" else None,
        notes=notes,
    )
    output_path.write_text(markdown, encoding="utf-8")
    log(f"Markdown written: {output_path}")

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


def run_benchmark(args: argparse.Namespace) -> int:
    input_file = validate_args(args)
    log(f"Benchmark on first {args.sample_seconds}s of: {input_file}")
    load_local_env()
    require_media_tools()

    with tempfile.TemporaryDirectory(prefix="meeting_benchmark_") as tmp:
        work_dir = Path(tmp)
        normalized = normalize_to_mp3(input_file, work_dir)
        sample = work_dir / "sample.mp3"
        run_tool(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i",
             str(normalized), "-t", str(args.sample_seconds), "-c", "copy", str(sample)],
            "ffmpeg benchmark sample",
        )
        sample_duration = get_duration_seconds(sample)
        chunks = build_chunk_metadata([sample])
        audio = AudioSource(sample, sample_duration, chunks, cache_dir=None)

        language = resolve_language(args.language)
        order = resolve_provider_order(args.stt_order)
        gemini = GeminiSettings(
            keys=resolve_gemini_keys(required=False),
            model=resolve_model(getattr(args, "model", None)),
        )
        providers = build_providers(order, gemini)

        rows: list[tuple[str, str, str, str]] = []
        for provider in providers:
            if not provider.is_configured():
                rows.append((provider.name, "skip", "-", "no API key"))
                continue
            try:
                started = time.monotonic()
                result = provider.transcribe(audio, language=language, progress=log)
                elapsed = time.monotonic() - started
                ratio = elapsed / max(sample_duration / 60.0, 1e-6)
                rows.append(
                    (provider.name, "ok", f"{elapsed:.1f}s",
                     f"{len(result.segments)} seg, {ratio:.1f} s/min")
                )
            except Exception as exc:  # noqa: BLE001 - report any failure per provider
                rows.append((provider.name, "FAIL", "-", str(exc)[:80]))

    print(f"\nBenchmark ({sample_duration:.0f}s sample):")
    print(f"{'provider':<14}{'status':<8}{'elapsed':<10}{'detail'}")
    for name, status, elapsed, detail in rows:
        print(f"{name:<14}{status:<8}{elapsed:<10}{detail}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.mode == "benchmark":
            return run_benchmark(args)
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
