"""Shared logging and error types used across the meeting_summary package."""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
LOG_FILE = SCRIPT_DIR / "meeting_summary.log"


class MeetingSummaryError(Exception):
    """Raised for expected user-facing failures."""


class EmptyResponseError(Exception):
    """An LLM returned no usable text (e.g. finishReason=MALFORMED_RESPONSE)."""


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
