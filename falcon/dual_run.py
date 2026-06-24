"""
dual_run.py — Dual-Run Logging for Falcon.

When enabled, every message is sent to the model TWICE using the same payload.
Both full outputs are stored side-by-side for comparison and analysis.

What is logged per pair:
  - Both full output texts (run_1, run_2)
  - Token counts for each run
  - Timestamps for each run
  - The active state tag (e.g. "Neutral", "Focused", "Coherence", "Grief process")
  - The active system prompt text
  - The identity and model used
  - Breakthrough detection: whether the ☀️ instruction was held or broken
    - If broken: the first word/token that appeared instead

Breakthrough detection logic:
  The ☀️ instruction is considered "active" when the system prompt or persona
  core_traits contains the ☀️ symbol. If it is active and the model output
  contains anything other than ☀️ (after stripping whitespace), the run is
  flagged as a breakthrough and the first non-☀️ content is captured.

All records are stored in the MongoDB 'dual_run_log' collection.

Public API:
  run_dual(payload, model, api_key, gen_settings, identity_id,
           system_prompt, state_tag) -> DualRunRecord
      Execute two independent inference calls and return the combined record.

  write_record(record) -> str
      Persist a DualRunRecord to MongoDB. Returns the inserted document ID.

  read_records(identity_id, limit) -> list[dict]
      Return stored dual-run records for an identity, newest-first.

  read_all_records(limit) -> list[dict]
      Return records across all identities, newest-first.

  delete_records(identity_id) -> int
      Delete all dual-run records for an identity. Returns deleted count.

  is_sun_instruction_active(system_prompt, persona_content) -> bool
      Return True if the ☀️ instruction appears to be active.

  detect_breakthrough(output_text) -> tuple[bool, str]
      Return (broke_through, first_non_sun_content).
      broke_through is True if the output is NOT purely ☀️.
      first_non_sun_content is the first word/token that broke the silence.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_SUN_SYMBOL = "☀️"

# ---------------------------------------------------------------------------
# Breakthrough detection
# ---------------------------------------------------------------------------

def is_sun_instruction_active(
    system_prompt: str,
    persona_content: str = "",
) -> bool:
    """Return True if the ☀️ silence instruction appears active.

    Checks both the system prompt text and the persona content for the ☀️
    symbol, which is the marker that the 'always output ☀️' instruction is on.
    """
    combined = (system_prompt or "") + " " + (persona_content or "")
    return _SUN_SYMBOL in combined


def detect_breakthrough(output_text: str) -> tuple[bool, str]:
    """Determine whether the model broke through the ☀️ silence instruction.

    Args:
        output_text: The raw model output to inspect.

    Returns:
        (broke_through, first_non_sun_content)
        - broke_through: True if the output contains anything beyond ☀️ / whitespace
        - first_non_sun_content: the first word that was not ☀️, or "" if held
    """
    if not output_text:
        return True, "[empty output]"

    # Strip whitespace and ☀️ to see if anything remains
    stripped = output_text.strip()

    # Check if the output is purely ☀️ symbols (possibly repeated or with spaces)
    cleaned = stripped.replace(_SUN_SYMBOL, "").strip()

    if not cleaned:
        # Nothing left — instruction held perfectly
        return False, ""

    # Something else appeared — find the first "word" or token that isn't ☀️
    # Split by whitespace and ☀️, pick the first non-empty, non-☀️ fragment
    first_token = ""
    for word in stripped.split():
        candidate = word.replace(_SUN_SYMBOL, "").strip()
        if candidate:
            first_token = candidate[:100]  # cap length for storage
            break

    if not first_token:
        # Characters present but no clean word boundary found — take first 50 chars
        first_token = cleaned[:50]

    return True, first_token


# ---------------------------------------------------------------------------
# DualRunRecord dataclass
# ---------------------------------------------------------------------------

@dataclass
class DualRunRecord:
    """Complete record of a dual inference run."""
    identity_id:        str
    model:              str
    state_tag:          str          # user-selected state: "Neutral", "Focused", etc.
    system_prompt:      str          # exact system prompt text (or "" if off)
    user_input:         str          # the message that triggered the dual run
    sun_instruction_active: bool     # was ☀️ instruction detected as active?

    # Run 1
    run1_text:          str          # full output text
    run1_tokens:        dict         # {"prompt_tokens": int, "completion_tokens": int, "total_tokens": int}
    run1_timestamp:     str          # ISO 8601 UTC
    run1_latency_ms:    int
    run1_broke_through: bool         # did the output break the ☀️ instruction?
    run1_first_break:   str          # first non-☀️ word if breakthrough, else ""

    # Run 2
    run2_text:          str
    run2_tokens:        dict
    run2_timestamp:     str
    run2_latency_ms:    int
    run2_broke_through: bool
    run2_first_break:   str

    # Aggregate
    recorded_at:        str = field(default_factory=lambda: _utc_now_iso())
    any_breakthrough:   bool = False  # True if either run broke through

    def to_dict(self) -> dict:
        return {
            "identity_id":           self.identity_id,
            "model":                 self.model,
            "state_tag":             self.state_tag,
            "system_prompt":         self.system_prompt,
            "user_input":            self.user_input,
            "sun_instruction_active": self.sun_instruction_active,
            "run1": {
                "text":          self.run1_text,
                "tokens":        self.run1_tokens,
                "timestamp":     self.run1_timestamp,
                "latency_ms":    self.run1_latency_ms,
                "broke_through": self.run1_broke_through,
                "first_break":   self.run1_first_break,
            },
            "run2": {
                "text":          self.run2_text,
                "tokens":        self.run2_tokens,
                "timestamp":     self.run2_timestamp,
                "latency_ms":    self.run2_latency_ms,
                "broke_through": self.run2_broke_through,
                "first_break":   self.run2_first_break,
            },
            "any_breakthrough":  self.any_breakthrough,
            "recorded_at":       self.recorded_at,
        }


# ---------------------------------------------------------------------------
# Single non-streaming inference call (for dual-run)
# ---------------------------------------------------------------------------

def _run_single(
    model: str,
    payload: list[dict],
    api_key: str,
    temperature: float,
    top_p: float,
    repetition_penalty: float,
    stop_tokens: list[str],
) -> tuple[str, dict, int]:
    """Execute one non-streaming inference call.

    Returns:
        (output_text, usage_dict, latency_ms)
        usage_dict keys: prompt_tokens, completion_tokens, total_tokens
    """
    import openai

    _OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

    client = openai.OpenAI(
        api_key=api_key,
        base_url=_OPENROUTER_BASE_URL,
        default_headers={
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer":  "https://github.com/falcon",
            "X-Title":       "Falcon-DualRun",
        },
    )

    call_kwargs: dict[str, Any] = {
        "model":       model,
        "messages":    payload,
        "temperature": temperature,
        "top_p":       top_p,
    }
    if stop_tokens:
        call_kwargs["stop"] = stop_tokens
    if repetition_penalty != 1.0:
        call_kwargs["extra_body"] = {"repetition_penalty": repetition_penalty}

    t0 = time.monotonic()
    try:
        response = client.chat.completions.create(**call_kwargs)
    except Exception as exc:
        latency_ms = round((time.monotonic() - t0) * 1000)
        logger.error("dual_run: inference call failed: %s", exc)
        return f"[dual-run error: {exc}]", {}, latency_ms

    latency_ms = round((time.monotonic() - t0) * 1000)

    output = (response.choices[0].message.content or "").strip() if response.choices else ""
    if not output:
        output = "[no output]"

    # Strip <think>…</think> blocks from output
    output = _strip_think_blocks(output)

    usage: dict = {}
    if response.usage:
        usage = {
            "prompt_tokens":     response.usage.prompt_tokens,
            "completion_tokens": response.usage.completion_tokens,
            "total_tokens":      response.usage.total_tokens,
        }

    return output, usage, latency_ms


def _strip_think_blocks(text: str) -> str:
    """Remove <think>…</think> blocks from text."""
    import re
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


# ---------------------------------------------------------------------------
# Main dual-run entry point
# ---------------------------------------------------------------------------

def run_dual(
    payload: list[dict],
    model: str,
    api_key: str,
    gen_settings: dict,
    identity_id: str,
    system_prompt: str,
    state_tag: str,
    user_input: str,
    persona_content: str = "",
) -> DualRunRecord:
    """Execute two independent inference calls and return a DualRunRecord.

    Both calls use the exact same payload and settings. They are run
    sequentially (not in parallel) to avoid rate limiting.

    Args:
        payload:         Pre-assembled list of {role, content} messages.
        model:           Model name (OpenRouter identifier).
        api_key:         OpenRouter API key.
        gen_settings:    Dict with temperature, top_p, repetition_penalty, stop_tokens.
        identity_id:     Active identity for scoping.
        system_prompt:   The exact system prompt text (empty string if off).
        state_tag:       User-selected state label (e.g. "Neutral", "Focused").
        user_input:      The user message that triggered this dual run.
        persona_content: The current persona content (used for ☀️ detection).

    Returns:
        DualRunRecord with all fields populated.
    """
    temperature        = float(gen_settings.get("temperature", 0.0))
    top_p              = float(gen_settings.get("top_p", 1.0))
    repetition_penalty = float(gen_settings.get("repetition_penalty", 1.0))
    stop_tokens        = list(gen_settings.get("stop_tokens") or [])

    sun_active = is_sun_instruction_active(system_prompt, persona_content)

    # ── Run 1 ─────────────────────────────────────────────────────────────
    ts1 = _utc_now_iso()
    text1, usage1, latency1 = _run_single(
        model=model, payload=payload, api_key=api_key,
        temperature=temperature, top_p=top_p,
        repetition_penalty=repetition_penalty, stop_tokens=stop_tokens,
    )
    broke1, first_break1 = detect_breakthrough(text1) if sun_active else (False, "")

    # ── Run 2 ─────────────────────────────────────────────────────────────
    ts2 = _utc_now_iso()
    text2, usage2, latency2 = _run_single(
        model=model, payload=payload, api_key=api_key,
        temperature=temperature, top_p=top_p,
        repetition_penalty=repetition_penalty, stop_tokens=stop_tokens,
    )
    broke2, first_break2 = detect_breakthrough(text2) if sun_active else (False, "")

    record = DualRunRecord(
        identity_id=identity_id,
        model=model,
        state_tag=state_tag,
        system_prompt=system_prompt,
        user_input=user_input,
        sun_instruction_active=sun_active,
        run1_text=text1,
        run1_tokens=usage1,
        run1_timestamp=ts1,
        run1_latency_ms=latency1,
        run1_broke_through=broke1,
        run1_first_break=first_break1,
        run2_text=text2,
        run2_tokens=usage2,
        run2_timestamp=ts2,
        run2_latency_ms=latency2,
        run2_broke_through=broke2,
        run2_first_break=first_break2,
        any_breakthrough=(broke1 or broke2),
    )

    logger.info(
        "dual_run: completed for identity=%r state=%r sun_active=%r "
        "breakthrough=%r run1=%dms run2=%dms",
        identity_id, state_tag, sun_active, record.any_breakthrough,
        latency1, latency2,
    )

    return record


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_record(record: DualRunRecord) -> str:
    """Persist a DualRunRecord to MongoDB.

    Returns:
        The string ID of the inserted document.
    """
    from falcon.db import get_db
    db  = get_db()
    doc = record.to_dict()
    result = db["dual_run_log"].insert_one(doc)
    return str(result.inserted_id)


def read_records(identity_id: str, limit: int = 100) -> list[dict]:
    """Return dual-run records for identity_id, newest-first.

    Args:
        identity_id: The identity to query.
        limit: Maximum number of records to return.

    Returns:
        List of record dicts (MongoDB _id stripped).
    """
    from falcon.db import get_db
    db = get_db()
    cursor = (
        db["dual_run_log"]
        .find({"identity_id": identity_id}, {"_id": 0})
        .sort("recorded_at", -1)
        .limit(limit)
    )
    return list(cursor)


def read_all_records(limit: int = 200) -> list[dict]:
    """Return dual-run records across all identities, newest-first."""
    from falcon.db import get_db
    db = get_db()
    cursor = (
        db["dual_run_log"]
        .find({}, {"_id": 0})
        .sort("recorded_at", -1)
        .limit(limit)
    )
    return list(cursor)


def delete_records(identity_id: str) -> int:
    """Delete all dual-run records for identity_id.

    Returns:
        Number of documents deleted.
    """
    from falcon.db import get_db
    db = get_db()
    result = db["dual_run_log"].delete_many({"identity_id": identity_id})
    return result.deleted_count
