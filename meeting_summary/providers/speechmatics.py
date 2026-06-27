"""Speechmatics speech-to-text provider (second choice).

Async batch API: submit the whole MP3 as a job, poll until done, then fetch the
json-v2 transcript and group the word/punctuation results into diarized,
timestamped utterances.
"""

from __future__ import annotations

import json
import os
import time

from common import log

from .base import (
    AudioSource,
    Progress,
    ProviderError,
    TranscriptionProvider,
    TranscriptResult,
    TranscriptSegment,
)
from ._http import raise_for_status, require_requests

BASE_URL = "https://asr.api.speechmatics.com/v2"
POLL_INTERVAL_SECONDS = 10
POLL_TIMEOUT_SECONDS = 60 * 60  # 1 hour backstop.

# Flush an utterance after a pause this long or once it gets this long.
_GAP_SECONDS = 2.0
_MAX_SEGMENT_SECONDS = 30.0


class SpeechmaticsProvider(TranscriptionProvider):
    name = "speechmatics"
    default_free_minutes = 480.0  # ~8 hours/month free tier.

    def __init__(self, free_minutes_override: float | None = None):
        super().__init__(free_minutes_override=free_minutes_override)
        self._api_key = os.environ.get("SPEECHMATICS_API_KEY", "").strip()

    def is_configured(self) -> bool:
        return bool(self._api_key)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._api_key}"}

    def transcribe(
        self,
        audio: AudioSource,
        *,
        language: str | None,
        progress: Progress,
    ) -> TranscriptResult:
        if not self._api_key:
            raise ProviderError(
                "Speechmatics provider has no SPEECHMATICS_API_KEY configured."
            )
        requests = require_requests()

        progress("speechmatics: submitting job...")
        job_id = self._submit(requests, audio, language)

        progress("speechmatics: waiting for transcription...")
        self._poll(requests, job_id, progress)

        progress("speechmatics: fetching transcript...")
        results = self._fetch_transcript(requests, job_id)

        segments = self._group(results)
        if not segments:
            raise ProviderError("Speechmatics returned an empty transcript.")
        return TranscriptResult(provider=self.name, segments=segments, language=language)

    def _submit(self, requests, audio: AudioSource, language: str | None) -> str:
        config = {
            "type": "transcription",
            "transcription_config": {
                "language": language or "ja",
                "operating_point": "enhanced",
                "diarization": "speaker",
            },
        }
        with audio.normalized_mp3.open("rb") as handle:
            response = requests.post(
                f"{BASE_URL}/jobs",
                headers=self._headers(),
                files={
                    "data_file": (audio.normalized_mp3.name, handle, "audio/mpeg"),
                    "config": (None, json.dumps(config), "application/json"),
                },
                timeout=300,
            )
        raise_for_status(self.name, response, "job submit")
        job_id = response.json().get("id")
        if not job_id:
            raise ProviderError("Speechmatics job submit returned no id.")
        return job_id

    def _poll(self, requests, job_id: str, progress: Progress) -> None:
        deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
        while True:
            response = requests.get(
                f"{BASE_URL}/jobs/{job_id}", headers=self._headers(), timeout=60
            )
            raise_for_status(self.name, response, "poll")
            job = response.json().get("job", {})
            status = job.get("status")
            if status == "done":
                return
            if status in ("rejected", "deleted", "expired"):
                detail = job.get("errors") or status
                raise ProviderError(f"Speechmatics job {status}: {detail}")
            if time.monotonic() >= deadline:
                raise ProviderError("Speechmatics transcription timed out.")
            log(f"speechmatics: status={status}; waiting...")
            time.sleep(POLL_INTERVAL_SECONDS)

    def _fetch_transcript(self, requests, job_id: str) -> list[dict]:
        response = requests.get(
            f"{BASE_URL}/jobs/{job_id}/transcript",
            headers=self._headers(),
            params={"format": "json-v2"},
            timeout=120,
        )
        raise_for_status(self.name, response, "transcript fetch")
        return response.json().get("results", [])

    def _group(self, results: list[dict]) -> list[TranscriptSegment]:
        segments: list[TranscriptSegment] = []
        cur_text = ""
        cur_speaker: str | None = None
        cur_start: float | None = None
        cur_end: float | None = None

        def flush() -> None:
            nonlocal cur_text, cur_speaker, cur_start, cur_end
            text = cur_text.strip()
            if text and cur_start is not None:
                segments.append(
                    TranscriptSegment(
                        start=cur_start,
                        end=cur_end if cur_end is not None else cur_start,
                        text=text,
                        speaker=cur_speaker,
                    )
                )
            cur_text, cur_speaker, cur_start, cur_end = "", None, None, None

        for result in results:
            alternatives = result.get("alternatives") or []
            if not alternatives:
                continue
            content = (alternatives[0].get("content") or "").strip()
            if not content:
                continue
            speaker = alternatives[0].get("speaker")
            start = float(result.get("start_time", cur_end or 0.0))
            end = float(result.get("end_time", start))

            if result.get("type") == "punctuation":
                cur_text = cur_text.rstrip() + content
                cur_end = end
                continue

            new_segment = (
                cur_start is None
                or (cur_speaker is not None and speaker != cur_speaker)
                or (cur_end is not None and start - cur_end > _GAP_SECONDS)
                or (cur_start is not None and end - cur_start > _MAX_SEGMENT_SECONDS)
            )
            if new_segment:
                flush()
                cur_speaker, cur_start = speaker, start
            cur_text = f"{cur_text} {content}".strip() if cur_text else content
            cur_end = end

        flush()
        return segments
