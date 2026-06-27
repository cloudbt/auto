"""Output: assemble Markdown, copy to Obsidian/iCloud, and publish to GitHub."""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Sequence

from common import MeetingSummaryError, log
from media import format_bytes, format_time, sanitize_filename

SCRIPT_DIR = Path(__file__).resolve().parent


def build_markdown(
    *,
    mode: str,
    input_file: Path,
    stt_provider: str,
    summarizer_model: str,
    language: str,
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
        f"- Transcription: `{stt_provider}`",
        f"- Summarizer: `{summarizer_model}`",
        f"- Language: `{language}`",
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
