"""Meeting-notes summarization: turn a transcript into 会议纪要 + TODO list.

Uses Gemini (with the shared multi-key ClientPool). Kept separate from the STT
layer so the summarizer is provider-agnostic about how the transcript was made.
"""

from __future__ import annotations

from common import log
from gemini_client import ClientPool, generate_from_text


def final_notes_prompt(source_label: str, source_text: str) -> str:
    return f"""
Create final meeting notes from the {source_label} below.

Output only Markdown with these exact sections:

## Summary

## Action Items (TODO)
| Owner | Action | Due | Context/Timestamp |
|---|---|---|---|

Rules:
- Follow the meeting's dominant spoken language.
- If the meeting is genuinely mixed Japanese/Chinese, keep mixed wording naturally.
- Summary should be concise but include key topics, decisions, risks, and open questions.
- Action Items must be concrete, actionable TODO tasks only.
- Use TBD for unknown owner or due date.
- Context/Timestamp should include the most relevant timestamp when available.
- If there are no action items, keep the table header and add one row: | TBD | None identified | TBD | TBD |

Source:
{source_text}
""".strip()


def summarize_transcript(
    transcript_text: str,
    *,
    keys: list[str],
    model: str,
    max_output_tokens: int,
    retries: int,
    source_label: str = "transcript",
    client: ClientPool | None = None,
) -> str:
    """Produce the Summary + Action Items (TODO) Markdown from a transcript.

    Pass an existing ``client`` to reuse a ClientPool already rotated past
    exhausted keys; otherwise a fresh pool is built from ``keys``.
    """
    pool = client or ClientPool(keys)
    log(
        f"Creating final meeting notes from {len(transcript_text):,} "
        "character(s) of transcript..."
    )
    return generate_from_text(
        pool,
        pool.types,
        model,
        final_notes_prompt(source_label, transcript_text),
        max_output_tokens,
        retries,
    )
