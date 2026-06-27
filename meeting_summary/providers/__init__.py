"""STT provider registry and the quota-aware fallback orchestrator."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from common import MeetingSummaryError, log
from config import free_minutes_override

from .base import (
    AudioSource,
    Progress,
    ProviderError,
    QuotaExceeded,
    TranscriptionProvider,
    TranscriptResult,
    TranscriptSegment,
)
from .gemini import GeminiProvider
from .gladia import GladiaProvider
from .speechmatics import SpeechmaticsProvider

__all__ = [
    "AudioSource",
    "ProviderError",
    "QuotaExceeded",
    "TranscriptionProvider",
    "TranscriptResult",
    "TranscriptSegment",
    "GeminiSettings",
    "build_providers",
    "transcribe_with_fallback",
]


@dataclass
class GeminiSettings:
    keys: list[str] = field(default_factory=list)
    model: str = "gemini-2.5-flash"
    max_output_tokens: int = 65536
    retries: int = 3


def build_providers(
    order: list[str], gemini: GeminiSettings
) -> list[TranscriptionProvider]:
    """Instantiate providers in the requested order (unknown names skipped)."""
    providers: list[TranscriptionProvider] = []
    for name in order:
        override = free_minutes_override(name)
        if name == "gladia":
            providers.append(GladiaProvider(free_minutes_override=override))
        elif name == "speechmatics":
            providers.append(SpeechmaticsProvider(free_minutes_override=override))
        elif name == "gemini":
            providers.append(
                GeminiProvider(
                    keys=gemini.keys,
                    model=gemini.model,
                    max_output_tokens=gemini.max_output_tokens,
                    retries=gemini.retries,
                    free_minutes_override=override,
                )
            )
        else:
            log(f"Unknown STT provider '{name}' in STT_PROVIDER_ORDER; skipping.")
    return providers


def transcribe_with_fallback(
    audio: AudioSource,
    providers: list[TranscriptionProvider],
    usage,
    *,
    language: str | None,
    progress: Progress = log,
) -> TranscriptResult:
    """Try each provider in order until one transcribes successfully.

    Skips providers that are not configured or whose local free-tier estimate is
    used up. On :class:`QuotaExceeded` the provider is marked exhausted for the
    month; on any other :class:`ProviderError` it falls through to the next.
    """
    minutes = audio.duration_s / 60.0
    errors: list[str] = []
    for provider in providers:
        name = provider.name
        if not provider.is_configured():
            progress(f"skip {name}: not configured (no API key)")
            continue
        if usage is not None and usage.is_exhausted(name, provider.free_minutes(), minutes):
            progress(f"skip {name}: free tier used up this month")
            continue
        try:
            progress(f"transcribing with {name} ...")
            started = time.monotonic()
            result = provider.transcribe(audio, language=language, progress=progress)
            elapsed = time.monotonic() - started
            if usage is not None:
                usage.record(name, minutes, elapsed=elapsed)
            progress(
                f"{name} produced {len(result.segments)} segment(s) "
                f"in {elapsed:.0f}s."
            )
            return result
        except QuotaExceeded as exc:
            if usage is not None:
                usage.mark_exhausted(name)
            errors.append(f"{name}: quota exhausted ({exc})")
            progress(f"{name} free quota exhausted; trying next provider.")
        except ProviderError as exc:
            errors.append(f"{name}: {exc}")
            progress(f"{name} failed ({exc}); trying next provider.")

    raise MeetingSummaryError(
        "All STT providers failed or are exhausted:\n  " + "\n  ".join(errors)
        if errors
        else "No STT providers were configured. Set GLADIA_API_KEY, "
        "SPEECHMATICS_API_KEY, or GEMINI_API_KEY in meeting_summary/.env."
    )
