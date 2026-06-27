from pathlib import Path

from usage import UsageTracker


def make_tracker(tmp_path: Path, month: str = "2026-06") -> UsageTracker:
    return UsageTracker(store_path=tmp_path / "usage.json", month=month)


def test_record_accumulates_minutes_and_persists(tmp_path):
    tracker = make_tracker(tmp_path)
    tracker.record("gladia", 10.0, elapsed=30.0)
    tracker.record("gladia", 5.0, elapsed=15.0)
    assert tracker.minutes_used("gladia") == 15.0
    # Reload from disk to confirm persistence.
    reloaded = make_tracker(tmp_path)
    assert reloaded.minutes_used("gladia") == 15.0
    assert reloaded.speed("gladia") is not None


def test_is_exhausted_thresholds(tmp_path):
    tracker = make_tracker(tmp_path)
    tracker.record("gladia", 595.0)
    # 595 used + 10 upcoming > 600 free -> exhausted.
    assert tracker.is_exhausted("gladia", 600.0, 10.0) is True
    # 595 used + 3 upcoming <= 600 -> still available.
    assert tracker.is_exhausted("gladia", 600.0, 3.0) is False


def test_unknown_free_limit_never_pre_skips(tmp_path):
    tracker = make_tracker(tmp_path)
    tracker.record("gemini", 100000.0)
    assert tracker.is_exhausted("gemini", None, 60.0) is False
    assert tracker.is_exhausted("gemini", 0, 60.0) is False


def test_mark_exhausted_is_sticky(tmp_path):
    tracker = make_tracker(tmp_path)
    tracker.mark_exhausted("speechmatics")
    assert tracker.is_exhausted("speechmatics", 480.0, 1.0) is True
    assert make_tracker(tmp_path).is_exhausted("speechmatics", 480.0, 1.0) is True


def test_month_buckets_are_independent(tmp_path):
    june = make_tracker(tmp_path, month="2026-06")
    june.record("gladia", 600.0)
    june.mark_exhausted("gladia")
    july = make_tracker(tmp_path, month="2026-07")
    # New month resets usage and the exhausted flag.
    assert july.minutes_used("gladia") == 0.0
    assert july.is_exhausted("gladia", 600.0, 10.0) is False
