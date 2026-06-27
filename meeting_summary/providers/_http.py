"""Shared HTTP helpers for the requests-based external STT providers."""

from __future__ import annotations

from .base import ProviderError, QuotaExceeded

# HTTP statuses that mean "out of free credits / over the rate limit" rather
# than a transient or auth error — these mark the provider exhausted.
QUOTA_STATUSES = {402, 429}
_QUOTA_MARKERS = ("quota", "credit", "insufficient", "exceeded", "limit", "balance")


def require_requests():
    try:
        import requests  # noqa: F401
    except ImportError as exc:  # pragma: no cover - import-time guard
        raise ProviderError(
            "The 'requests' package is required for HTTP-based STT providers. "
            "Run: python -m pip install -r meeting_summary/requirements.txt"
        ) from exc
    return requests


def raise_for_status(provider: str, response, context: str) -> None:
    """Translate an error response into Quota/Provider errors.

    402/429, plus 403 bodies that mention quota/credit wording, are treated as
    free-tier exhaustion; everything else is a generic provider error.
    """
    if response.ok:
        return
    body = ""
    try:
        body = response.text or ""
    except Exception:  # pragma: no cover - defensive
        body = ""
    snippet = body.strip()[:500]
    status = response.status_code
    lowered = snippet.lower()
    is_quota = status in QUOTA_STATUSES or (
        status == 403 and any(marker in lowered for marker in _QUOTA_MARKERS)
    )
    message = f"{provider} {context} failed: HTTP {status} {snippet}".strip()
    if is_quota:
        raise QuotaExceeded(message)
    raise ProviderError(message)
