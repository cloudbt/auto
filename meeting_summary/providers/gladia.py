"""Gladia speech-to-text provider (first choice).

Two-step async API: upload the whole normalized MP3, request a pre-recorded
transcription job, then poll until done. Returns diarized, timestamped
utterances. Generous free tier, so it is tried first.
"""

from __future__ import annotations

import os
import time

from common import log

from .base import (
    AudioSource,
    Progress,
    ProviderError,
    QuotaExceeded,
    TranscriptionProvider,
    TranscriptResult,
    TranscriptSegment,
)
from ._http import raise_for_status, require_requests

BASE_URL = "https://api.gladia.io"
POLL_INTERVAL_SECONDS = 5
POLL_TIMEOUT_SECONDS = 60 * 60  # 1 hour backstop for long recordings.


class GladiaProvider(TranscriptionProvider):
    name = "gladia"
    default_free_minutes = 600.0  # ~10 hours/month free tier.

    def __init__(self, free_minutes_override: float | None = None):
        super().__init__(free_minutes_override=free_minutes_override)
        self._api_key = os.environ.get("GLADIA_API_KEY", "").strip()

    def is_configured(self) -> bool:
        return bool(self._api_key)

    def _headers(self) -> dict:
        return {"x-gladia-key": self._api_key}

    def transcribe(
        self,
        audio: AudioSource,
        *,
        language: str | None,
        progress: Progress,
    ) -> TranscriptResult:
        if not self._api_key:
            raise ProviderError("Gladia provider has no GLADIA_API_KEY configured.")
        requests = require_requests()

        progress("gladia: uploading audio...")
        audio_url = self._upload(requests, audio)

        progress("gladia: requesting transcription...")
        result_url = self._request_job(requests, audio_url, language)

        progress("gladia: waiting for transcription...")
        result = self._poll(requests, result_url, progress)

        segments = self._parse(result)
        if not segments:
            raise ProviderError("Gladia returned an empty transcript.")
        return TranscriptResult(provider=self.name, segments=segments, language=language)

    def _upload(self, requests, audio: AudioSource) -> str:
        with audio.normalized_mp3.open("rb") as handle:
            response = requests.post(
                f"{BASE_URL}/v2/upload",
                headers=self._headers(),
                files={"audio": (audio.normalized_mp3.name, handle, "audio/mpeg")},
                timeout=300,
            )
        raise_for_status(self.name, response, "upload")
        data = response.json()
        audio_url = data.get("audio_url")
        if not audio_url:
            raise ProviderError(f"Gladia upload returned no audio_url: {data}")
        return audio_url

    def _request_job(self, requests, audio_url: str, language: str | None) -> str:
        body: dict = {"audio_url": audio_url, "diarization": True}
        if language:
            body["language"] = language
            body["enable_code_switching"] = True
        else:
            body["detect_language"] = True
        response = requests.post(
            f"{BASE_URL}/v2/pre-recorded",
            headers={**self._headers(), "Content-Type": "application/json"},
            json=body,
            timeout=60,
        )
        raise_for_status(self.name, response, "job submit")
        data = response.json()
        result_url = data.get("result_url") or (
            f"{BASE_URL}/v2/pre-recorded/{data['id']}" if data.get("id") else None
        )
        if not result_url:
            raise ProviderError(f"Gladia job submit returned no result_url: {data}")
        return result_url

    def _poll(self, requests, result_url: str, progress: Progress) -> dict:
        deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
        while True:
            response = requests.get(result_url, headers=self._headers(), timeout=60)
            raise_for_status(self.name, response, "poll")
            data = response.json()
            status = data.get("status")
            if status == "done":
                return data.get("result", {})
            if status == "error":
                detail = data.get("error_code") or data.get("error") or data
                raise ProviderError(f"Gladia transcription errored: {detail}")
            if time.monotonic() >= deadline:
                raise ProviderError("Gladia transcription timed out.")
            log(f"gladia: status={status}; waiting...")
            time.sleep(POLL_INTERVAL_SECONDS)

    def _parse(self, result: dict) -> list[TranscriptSegment]:
        transcription = result.get("transcription", {}) if isinstance(result, dict) else {}
        utterances = transcription.get("utterances") or []
        segments: list[TranscriptSegment] = []
        for utt in utterances:
            text = (utt.get("text") or "").strip()
            if not text:
                continue
            speaker = utt.get("speaker")
            if speaker is not None:
                speaker = f"Speaker {speaker}"
            segments.append(
                TranscriptSegment(
                    start=float(utt.get("start", 0.0)),
                    end=float(utt.get("end", utt.get("start", 0.0))),
                    text=text,
                    speaker=speaker,
                )
            )
        if not segments:
            full = (transcription.get("full_transcript") or "").strip()
            if full:
                segments.append(TranscriptSegment(start=0.0, end=0.0, text=full))
        return segments
