"""Gemini audio transcription provider (final STT fallback).

Reuses the existing google-genai upload + generate path with multi-key
rotation, then parses Gemini's ``[HH:MM:SS] text`` Markdown back into the
unified segment model.
"""

from __future__ import annotations

import hashlib
import re

from common import MeetingSummaryError, log
from gemini_client import (
    ClientPool,
    DEFAULT_MAX_OUTPUT_TOKENS,
    generate_from_file,
    is_rate_limited,
)
from media import AudioChunk, MEDIA_CACHE_VERSION, format_time

from .base import (
    AudioSource,
    Progress,
    ProviderError,
    QuotaExceeded,
    TranscriptionProvider,
    TranscriptResult,
    TranscriptSegment,
)

_TIMESTAMP_RE = re.compile(r"^\[(\d{1,2}):(\d{2}):(\d{2})\]\s*(.*)$")
_SPEAKER_RE = re.compile(r"^\(([^)]{1,40})\)\s*(.*)$")


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


def parse_timestamped_text(
    text: str, fallback_start: float, fallback_end: float
) -> list[TranscriptSegment]:
    """Parse ``[HH:MM:SS] text`` paragraphs into segments.

    Lines without a leading timestamp are appended to the current segment. If no
    timestamps are found at all, the whole block becomes one segment spanning the
    chunk.
    """
    raw_segments: list[tuple[float, list[str]]] = []
    for line in text.splitlines():
        match = _TIMESTAMP_RE.match(line.strip())
        if match:
            h, m, s, rest = match.groups()
            start = int(h) * 3600 + int(m) * 60 + int(s)
            raw_segments.append((float(start), [rest.strip()] if rest.strip() else []))
        elif raw_segments and line.strip():
            raw_segments[-1][1].append(line.strip())

    if not raw_segments:
        body = text.strip()
        if not body:
            return []
        return [TranscriptSegment(start=fallback_start, end=fallback_end, text=body)]

    segments: list[TranscriptSegment] = []
    for index, (start, lines) in enumerate(raw_segments):
        end = raw_segments[index + 1][0] if index + 1 < len(raw_segments) else fallback_end
        if end < start:
            end = start
        body = " ".join(part for part in lines if part).strip()
        speaker = None
        speaker_match = _SPEAKER_RE.match(body)
        if speaker_match:
            speaker, body = speaker_match.group(1).strip(), speaker_match.group(2).strip()
        if not body:
            continue
        segments.append(
            TranscriptSegment(start=start, end=end, text=body, speaker=speaker)
        )
    return segments


class GeminiProvider(TranscriptionProvider):
    name = "gemini"
    default_free_minutes = None  # Multi-key rotation handles per-key limits.

    def __init__(
        self,
        keys: list[str],
        model: str,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        retries: int = 3,
        free_minutes_override: float | None = None,
    ):
        super().__init__(free_minutes_override=free_minutes_override)
        self._keys = list(keys)
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._retries = retries

    def is_configured(self) -> bool:
        return bool(self._keys)

    def transcribe(
        self,
        audio: AudioSource,
        *,
        language: str | None,
        progress: Progress,
    ) -> TranscriptResult:
        if not self._keys:
            raise ProviderError("Gemini provider has no API keys configured.")
        client = ClientPool(self._keys)
        segments: list[TranscriptSegment] = []
        try:
            for chunk in audio.chunks:
                progress(
                    f"gemini: transcribing chunk {chunk.index}/{chunk.total} "
                    f"({format_time(chunk.start_seconds)}-{format_time(chunk.end_seconds)})"
                )
                text = self._transcribe_chunk(client, chunk, audio.cache_dir)
                segments.extend(
                    parse_timestamped_text(text, chunk.start_seconds, chunk.end_seconds)
                )
        except MeetingSummaryError as exc:
            # All keys exhausted on a quota error is the signal to fall over to
            # nothing (Gemini is last); surface it as QuotaExceeded so the month
            # is marked rather than retried.
            if is_rate_limited(exc):
                raise QuotaExceeded(str(exc)) from exc
            raise ProviderError(str(exc)) from exc

        if not segments:
            raise ProviderError("Gemini returned no transcript segments.")
        return TranscriptResult(provider=self.name, segments=segments, language=language)

    def _transcribe_chunk(self, client, chunk: AudioChunk, cache_dir) -> str:
        prompt = transcript_prompt(chunk)
        cached = self._read_cache(cache_dir, chunk, prompt)
        if cached is not None:
            log(f"Reusing cached Gemini transcript for chunk {chunk.index}/{chunk.total}.")
            return cached
        text = generate_from_file(
            client,
            client.types,
            self._model,
            chunk.path,
            prompt,
            self._max_output_tokens,
            self._retries,
        )
        self._write_cache(cache_dir, chunk, prompt, text)
        return text

    def _cache_path(self, cache_dir, chunk: AudioChunk, prompt: str):
        source = "|".join(
            [
                MEDIA_CACHE_VERSION,
                self._model,
                "transcript",
                chunk.path.name,
                str(chunk.path.stat().st_size),
                format_time(chunk.start_seconds),
                format_time(chunk.end_seconds),
                prompt,
            ]
        )
        digest = hashlib.sha1(source.encode("utf-8")).hexdigest()[:12]
        return cache_dir / "gemini_responses" / f"transcript_chunk_{chunk.index:04d}_{digest}.md"

    def _read_cache(self, cache_dir, chunk: AudioChunk, prompt: str):
        if cache_dir is None:
            return None
        path = self._cache_path(cache_dir, chunk, prompt)
        if not path.exists() or path.stat().st_size == 0:
            return None
        text = path.read_text(encoding="utf-8").strip()
        return text or None

    def _write_cache(self, cache_dir, chunk: AudioChunk, prompt: str, text: str) -> None:
        if cache_dir is None:
            return
        path = self._cache_path(cache_dir, chunk, prompt)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text.strip() + "\n", encoding="utf-8")
