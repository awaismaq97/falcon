"""
summarizer.py — Conversation Summarization for Falcon.

After every inference turn, the full conversation for an identity is
summarized by an LLM and the result is persisted in MongoDB.

The summary is stored in the 'conversation_summaries' collection:
  {
    identity_id:  str,
    summary:      str,   -- AI-generated summary of the full conversation so far
    turn_count:   int,   -- number of turns at time of summary
    updated_at:   str,   -- ISO 8601 UTC timestamp
  }

One document per identity (upserted on every update).

Public API:
  update_summary(identity_id, history, model, api_key) -> str | None
      Calls the LLM to summarize history, persists to DB, returns summary text.

  get_summary(identity_id) -> str | None
      Returns the stored summary for identity_id, or None if not present.

  delete_summary(identity_id) -> None
      Removes the summary for identity_id (called on clear/delete).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from falcon.db import get_db

logger = logging.getLogger(__name__)

_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

_SUMMARY_SYSTEM_PROMPT = """\
You are a conversation summarizer. Your job is to produce a concise but \
complete summary of the conversation history provided. The summary will be \
injected into future inference calls as context, so it must preserve:
- All factual information exchanged
- The user's stated goals, preferences, and context
- Key decisions or conclusions reached
- Important entities, names, or references mentioned

Rules:
- Write in third-person, neutral, factual prose.
- Do NOT editorialize or add commentary.
- Do NOT include meta-commentary about the conversation structure itself.
- Keep it as brief as possible while retaining all meaningful content.
- If the conversation is very short (1-2 turns), a single short paragraph suffices.
- Output ONLY the summary text — no preamble, no "Here is a summary:", just the text.\
"""

_SUMMARY_USER_TEMPLATE = """\
Summarize the following conversation history:

{conversation_text}

Output only the summary:\
"""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_conversation_text(history: list[dict]) -> str:
    """Convert history list to a readable conversation transcript."""
    lines: list[str] = []
    for entry in history:
        role = entry.get("role", "user")
        content = entry.get("content", "").strip()
        if not content:
            continue
        speaker = "User" if role == "user" else "Assistant"
        lines.append(f"{speaker}: {content}")
    return "\n\n".join(lines)


def update_summary(
    identity_id: str,
    history: list[dict],
    model: str,
    api_key: str,
) -> str | None:
    """Summarize the full conversation history and persist to MongoDB.

    Called in a background thread after each inference turn. Never raises —
    all exceptions are caught and logged.

    Args:
        identity_id: The identity whose conversation is being summarized.
        history:     Full conversation history (list of {role, content} dicts).
        model:       The model to use for summarization.
        api_key:     OpenRouter API key.

    Returns:
        The summary text on success, None on failure.
    """
    if not history:
        logger.info("summarizer: empty history for identity=%r — skipping", identity_id)
        return None

    conversation_text = _build_conversation_text(history)
    if not conversation_text.strip():
        return None

    # Count turns (user messages)
    turn_count = sum(1 for e in history if e.get("role") == "user")

    try:
        from falcon.engine import get_client
        client = get_client(api_key, title="Falcon-Summarizer")
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": _SUMMARY_USER_TEMPLATE.format(
                        conversation_text=conversation_text[:12000]
                    ),
                },
            ],
            temperature=0.2,
            max_tokens=1024,
        )
        summary_text = (response.choices[0].message.content or "").strip()
    except Exception as exc:
        logger.error(
            "summarizer: LLM call failed for identity=%r: %s", identity_id, exc
        )
        return None

    if not summary_text:
        logger.warning("summarizer: empty summary returned for identity=%r", identity_id)
        return None

    # Persist to MongoDB — one document per identity (upsert)
    try:
        db = get_db()
        db["conversation_summaries"].update_one(
            {"identity_id": identity_id},
            {
                "$set": {
                    "identity_id": identity_id,
                    "summary": summary_text,
                    "turn_count": turn_count,
                    "updated_at": _utc_now_iso(),
                }
            },
            upsert=True,
        )
        logger.info(
            "summarizer: summary updated for identity=%r (%d turns, %d chars)",
            identity_id,
            turn_count,
            len(summary_text),
        )
    except Exception as exc:
        logger.error(
            "summarizer: MongoDB write failed for identity=%r: %s", identity_id, exc
        )
        return None

    return summary_text


def get_summary(identity_id: str) -> str | None:
    """Return the stored conversation summary for identity_id.

    Args:
        identity_id: The identity to retrieve the summary for.

    Returns:
        Summary text string, or None if no summary exists.
    """
    try:
        db = get_db()
        doc = db["conversation_summaries"].find_one(
            {"identity_id": identity_id},
            {"_id": 0, "summary": 1},
        )
        if doc:
            return doc.get("summary") or None
        return None
    except Exception as exc:
        logger.error(
            "summarizer: get_summary failed for identity=%r: %s", identity_id, exc
        )
        return None


def get_summary_doc(identity_id: str) -> dict | None:
    """Return the full summary document (summary, turn_count, updated_at) or None."""
    try:
        db = get_db()
        doc = db["conversation_summaries"].find_one(
            {"identity_id": identity_id},
            {"_id": 0},
        )
        return doc or None
    except Exception as exc:
        logger.error(
            "summarizer: get_summary_doc failed for identity=%r: %s", identity_id, exc
        )
        return None


def delete_summary(identity_id: str) -> None:
    """Delete the stored summary for identity_id.

    Called when a conversation is cleared or an identity is deleted.
    No-op if no summary exists.

    Args:
        identity_id: The identity whose summary should be deleted.
    """
    try:
        db = get_db()
        db["conversation_summaries"].delete_one({"identity_id": identity_id})
        logger.info("summarizer: summary deleted for identity=%r", identity_id)
    except Exception as exc:
        logger.error(
            "summarizer: delete_summary failed for identity=%r: %s", identity_id, exc
        )
