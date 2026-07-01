"""
memory_extractor.py — Background memory extraction agent.

Runs in a background thread after each inference turn. Classifies content
from the completed turn into semantic/episodic/procedural/working types
and persists to MongoDB with source="auto".

NEVER touches persona or archive entries.
NEVER holds a reference to any mutable Engine state.
Receives only an immutable dict snapshot of the completed turn.

Per-identity queue management:
  _extractor_queues[identity_id] is a deque(maxlen=10).
  If queue depth >= 10 for an identity, new tasks are dropped with a WARNING.

Public API:
  run(turn_snapshot: dict) -> None
      Entry point called in a background thread.
      turn_snapshot keys: identity_id, user_message, assistant_message,
                          turn_index, timestamp
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict, deque
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-identity queue (maxlen=10 per spec — excess tasks are dropped)
# ---------------------------------------------------------------------------
_extractor_queues: dict[str, deque] = defaultdict(lambda: deque(maxlen=10))

# Valid types the extractor may write — persona and archive are FORBIDDEN.
_ALLOWED_TYPES = frozenset({"semantic", "episodic", "procedural", "working"})

# Memory extraction prompt.
# Key rules baked in:
#   - Only extract facts ABOUT THE USER or their context
#   - Never record anything about the AI/model/assistant itself
#   - Never record that the model said it is/isn't an AI, assistant, etc.
_EXTRACTION_PROMPT = """\
You are a memory extraction system for a personal knowledge base. Your ONLY job is to identify and record PERMANENT FACTS ABOUT THE USER.

WHAT TO EXTRACT — facts that will still be true and useful in future conversations:
- User's name, age, location, occupation, relationships
- User's stated preferences, opinions, and habits
- Skills, knowledge domains, or goals the user mentioned
- Specific past events the user described experiencing

WHAT TO NEVER EXTRACT — this is critical, violations corrupt the memory store:
- ANYTHING the assistant said about itself (e.g. "I am an AI", "I can help", "I'm an assistant")
- ANYTHING about the assistant's capabilities, nature, or identity
- Generic greetings ("hello", "hi", "how are you")
- Questions — only extract stated facts, never questions
- Filler responses ("sure", "of course", "got it")
- Turn metadata ("the user said hello", "the assistant greeted the user")
- The fact that a conversation happened or how it started

Memory types — choose the MOST specific fit:
  "semantic"   — permanent facts about the user (name, job, location, preferences)
  "episodic"   — specific past events the user personally experienced
  "procedural" — how the user likes things done, their workflow preferences
  "working"    — temporary info only useful in this session (forget after session ends)

Output format: a JSON array. If there is NOTHING worth extracting, output exactly: []
Each item: {{"memory_type": "<type>", "content": "<third-person fact about user, max 200 chars>", "tags": ["<tag1>", "<tag2>"]}}

CONVERSATION TURN:
User: {user_message}
Assistant: {assistant_message}

Extract ONLY permanent user facts. Output JSON array only, no explanation:"""


def run(turn_snapshot: dict) -> None:
    """Entry point for background memory extraction.

    Called in a background thread by _handle_send after each inference turn.
    The turn_snapshot is an immutable copy — never holds mutable Engine refs.

    Args:
        turn_snapshot: Dict with keys:
            identity_id      (str)
            user_message     (str)
            assistant_message (str)
            turn_index       (int)
            timestamp        (str, ISO 8601)
    """
    identity_id = turn_snapshot.get("identity_id", "")
    turn_index  = turn_snapshot.get("turn_index", -1)

    try:
        _run_extraction(turn_snapshot)
    except Exception as exc:
        logger.error(
            "memory_extractor: uncaught exception for identity=%r turn=%s: %s",
            identity_id, turn_index, exc,
        )
        # Never re-raise — do not crash the main thread.


def _run_extraction(turn_snapshot: dict) -> None:
    """Inner extraction logic. All exceptions propagate to run() which catches them."""
    identity_id       = turn_snapshot.get("identity_id", "")
    user_message      = turn_snapshot.get("user_message", "")
    assistant_message = turn_snapshot.get("assistant_message", "")
    turn_index        = turn_snapshot.get("turn_index", -1)

    if not identity_id:
        logger.error("memory_extractor: missing identity_id in turn_snapshot")
        return

    # ── Queue depth check ─────────────────────────────────────────────────
    queue = _extractor_queues[identity_id]
    if len(queue) >= 10:
        logger.warning(
            "memory_extractor: queue full (>=10) for identity=%r turn=%s — task dropped",
            identity_id, turn_index,
        )
        return

    # Register task in queue (track it)
    queue.append(turn_index)

    try:
        _extract_and_persist(identity_id, user_message, assistant_message, turn_index)
    finally:
        # Remove from queue when done (deque contains turn indices)
        try:
            queue.remove(turn_index)
        except ValueError:
            pass  # already removed or wasn't in queue


def _should_reject(content: str, user_message: str, assistant_message: str) -> bool:
    """Hard code-level filter. Returns True if the entry should be dropped.

    This catches cases where the LLM ignored the prompt instructions and
    extracted things it was explicitly told not to — assistant self-descriptions,
    greetings, turn metadata, questions, etc.
    """
    c_lower = content.lower()
    u_lower = user_message.lower().strip()

    # Reject if content is about the assistant/AI/model
    ai_phrases = (
        "the assistant", "assistant is", "assistant said", "assistant greet",
        "assistant responded", "assistant replied",
        "is an ai", "is not an ai", "is a language model",
        "is designed to", "designed to assist",
        "i am an ai", "i'm an ai", "i am not an ai",
        "the ai", "ai is", "the model", "model is",
        "language model", "large language model",
        "offers assistance", "offer help", "offers help",
        "asked to help", "here to help",
    )
    for phrase in ai_phrases:
        if phrase in c_lower:
            return True

    # Reject if it's just describing that a greeting happened
    greeting_words = {"hello", "hi", "hey", "greetings", "good morning",
                      "good afternoon", "good evening", "howdy"}
    if u_lower in greeting_words or u_lower.rstrip("!.,") in greeting_words:
        # User only sent a greeting — nothing factual to extract
        return True

    # Reject turn metadata ("the user initiated", "the user said hello",
    # "the conversation started with", etc.)
    meta_phrases = (
        "initiated the conversation",
        "started the conversation",
        "began the conversation",
        "the user said hello",
        "the user greeted",
        "user initiated",
        "user started",
        "conversation with",
        "conversation with a greeting",
        "conversation with 'hello'",
        "with a greeting",
        "with a hello",
    )
    for phrase in meta_phrases:
        if phrase in c_lower:
            return True

    # Reject if content ends with a question mark (extracted a question)
    if content.rstrip().endswith("?"):
        return True

    # Reject very short entries that carry no real information
    # (less than 15 chars after stripping is almost certainly noise)
    if len(content.strip()) < 15:
        return True

    return False


def _extract_and_persist(
    identity_id: str,
    user_message: str,
    assistant_message: str,
    turn_index: int,
) -> None:
    """Call LLM for extraction, validate, and persist entries."""
    # Lazy imports to avoid circular imports at module load time.
    try:
        import openai
        import falcon.config as Config
        import falcon.memory as Memory
    except ImportError as exc:
        logger.error(
            "memory_extractor: import error for identity=%r turn=%s: %s",
            identity_id, turn_index, exc,
        )
        return

    prompt = _EXTRACTION_PROMPT.format(
        user_message=user_message[:2000],
        assistant_message=assistant_message[:2000],
    )

    # ── LLM call ─────────────────────────────────────────────────────────
    try:
        from falcon.engine import get_client
        client = get_client(Config.OPENROUTER_API_KEY, title="Falcon-MemoryExtractor")
        response = client.chat.completions.create(
            model=Config.extraction_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=1024,
        )
        raw_content = (response.choices[0].message.content or "").strip()
    except Exception as exc:
        logger.error(
            "memory_extractor: LLM call failed for identity=%r turn=%s: %s",
            identity_id, turn_index, exc,
        )
        return  # exit silently on LLM failure

    # ── Parse JSON ────────────────────────────────────────────────────────
    try:
        # Strip markdown code fences if present
        text = raw_content
        if text.startswith("```"):
            lines = text.splitlines()
            # Remove first and last fence lines
            text = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
        extracted: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.error(
            "memory_extractor: malformed JSON from LLM for identity=%r turn=%s: %s",
            identity_id, turn_index, exc,
        )
        return  # persist zero entries

    if not isinstance(extracted, list):
        logger.error(
            "memory_extractor: expected JSON array, got %s for identity=%r turn=%s",
            type(extracted).__name__, identity_id, turn_index,
        )
        return

    # ── Validate and persist each entry ──────────────────────────────────
    persisted = 0
    for item in extracted:
        if not isinstance(item, dict):
            continue

        mem_type = item.get("memory_type", "")
        content  = str(item.get("content", "")).strip()
        tags     = item.get("tags", [])

        # Enforce: NEVER write persona or archive.
        if mem_type not in _ALLOWED_TYPES:
            logger.warning(
                "memory_extractor: skipping forbidden type %r for identity=%r",
                mem_type, identity_id,
            )
            continue

        if not content:
            continue

        # ── Hard content filter — reject entries the prompt should have blocked
        # but sometimes doesn't. This is a code-level safety net.
        if _should_reject(content, user_message, assistant_message):
            logger.info(
                "memory_extractor: filtered out low-quality entry %r for identity=%r",
                content[:80], identity_id,
            )
            continue

        if not isinstance(tags, list):
            tags = []
        tags = [str(t) for t in tags[:5]]

        # ── MongoDB write ─────────────────────────────────────────────
        try:
            Memory.add_memory(
                identity_id=identity_id,
                memory_type=mem_type,  # type: ignore[arg-type]
                content=content[:10000],
                tags=tags,
                pinned=False,
                source="auto",
            )
            persisted += 1
        except Exception as exc:
            logger.error(
                "memory_extractor: MongoDB write failed for identity=%r turn=%s: %s",
                identity_id, turn_index, exc,
            )
            # Continue trying other entries — do not re-raise.

    logger.info(
        "memory_extractor: persisted %d entries for identity=%r turn=%s",
        persisted, identity_id, turn_index,
    )
