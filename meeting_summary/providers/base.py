"""Provider contract and unified transcript data model.

Every speech-to-text provider returns a :class:`TranscriptResult` made of
absolute-timestamped :class:`TranscriptSegment` objects, so the rest of the
pipeline (summarizer, Markdown export, caching) is provider-agnostic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from media import AudioChunk, format_time

Progress = Callable[[str], None]


class ProviderError(Exception):
    """A provider failed for a recoverable reason; try the next provider."""


class QuotaExceeded(ProviderError):
    """The provider's free quota / credits are exhausted.

    The orchestrator marks the provider exhausted for the month and moves on.
    """


class ProviderNotConfigured(ProviderError):
    """No credentials for this provider; it is skipped silently."""


@dataclass(frozen=True)
class TranscriptSegment:
    start: float  # absolute seconds in the original recording
    end: float
    text: str
    speaker: str | None = None

    def to_dict(self) -> dict:
        data = {"start": self.start, "end": self.end, "text": self.text}
        if self.speaker:
            data["speaker"] = self.speaker
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "TranscriptSegment":
        return cls(
            start=float(data.get("start", 0.0)),
            end=float(data.get("end", 0.0)),
            text=str(data.get("text", "")),
            speaker=data.get("speaker"),
        )


@dataclass
class TranscriptResult:
    provider: str
    segments: list[TranscriptSegment] = field(default_factory=list)
    language: str | None = None

    def to_markdown(self) -> str:
        """Render ``[HH:MM:SS] (Speaker) text`` paragraphs (absolute times)."""
        lines: list[str] = []
        for seg in self.segments:
            text = seg.text.strip()
            if not text:
                continue
            speaker = f"({seg.speaker}) " if seg.speaker else ""
            lines.append(f"[{format_time(seg.start)}] {speaker}{text}")
        return "\n\n".join(lines)

    def to_plain_text(self) -> str:
        """Timestamped plain text fed to the summarizer."""
        return self.to_markdown()

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "language": self.language,
            "segments": [seg.to_dict() for seg in self.segments],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TranscriptResult":
        return cls(
            provider=str(data.get("provider", "unknown")),
            language=data.get("language"),
            segments=[TranscriptSegment.from_dict(s) for s in data.get("segments", [])],
        )


@dataclass
class AudioSource:
    """The audio handed to a provider.

    ``normalized_mp3`` is the whole file (used by providers that accept long
    audio in one request, e.g. Gladia/Speechmatics). ``chunks`` are the
    time-aligned splits (used by chunked providers, e.g. Gemini). ``cache_dir``
    is the per-input media cache where chunked providers may store partial
    per-chunk results to resume on failure (``None`` disables caching).
    """

    normalized_mp3: Path
    duration_s: float
    chunks: list[AudioChunk]
    cache_dir: Path | None = None


class TranscriptionProvider(ABC):
    name: str = "base"
    # Known monthly free-tier allowance in minutes (None = unknown/unlimited).
    default_free_minutes: float | None = None

    def __init__(self, free_minutes_override: float | None = None):
        self._free_minutes_override = free_minutes_override

    def free_minutes(self) -> float | None:
        if self._free_minutes_override is not None:
            return self._free_minutes_override
        return self.default_free_minutes

    @abstractmethod
    def is_configured(self) -> bool:
        """True when the provider has the credentials it needs to run."""

    @abstractmethod
    def transcribe(
        self,
        audio: AudioSource,
        *,
        language: str | None,
        progress: Progress,
    ) -> TranscriptResult:
        """Transcribe ``audio`` or raise.

        Raise :class:`QuotaExceeded` when free credits are gone (so the month is
        marked exhausted), or :class:`ProviderError` for other recoverable
        failures (so the orchestrator tries the next provider).
        """
