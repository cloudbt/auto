"""Local free-quota and speed tracking for STT providers.

Persists a small JSON file bucketed by calendar month. Used to (a) pre-skip a
provider whose known monthly free minutes are already used up, and (b) record a
rolling transcription speed (``sec_per_min``) for the ``--benchmark`` view.

This is a best-effort local estimate, not authoritative billing data. The
orchestrator still falls over reactively when a provider itself reports a quota
error (which calls :meth:`UsageTracker.mark_exhausted`).
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from common import log


def default_store_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        return Path(base) / "meeting_summary" / "usage.json"
    return Path.home() / ".cache" / "meeting_summary" / "usage.json"


def current_month() -> str:
    return datetime.now().strftime("%Y-%m")


class UsageTracker:
    def __init__(self, store_path: Path | None = None, month: str | None = None):
        self.store_path = store_path or default_store_path()
        self.month = month or current_month()
        self._data = self._load()

    def _load(self) -> dict:
        try:
            raw = self.store_path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self) -> None:
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            self.store_path.write_text(
                json.dumps(self._data, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except OSError as exc:  # Tracking is best-effort; never abort a job.
            log(f"Warning: could not write usage store {self.store_path}: {exc}")

    def _bucket(self) -> dict:
        return self._data.setdefault(self.month, {})

    def _provider(self, name: str) -> dict:
        return self._bucket().setdefault(
            name, {"minutes": 0.0, "exhausted": False, "sec_per_min": None}
        )

    def is_exhausted(
        self, name: str, free_minutes: float | None, upcoming_minutes: float
    ) -> bool:
        """True if the provider should be skipped before even trying it.

        Skips when it was flagged exhausted this month, or when its known free
        allowance (``free_minutes``) would be exceeded by this job. A free limit
        of ``None`` (unknown/unlimited) never pre-skips.
        """
        record = self._provider(name)
        if record.get("exhausted"):
            return True
        if not free_minutes or free_minutes <= 0:
            return False
        used = float(record.get("minutes", 0.0))
        return used + upcoming_minutes > free_minutes

    def record(self, name: str, minutes: float, elapsed: float | None = None) -> None:
        record = self._provider(name)
        record["minutes"] = float(record.get("minutes", 0.0)) + float(minutes)
        if elapsed is not None and minutes > 0:
            sec_per_min = elapsed / minutes
            prior = record.get("sec_per_min")
            # Exponential moving average so the figure tracks recent runs.
            record["sec_per_min"] = (
                sec_per_min if prior is None else round(0.5 * prior + 0.5 * sec_per_min, 3)
            )
        self._save()

    def mark_exhausted(self, name: str) -> None:
        self._provider(name)["exhausted"] = True
        self._save()
        log(f"Marked provider '{name}' as free-tier exhausted for {self.month}.")

    def speed(self, name: str) -> float | None:
        return self._provider(name).get("sec_per_min")

    def minutes_used(self, name: str) -> float:
        return float(self._provider(name).get("minutes", 0.0))
