from pathlib import Path

import pytest

from common import MeetingSummaryError
from providers import AudioSource, transcribe_with_fallback
from providers.base import (
    ProviderError,
    QuotaExceeded,
    TranscriptionProvider,
    TranscriptResult,
    TranscriptSegment,
)
from usage import UsageTracker


class FakeProvider(TranscriptionProvider):
    def __init__(self, name, behavior="ok", configured=True, free=None):
        super().__init__()
        self.name = name
        self.default_free_minutes = free
        self._behavior = behavior
        self._configured = configured
        self.calls = 0

    def is_configured(self):
        return self._configured

    def transcribe(self, audio, *, language, progress):
        self.calls += 1
        if self._behavior == "ok":
            return TranscriptResult(self.name, [TranscriptSegment(0.0, 1.0, "hello")])
        if self._behavior == "quota":
            raise QuotaExceeded(f"{self.name} out of credits")
        raise ProviderError(f"{self.name} failed")


def make_audio(minutes=60.0):
    return AudioSource(Path("x.mp3"), duration_s=minutes * 60, chunks=[])


def tracker(tmp_path):
    return UsageTracker(store_path=tmp_path / "usage.json", month="2026-06")


def test_first_provider_wins(tmp_path):
    a, b = FakeProvider("a"), FakeProvider("b")
    result = transcribe_with_fallback(
        make_audio(), [a, b], tracker(tmp_path), language="ja"
    )
    assert result.provider == "a"
    assert b.calls == 0


def test_quota_falls_over_and_marks_exhausted(tmp_path):
    usage = tracker(tmp_path)
    a, b = FakeProvider("a", behavior="quota"), FakeProvider("b")
    result = transcribe_with_fallback(make_audio(), [a, b], usage, language="ja")
    assert result.provider == "b"
    assert a.calls == 1
    assert usage.is_exhausted("a", a.free_minutes(), 1.0) is True


def test_provider_error_falls_over_without_marking(tmp_path):
    usage = tracker(tmp_path)
    a, b = FakeProvider("a", behavior="error"), FakeProvider("b")
    result = transcribe_with_fallback(make_audio(), [a, b], usage, language="ja")
    assert result.provider == "b"
    # Generic errors do not flag the month as exhausted.
    assert usage.is_exhausted("a", a.free_minutes(), 1.0) is False


def test_unconfigured_provider_is_skipped(tmp_path):
    a = FakeProvider("a", configured=False)
    b = FakeProvider("b")
    result = transcribe_with_fallback(make_audio(), [a, b], tracker(tmp_path), language="ja")
    assert result.provider == "b"
    assert a.calls == 0


def test_locally_exhausted_provider_is_pre_skipped(tmp_path):
    usage = tracker(tmp_path)
    usage.record("a", 600.0)  # at the free limit
    a = FakeProvider("a", free=600.0)
    b = FakeProvider("b")
    result = transcribe_with_fallback(make_audio(60), [a, b], usage, language="ja")
    assert result.provider == "b"
    assert a.calls == 0  # never even attempted


def test_all_failing_raises(tmp_path):
    a = FakeProvider("a", behavior="quota")
    b = FakeProvider("b", behavior="error")
    with pytest.raises(MeetingSummaryError):
        transcribe_with_fallback(make_audio(), [a, b], tracker(tmp_path), language="ja")
