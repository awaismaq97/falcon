"""
audit.py — Inference Audit Trail for Falcon.

Falcon spec requirement:
  Maintain a complete log of every inference event so any session can
  be reconstructed and investigated for continuity, drift, contamination,
  or unexpected behavior.

Logged per inference event:
  - timestamp (UTC ISO 8601)
  - model used
  - active identity / channel
  - prompt_state: "present" | "empty" | "null" | "deleted"
  - system_prompt: the exact prompt text (or null)
  - retrieved_memories: list of memory entries used
  - generation_settings: temperature, top_p, repetition_penalty,
                         max_tokens, stop_tokens
  - context_size: number of messages in assembled payload
  - context_token_estimate: rough word-count proxy
  - raw_model_output: the exact text the model returned
  - usage: prompt_tokens, completion_tokens, total_tokens
  - latency_ms: inference wall-clock time

All records are stored in the MongoDB 'audit_log' collection (configurable).
The collection is scoped by identity_id.

Public API:
  write_audit_record(identity_id, record)
      → None   Inserts one audit record.

  read_audit_records(identity_id, limit)
      → list[dict]   Returns records newest-first.

  read_all_audit_records(limit)
      → list[dict]   Returns records across all identities, newest-first.
"""

from datetime import datetime, timezone
from typing import Any

from bson import ObjectId

from falcon.db import get_db

# Heavy fields embedded in every audit record — the full assembled payload,
# raw model output, retrieved memories, etc. A single record can be hundreds of
# KB because assembled_payload contains the entire growing conversation at that
# turn. The list view must NOT pull these; it projects them out and the full
# record is fetched on demand (read_audit_detail) only when a user expands one.
_HEAVY_FIELDS = (
    "assembled_payload",
    "raw_model_output",
    "retrieved_memories",
    "system_prompt",
    "generation_settings",
)

# Projection for the list view: exclude every heavy field but KEEP _id so the
# full record can be fetched on demand via read_audit_detail().
_SUMMARY_PROJECTION = {f: 0 for f in _HEAVY_FIELDS}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_audit_record(identity_id: str, record: dict[str, Any]) -> None:
    """Insert one audit record into the audit_log collection.

    The record is stored verbatim with identity_id and a server-side
    timestamp added if not already present.

    Args:
        identity_id: The identity / channel that generated this inference.
        record: Dict with all audit fields (see module docstring).
    """
    db = get_db()
    doc = {
        "identity_id": identity_id,
        "recorded_at": record.get("timestamp") or _utc_now_iso(),
        **record,
    }
    db["audit_log"].insert_one(doc)


def read_audit_records(identity_id: str, limit: int = 100) -> list[dict]:
    """Return audit records for identity_id, newest-first.

    Args:
        identity_id: The identity to query.
        limit: Maximum number of records to return.

    Returns:
        List of audit record dicts (MongoDB _id stripped).
    """
    db = get_db()
    cursor = (
        db["audit_log"]
        .find({"identity_id": identity_id}, {"_id": 0})
        .sort("recorded_at", -1)
        .limit(limit)
    )
    return list(cursor)


def read_all_audit_records(limit: int = 200) -> list[dict]:
    """Return audit records across all identities, newest-first.

    Args:
        limit: Maximum number of records to return.

    Returns:
        List of audit record dicts (MongoDB _id stripped).
    """
    db = get_db()
    cursor = (
        db["audit_log"]
        .find({}, {"_id": 0})
        .sort("recorded_at", -1)
        .limit(limit)
    )
    return list(cursor)


# ---------------------------------------------------------------------------
# Two-tier reads: light summaries for the list, full detail on demand.
# ---------------------------------------------------------------------------

def read_audit_summaries(identity_id: str, limit: int = 100) -> list[dict]:
    """Return lightweight audit summaries for identity_id, newest-first.

    Heavy fields (assembled_payload, raw_model_output, retrieved_memories,
    system_prompt, generation_settings) are projected out so the list loads
    in milliseconds instead of pulling tens of MB. Each summary carries a
    string ``_id`` for fetching the full record via read_audit_detail().
    """
    db = get_db()
    cursor = (
        db["audit_log"]
        .find({"identity_id": identity_id}, _SUMMARY_PROJECTION)
        .sort("recorded_at", -1)
        .limit(limit)
    )
    out: list[dict] = []
    for doc in cursor:
        doc["_id"] = str(doc["_id"])
        out.append(doc)
    return out


def read_all_audit_summaries(limit: int = 200) -> list[dict]:
    """Return lightweight audit summaries across all identities, newest-first."""
    db = get_db()
    cursor = (
        db["audit_log"]
        .find({}, _SUMMARY_PROJECTION)
        .sort("recorded_at", -1)
        .limit(limit)
    )
    out: list[dict] = []
    for doc in cursor:
        doc["_id"] = str(doc["_id"])
        out.append(doc)
    return out


def read_audit_detail(record_id: str) -> dict | None:
    """Fetch the full heavy fields for one audit record by its string _id.

    Returns None if the id is malformed or no record matches.
    """
    try:
        oid = ObjectId(record_id)
    except Exception:
        return None
    db = get_db()
    doc = db["audit_log"].find_one({"_id": oid})
    if not doc:
        return None
    doc["_id"] = str(doc["_id"])
    return doc


def build_audit_record(
    identity_id: str,
    model: str,
    prompt_state: str,
    system_prompt: str | None,
    retrieved_memories: list[dict],
    generation_settings: dict,
    context_size: int | None = None,
    context_token_estimate: int | None = None,
    assembled_payload: list[dict] | None = None,
    raw_model_output: str = "",
    usage: dict | None = None,
    latency_ms: float = 0.0,
) -> dict:
    """Construct an audit record dict ready for write_audit_record().

    All fields are required per spec (Req 14.1). Raises ValueError if any
    required field is missing or None when it must be present.

    Args:
        identity_id:            Identity / channel for this inference.
        model:                  Model name used.
        prompt_state:           "present" | "empty"
        system_prompt:          Exact prompt text, or None if absent.
        retrieved_memories:     Memory entries that entered this generation.
        generation_settings:    Dict of temperature, top_p, etc.
        context_size:           Number of messages in assembled payload.
        context_token_estimate: Rough token count of the assembled context.
        assembled_payload:      The exact message list sent to the model.
        raw_model_output:       Unfiltered model response text.
        usage:                  Token counts from the model API.
        latency_ms:             Wall-clock inference time in milliseconds.

    Returns:
        Audit record dict with all 13 required keys.

    Raises:
        ValueError: If any required field is absent/None.
    """
    assembled_payload = assembled_payload or []
    usage = usage or {}

    # Validate required fields (Req 14.1)
    _required = {
        "identity_id": identity_id,
        "model": model,
        "prompt_state": prompt_state,
        "generation_settings": generation_settings,
        "assembled_payload": assembled_payload,
    }
    for field_name, value in _required.items():
        if value is None:
            raise ValueError(
                f"build_audit_record: required field '{field_name}' is None"
            )

    if not identity_id:
        raise ValueError("build_audit_record: 'identity_id' must not be empty")
    if not model:
        raise ValueError("build_audit_record: 'model' must not be empty")

    # Compute context_size and context_token_estimate if not provided
    if context_size is None:
        context_size = len(assembled_payload)
    if context_token_estimate is None:
        context_text = " ".join(m.get("content", "") for m in assembled_payload)
        context_token_estimate = len(context_text) // 4

    return {
        "timestamp":              _utc_now_iso(),
        "identity_id":            identity_id,
        "model":                  model,
        "prompt_state":           prompt_state,
        "system_prompt":          system_prompt,
        "retrieved_memories":     retrieved_memories,
        "generation_settings":    generation_settings,
        "context_size":           context_size,
        "context_token_estimate": context_token_estimate,
        "assembled_payload":      assembled_payload,
        "raw_model_output":       raw_model_output,
        "usage":                  usage,
        "latency_ms":             latency_ms,
    }
