"""Environment/config resolution: .env loading, API keys, provider ordering."""

from __future__ import annotations

import os
import re
from pathlib import Path

from common import MeetingSummaryError

SCRIPT_DIR = Path(__file__).resolve().parent

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
DEFAULT_GEMINI_FALLBACK_MODEL = "gemini-3.5-flash"
# Cost-priority default: free tiers first, Gemini last as the always-available
# fallback (its own multi-key rotation handles per-key limits).
DEFAULT_PROVIDER_ORDER = ("gladia", "speechmatics", "gemini")
DEFAULT_LANGUAGE = "ja"


def load_local_env() -> None:
    env_path = SCRIPT_DIR / ".env"
    if not env_path.exists():
        return

    try:
        from dotenv import load_dotenv
    except ImportError:
        load_dotenv = None

    if load_dotenv is not None:
        load_dotenv(env_path, override=False)
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def resolve_gemini_keys(required: bool = True) -> list[str]:
    """Collect every configured Gemini API key, in priority order.

    Supports a single ``GEMINI_API_KEY`` as well as numbered variants
    (``GEMINI_API_KEY1``, ``GEMINI_API_KEY2``, ...). The unnumbered key is
    tried first, then numbered keys in ascending order. Duplicates and blanks
    are dropped. Multiple keys let us fail over when one hits a rate/quota
    limit. With ``required=False`` an empty list is returned instead of raising
    (used to detect whether the Gemini provider is configured at all).
    """
    pattern = re.compile(r"^GEMINI_API_KEY(\d*)$")
    found: list[tuple[int, str]] = []
    for name, value in os.environ.items():
        match = pattern.match(name)
        if match is None:
            continue
        key = value.strip()
        if not key:
            continue
        order = int(match.group(1)) if match.group(1) else 0
        found.append((order, key))
    found.sort(key=lambda item: item[0])

    keys: list[str] = []
    seen: set[str] = set()
    for _, key in found:
        if key not in seen:
            seen.add(key)
            keys.append(key)

    if not keys and required:
        raise MeetingSummaryError(
            "No Gemini API key is set. Create meeting_summary/.env from "
            ".env.example or set GEMINI_API_KEY (or GEMINI_API_KEY1, "
            "GEMINI_API_KEY2, ...)."
        )
    return keys


def resolve_model(cli_model: str | None) -> str:
    return (
        cli_model
        or os.environ.get("GEMINI_MODEL", "").strip()
        or DEFAULT_GEMINI_MODEL
    )


def resolve_fallback_model(model: str) -> str | None:
    fallback = (
        os.environ.get("GEMINI_FALLBACK_MODEL", "").strip()
        or DEFAULT_GEMINI_FALLBACK_MODEL
    )
    if not fallback or fallback == model:
        return None
    return fallback


def resolve_provider_order(cli_order: str | None = None) -> list[str]:
    """Ordered list of STT provider names to try, lowercased and de-duplicated."""
    raw = (cli_order or os.environ.get("STT_PROVIDER_ORDER", "")).strip()
    names = [part.strip().lower() for part in raw.split(",") if part.strip()]
    if not names:
        names = list(DEFAULT_PROVIDER_ORDER)
    ordered: list[str] = []
    seen: set[str] = set()
    for name in names:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def resolve_language(cli_language: str | None = None) -> str:
    return (
        (cli_language or "").strip()
        or os.environ.get("MEETING_LANGUAGE", "").strip()
        or DEFAULT_LANGUAGE
    )


def free_minutes_override(provider_name: str) -> float | None:
    """Read ``<PROVIDER>_FREE_MINUTES`` env override, if any."""
    raw = os.environ.get(f"{provider_name.upper()}_FREE_MINUTES", "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None
