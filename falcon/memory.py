"""
memory.py — Memory Architecture for Falcon.

Falcon spec:
  Long-term memory is user-controlled.
  Memory retrieval is visible — user knows what was retrieved,
  why it was retrieved, and how it affected generation.

Memory types (all stored in MongoDB 'memory' collection):
  semantic   — long-term factual knowledge and domain concepts
  episodic   — records of specific past events and notable interactions
  procedural — learned behaviors, stated preferences, interaction patterns
  working    — short-term scratch-space entries scoped to the current session
  archive    — aged-out or low-relevance entries (excluded from active retrieval)
  persona    — agent identity record (name, tone, communication_style, core_traits)

Each memory document:
  {
    identity_id:  str,
    memory_type:  "semantic" | "episodic" | "procedural" | "working" | "archive" | "persona",
    content:      str,
    tags:         list[str],
    created_at:   str (ISO 8601),
    updated_at:   str (ISO 8601),
    pinned:       bool,
    source:       str,  # "user" | "auto" | "manual" | "import"
    # Non-persona entries only (populated at retrieval time, not stored):
    score:        float,       # 0.0–1.0
    match_reason: str,         # "pinned" | "tag-match" | "keyword-match" | "recency"
  }

Public API:
  add_memory(identity_id, memory_type, content, tags, pinned, source)
  update_memory(memory_id, content, tags, pinned)
  delete_memory(memory_id)
  get_memories(identity_id, memory_type, limit)
  retrieve_for_generation(identity_id, query, top_k_per_type, recency_weight, relevance_weight)
      → RetrievalResult
  clear_working_memory(identity_id)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from bson import ObjectId

from falcon.db import get_db

# ---------------------------------------------------------------------------
# Memory type definitions (Task 2.1)
# ---------------------------------------------------------------------------

MemoryType = Literal["semantic", "episodic", "procedural", "working", "archive", "persona"]

_ACTIVE_TYPES: tuple[str, ...] = ("semantic", "episodic", "procedural", "working")
_INACTIVE_TYPES: tuple[str, ...] = ("archive",)
_PERSONA_TYPE: str = "persona"
_ALL_TYPES: tuple[str, ...] = _ACTIVE_TYPES + _INACTIVE_TYPES + (_PERSONA_TYPE,)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _doc_to_dict(doc: dict) -> dict:
    """Convert a MongoDB document to a plain dict (stringify ObjectId)."""
    result = {k: v for k, v in doc.items() if k != "_id"}
    if "_id" in doc:
        result["_id"] = str(doc["_id"])
    return result


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------

def add_memory(
    identity_id: str,
    memory_type: MemoryType,
    content: str,
    tags: list[str] | None = None,
    pinned: bool = False,
    source: str = "user",
) -> str:
    """Add a new memory entry and return its string ID.

    Args:
        identity_id:  The identity this memory belongs to.
        memory_type:  One of _ALL_TYPES; raises ValueError for unknown types.
        content:      The memory text.
        tags:         Optional list of searchable tags.
        pinned:       If True, always included in retrieval regardless of limits.
        source:       "user" | "auto" | "manual" | "import"

    Returns:
        The new document's string ID.

    Raises:
        ValueError: If memory_type is not in _ALL_TYPES.
    """
    if memory_type not in _ALL_TYPES:
        raise ValueError(
            f"Unknown memory_type: {memory_type!r}. "
            f"Must be one of: {', '.join(_ALL_TYPES)}"
        )

    now = _utc_now_iso()
    db  = get_db()
    result = db["memory"].insert_one({
        "identity_id": identity_id,
        "memory_type": memory_type,
        "content":     content,
        "tags":        tags or [],
        "created_at":  now,
        "updated_at":  now,
        "pinned":      pinned,
        "source":      source,
    })
    return str(result.inserted_id)


def update_memory(
    memory_id: str,
    content: str | None = None,
    tags: list[str] | None = None,
    pinned: bool | None = None,
) -> bool:
    """Update an existing memory entry.

    Only provided (non-None) fields are updated.

    Args:
        memory_id: String ObjectId of the memory to update.
        content:   New content text, or None to leave unchanged.
        tags:      New tag list, or None to leave unchanged.
        pinned:    New pinned state, or None to leave unchanged.

    Returns:
        True if a document was found and updated, False otherwise.
    """
    updates: dict = {"updated_at": _utc_now_iso()}
    if content  is not None: updates["content"] = content
    if tags     is not None: updates["tags"]    = tags
    if pinned   is not None: updates["pinned"]  = pinned

    db = get_db()
    result = db["memory"].update_one(
        {"_id": ObjectId(memory_id)},
        {"$set": updates},
    )
    return result.matched_count > 0


def delete_memory(memory_id: str) -> bool:
    """Delete a memory entry by ID.

    Returns:
        True if deleted, False if not found.
    """
    db = get_db()
    result = db["memory"].delete_one({"_id": ObjectId(memory_id)})
    return result.deleted_count > 0


def clear_working_memory(identity_id: str) -> int:
    """Delete all working memory entries for a specific identity.

    Identity-scoped: only deletes working entries for identity_id.
    Does not affect any other identity's working memory.

    Returns:
        Number of entries deleted.
    """
    db     = get_db()
    result = db["memory"].delete_many({
        "identity_id": identity_id,
        "memory_type": "working",
    })
    return result.deleted_count


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------

def get_memories(
    identity_id: str,
    memory_type: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Return memory entries for an identity, newest-first.

    Args:
        identity_id:  Identity to query.
        memory_type:  Filter by type, or None for all types.
        limit:        Maximum entries to return.

    Returns:
        List of memory dicts (with string _id).
    """
    db    = get_db()
    query: dict = {"identity_id": identity_id}
    if memory_type:
        query["memory_type"] = memory_type

    cursor = (
        db["memory"]
        .find(query)
        .sort("created_at", -1)
        .limit(limit)
    )
    return [_doc_to_dict(d) for d in cursor]


# ---------------------------------------------------------------------------
# RetrievalResult dataclass (Task 2.3)
# ---------------------------------------------------------------------------

@dataclass
class RetrievalResult:
    """Result of a memory retrieval operation.

    Attributes:
        entries:     The memory entries that were retrieved (persona prepended if exists).
        reasoning:   One human-readable string per non-persona entry.
        by_type:     Entries grouped by memory_type.
        total_found: How many entries matched before the per-type limit was applied.
    """
    entries:     list[dict]             = field(default_factory=list)
    reasoning:   list[str]              = field(default_factory=list)
    by_type:     dict[str, list[dict]]  = field(default_factory=dict)
    total_found: int                    = 0

    def to_display_dict(self) -> dict:
        """Convert to a dict suitable for the Raw Context Viewer."""
        return {
            "retrieved_count": len(self.entries),
            "total_found":     self.total_found,
            "reasoning":       self.reasoning,
            "by_type":         {k: len(v) for k, v in self.by_type.items()},
            "entries":         self.entries,
        }


# ---------------------------------------------------------------------------
# Retrieval for generation (Task 2.4 — weighted scoring)
# ---------------------------------------------------------------------------

def _compute_overlap_score(entry: dict, query_lower: str) -> tuple[float, str]:
    """Compute keyword/tag overlap score and match reason for a single entry.

    Priority order: pinned > tag-match > keyword-match > recency

    Returns:
        (overlap_score, match_reason)
    """
    if entry.get("pinned"):
        return 1.0, "pinned"

    tags = [t.lower() for t in (entry.get("tags") or [])]
    if tags and query_lower:
        matched = sum(1 for tag in tags if tag in query_lower)
        if matched > 0:
            return matched / len(tags), "tag-match"

    content_words = set((entry.get("content") or "").lower().split())
    if content_words and query_lower:
        query_words = set(query_lower.split())
        if query_words:
            matched_words = content_words & query_words
            if matched_words:
                ratio = len(matched_words) / len(query_words)
                return ratio, "keyword-match"

    return 0.0, "recency"


def retrieve_for_generation(
    identity_id: str,
    query: str = "",
    top_k_per_type: int = 3,
    recency_weight: float = 0.4,
    relevance_weight: float = 0.6,
) -> RetrievalResult:
    """Retrieve memory entries for generation using weighted scoring.

    Scoring formula:
        score = (recency_rank_score * recency_weight) + (overlap_score * relevance_weight)

    Where:
        recency_rank_score = 1/(rank+1) normalised to [0,1] across entries of that type
        overlap_score      = tag overlap | keyword overlap | 0.0

    Persona is always prepended to entries if it exists; never scored.
    Archive entries are never returned.
    All queries carry identity_id filter — no cross-identity leakage.

    Args:
        identity_id:     Identity to retrieve for.
        query:           Current user input for relevance scoring.
        top_k_per_type:  Max entries per active type to return.
        recency_weight:  Weight for recency rank score.
        relevance_weight: Weight for overlap score.

    Returns:
        RetrievalResult with all retrieved entries and per-entry reasoning.
    """
    db          = get_db()
    entries:    list[dict]            = []
    reasoning:  list[str]             = []
    by_type:    dict[str, list[dict]] = {}
    total_found = 0

    query_lower = query.lower() if query else ""

    # ── Single round-trip: fetch persona + all active-type entries at once ─
    # One indexed query instead of five sequential Atlas round-trips (persona
    # + one per active type). Results are grouped in Python below; because the
    # cursor is sorted created_at desc, each type's slice preserves the exact
    # newest-first ordering the per-type scoring loop relies on.
    cursor = (
        db["memory"]
        .find({
            "identity_id": identity_id,
            "memory_type": {"$in": list(_ACTIVE_TYPES) + [_PERSONA_TYPE]},
        })
        .sort("created_at", -1)
    )
    docs_by_type: dict[str, list[dict]] = {}
    for d in cursor:
        docs_by_type.setdefault(d.get("memory_type", ""), []).append(_doc_to_dict(d))

    # ── Persona: always prepend if exists; never scored ───────────────────
    persona_entry: dict | None = None
    persona_docs = docs_by_type.get(_PERSONA_TYPE)
    if persona_docs:
        persona_entry = persona_docs[0]
        by_type[_PERSONA_TYPE] = [persona_entry]

    # ── Active types: weighted scoring ────────────────────────────────────
    for mem_type in _ACTIVE_TYPES:
        docs = docs_by_type.get(mem_type, [])
        if not docs:
            continue

        total_found += len(docs)
        n = len(docs)

        # Score each entry
        scored: list[dict] = []
        for rank, doc in enumerate(docs):
            # Recency rank score: 1/(rank+1), normalised by the max possible 1/1 = 1.0
            recency_rank_score = (1.0 / (rank + 1)) / (1.0 / 1)  # normalised: max is 1.0 at rank 0
            # Further normalise so all recency scores are in [0,1]
            # with rank 0 = 1.0, rank n-1 approaching 0
            recency_rank_score = 1.0 / (rank + 1) if n == 1 else (1.0 / (rank + 1)) / (1.0)
            # Clamp to [0, 1]
            recency_rank_score = min(1.0, max(0.0, recency_rank_score))

            overlap_score, match_reason = _compute_overlap_score(doc, query_lower)

            final_score = (recency_rank_score * recency_weight) + (overlap_score * relevance_weight)
            final_score = min(1.0, max(0.0, final_score))

            scored_entry = dict(doc)
            scored_entry["score"]        = round(final_score, 4)
            scored_entry["match_reason"] = match_reason
            scored.append(scored_entry)

        # Sort descending by score, take top_k_per_type
        scored.sort(key=lambda e: e["score"], reverse=True)
        top_entries = scored[:top_k_per_type]

        by_type[mem_type] = top_entries
        entries.extend(top_entries)

        # Build reasoning: one string per entry
        for e in top_entries:
            eid    = e.get("_id", "?")
            score  = e.get("score", 0.0)
            reason = e.get("match_reason", "recency")
            reasoning.append(f"{mem_type}/{eid}: score={score}, reason={reason}")

    # ── Prepend persona to entries (after scoring loop) ───────────────────
    if persona_entry is not None:
        entries = [persona_entry] + entries

    if not entries and persona_entry is None:
        reasoning.append("No memory entries found for this identity.")

    return RetrievalResult(
        entries=entries,
        reasoning=reasoning,
        by_type=by_type,
        total_found=total_found,
    )
