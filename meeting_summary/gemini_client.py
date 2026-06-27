"""Low-level Gemini access: multi-key client pool, retry, and generation.

Shared by the Gemini STT provider (providers/gemini.py) and the summarizer
(summarize.py). Knows nothing about meetings or transcripts.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Sequence, TypeVar

from common import EmptyResponseError, MeetingSummaryError, log
from config import resolve_fallback_model
from media import format_bytes

T = TypeVar("T")

REQUEST_TIMEOUT_MS = 180_000
GENERATION_TEMPERATURE = 0.3
DEFAULT_MAX_OUTPUT_TOKENS = 65536
GOOD_FINISH_REASONS = {"STOP", "FINISH_REASON_UNSPECIFIED"}


def make_client(api_key: str):
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise MeetingSummaryError(
            "Missing Python dependency. Run: "
            "python -m pip install -r meeting_summary/requirements.txt"
        ) from exc
    log("Gemini client initialized.")
    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS),
    )
    return client, types


class ClientPool:
    """Holds several Gemini API keys and rotates to the next one when the
    active key hits a rate/quota limit.

    Attribute access (``.models``, ``.files``, ...) is proxied to the current
    underlying google-genai client, so existing call sites can keep using this
    object exactly like a normal client.
    """

    def __init__(self, api_keys: Sequence[str]):
        if not api_keys:
            raise MeetingSummaryError("ClientPool requires at least one API key.")
        self._keys = list(api_keys)
        self._index = 0
        self._client = None
        self.types = None
        self._build()

    def _build(self) -> None:
        self._client, self.types = make_client(self._keys[self._index])
        log(f"Using Gemini API key #{self._index + 1} of {len(self._keys)}.")

    def __getattr__(self, name: str):
        # Only reached for attributes not set on the instance (e.g. models,
        # files); forward them to the active client.
        return getattr(self.__dict__["_client"], name)

    @property
    def has_next_key(self) -> bool:
        return self._index + 1 < len(self._keys)

    def rotate(self) -> bool:
        """Switch to the next API key. Returns False if none are left."""
        if not self.has_next_key:
            log("Rate limit hit but no more Gemini API keys to switch to.")
            return False
        self._index += 1
        log(
            f"Rate limit hit; switching to Gemini API key "
            f"#{self._index + 1} of {len(self._keys)}."
        )
        self._build()
        return True


def is_rate_limited(exc: Exception) -> bool:
    """True when an error reflects a per-key quota / rate limit, which is
    recoverable by switching to a different API key."""
    text = str(exc).upper()
    return any(
        marker in text
        for marker in ("429", "RESOURCE_EXHAUSTED", "QUOTA", "RATE LIMIT")
    )


def generation_config(types, max_output_tokens: int):
    return types.GenerateContentConfig(
        temperature=GENERATION_TEMPERATURE,
        max_output_tokens=max_output_tokens,
    )


def retry(
    operation: Callable[[], T],
    description: str,
    attempts: int,
    pool: "ClientPool | None" = None,
) -> T:
    attempts = max(1, attempts)
    last_error: Exception | None = None
    attempt = 0
    while attempt < attempts:
        attempt += 1
        try:
            log(f"{description}: attempt {attempt}/{attempts}.")
            return operation()
        except Exception as exc:  # SDK exceptions vary by transport.
            last_error = exc
            # On a per-key quota / rate limit, switch API keys and retry
            # without burning an attempt; the operation closes over the pool
            # so the next call uses the new key automatically.
            if pool is not None and is_rate_limited(exc) and pool.rotate():
                log(f"Retrying {description} with the next API key.")
                attempt -= 1
                continue
            if attempt == attempts:
                break
            delay = min(2 ** (attempt - 1), 8)
            log(
                f"{description} failed on attempt {attempt}/{attempts}; "
                f"retrying in {delay}s... ({exc})"
            )
            time.sleep(delay)
    raise MeetingSummaryError(
        f"{description} failed after {attempts} attempt(s): {last_error}"
    ) from last_error


def file_state_name(file_obj) -> str | None:
    state = getattr(file_obj, "state", None)
    if state is None:
        return None
    name = getattr(state, "name", None) or str(state)
    return name.rsplit(".", 1)[-1].upper()


def wait_until_active(client, file_obj, timeout_seconds: int = 300):
    deadline = time.monotonic() + timeout_seconds
    current = file_obj
    while True:
        state_name = file_state_name(current)
        if state_name in (None, "ACTIVE"):
            log(f"Uploaded file is ready: {getattr(current, 'name', 'unknown')}")
            return current
        if state_name in ("FAILED", "ERROR"):
            raise MeetingSummaryError(f"Uploaded file processing failed: {state_name}")
        if time.monotonic() >= deadline:
            raise MeetingSummaryError(
                f"Timed out waiting for uploaded file to become ACTIVE: {state_name}"
            )
        log(
            "Waiting for uploaded file processing: "
            f"{getattr(current, 'name', 'unknown')} state={state_name}"
        )
        time.sleep(5)
        current = client.files.get(name=current.name)


def finish_reason_label(response) -> str | None:
    candidates = getattr(response, "candidates", None)
    if not candidates:
        return None
    reason = getattr(candidates[0], "finish_reason", None)
    if reason is None:
        return None
    return getattr(reason, "name", None) or str(reason)


def extract_response_text(response) -> str:
    reason = finish_reason_label(response)
    try:
        text = getattr(response, "text", None)
    except Exception:  # SDK may raise when the candidate carries no text parts.
        text = None
    # A non-STOP reason means the output was truncated/degenerate (e.g.
    # MAX_TOKENS repeat-loop) or malformed, even when some text came back.
    if reason and reason.upper() not in GOOD_FINISH_REASONS:
        raise EmptyResponseError(
            f"Gemini stopped with finishReason={reason} "
            "(truncated or degenerate output)."
        )
    if text and text.strip():
        return strip_outer_fence(text.strip())
    detail = f" (finishReason={reason})" if reason else ""
    raise EmptyResponseError(f"Gemini returned an empty response{detail}.")


def strip_outer_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```") or not stripped.endswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) >= 3:
        return "\n".join(lines[1:-1]).strip()
    return stripped


def should_failover_fast(exc: Exception) -> bool:
    """Transient capacity / hang errors where retrying the same model is futile,
    so we switch to the fallback model immediately instead of burning retries."""
    text = str(exc).upper()
    markers = (
        "503",
        "UNAVAILABLE",
        "429",
        "RESOURCE_EXHAUSTED",
        "OVERLOADED",
        "TIMEOUT",
        "DEADLINE",
    )
    return any(marker in text for marker in markers)


def generate_text(client, model: str, contents, config, retries: int) -> str:
    models_to_try = [model]
    fallback = resolve_fallback_model(model)
    if fallback is not None:
        models_to_try.append(fallback)
    attempts = max(1, retries)

    last_error: Exception | None = None
    for index, current_model in enumerate(models_to_try):
        has_next_model = index < len(models_to_try) - 1
        if index > 0:
            log(
                f"Falling back to model {current_model} after "
                f"model {models_to_try[index - 1]} failed."
            )
        attempt = 0
        while attempt < attempts:
            attempt += 1
            try:
                log(
                    f"Gemini content generation ({current_model}): "
                    f"attempt {attempt}/{attempts}."
                )
                response = client.models.generate_content(
                    model=current_model,
                    contents=contents,
                    config=config,
                )
                return extract_response_text(response)
            except Exception as exc:  # SDK exceptions vary by transport.
                last_error = exc
                # A per-key quota / rate limit recovers by switching API keys,
                # not by retrying or changing models. Try the next key first,
                # keeping the same model and without burning a retry attempt.
                if (
                    is_rate_limited(exc)
                    and isinstance(client, ClientPool)
                    and client.rotate()
                ):
                    log(f"Retrying model {current_model} with the next API key.")
                    attempt -= 1
                    continue
                # An overloaded/unresponsive model won't recover by retrying it;
                # jump straight to the fallback model if one is left.
                if has_next_model and should_failover_fast(exc):
                    log(
                        f"{current_model} is overloaded/unresponsive ({exc}); "
                        "switching to fallback model now."
                    )
                    break
                if attempt == attempts:
                    break
                delay = min(2 ** (attempt - 1), 8)
                log(
                    f"Gemini content generation ({current_model}) failed on "
                    f"attempt {attempt}/{attempts}; retrying in {delay}s... ({exc})"
                )
                time.sleep(delay)

    raise MeetingSummaryError(
        f"Gemini content generation failed for model(s) "
        f"{', '.join(models_to_try)}: {last_error}"
    )


def generate_from_file(
    client,
    types,
    model: str,
    chunk_path: Path,
    prompt: str,
    max_output_tokens: int,
    retries: int,
) -> str:
    uploaded = None
    try:
        log(
            f"Uploading chunk file: {chunk_path.name} "
            f"({format_bytes(chunk_path.stat().st_size)})"
        )
        uploaded = retry(
            lambda: client.files.upload(file=str(chunk_path)),
            "Gemini file upload",
            retries,
            pool=client if isinstance(client, ClientPool) else None,
        )
        uploaded = wait_until_active(client, uploaded)
        config = generation_config(types, max_output_tokens)
        log(f"Generating content with model {model} from uploaded chunk...")
        text = generate_text(client, model, [prompt, uploaded], config, retries)
        log(f"Gemini returned {len(text):,} character(s) for chunk file.")
        return text
    finally:
        if uploaded is not None:
            try:
                client.files.delete(name=uploaded.name)
                log(f"Deleted uploaded Gemini file: {uploaded.name}")
            except Exception as exc:
                log(
                    f"Warning: could not delete uploaded file {uploaded.name}: {exc}",
                )


def generate_from_text(
    client,
    types,
    model: str,
    prompt: str,
    max_output_tokens: int,
    retries: int,
) -> str:
    config = generation_config(types, max_output_tokens)
    log(
        f"Generating content with model {model} from text prompt "
        f"({len(prompt):,} character(s))..."
    )
    text = generate_text(client, model, prompt, config, retries)
    log(f"Gemini returned {len(text):,} character(s) from text prompt.")
    return text
