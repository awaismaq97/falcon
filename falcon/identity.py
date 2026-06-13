"""
Identity Manager module for Falcon V1.

Provides operations over per-identity data:
  - list_identities()   — enumerate all identities (from the identities collection + messages)
  - create_identity()   — persist a new identity immediately (before any messages)
  - load_history()      — load the message history for a given identity
  - clear_identity()    — delete all messages for a given identity

Data lives in:
  - MongoDB 'identities' collection  — one doc per identity, written on creation
  - MongoDB 'messages'   collection  — conversation history, scoped by identity_id
"""

from datetime import datetime, timezone

from falcon.db import get_db

# Forbidden characters / sequences in identity_id values.
_FORBIDDEN_CHARS = ("/", "\\")
_FORBIDDEN_SEQUENCES = ("..",)
_FORBIDDEN_BYTES = ("\x00",)


def _validate_identity_id(identity_id: str) -> None:
    """Raise ValueError if identity_id contains path-traversal characters.

        - forward slash  /
        - backslash      \\
        - double-dot     ..
        - null byte      \\x00
    """
    bad: list[str] = []

    if "/" in identity_id:
        bad.append("/")
    if "\\" in identity_id:
        bad.append("\\")
    if ".." in identity_id:
        bad.append("..")
    if "\x00" in identity_id:
        bad.append("null byte")

    if bad:
        raise ValueError(
            f"identity_id contains disallowed character(s): {', '.join(bad)}"
        )


def list_identities() -> list[str]:
    """Return all identity IDs — from the identities collection union messages.

    This ensures identities created before their first message are included.
    Returns an empty list if no identities exist at all.
    Each ID appears exactly once.
    """
    db = get_db()
    from_registry = set(
        doc["identity_id"]
        for doc in db["identities"].find({}, {"identity_id": 1, "_id": 0})
    )
    from_messages = set(db["messages"].distinct("identity_id"))
    return sorted(from_registry | from_messages)


def create_identity(identity_id: str) -> None:
    """Persist a new identity immediately, before any messages are sent.

    Inserts a document into the 'identities' collection so the identity
    shows up in list_identities() right away without needing a first message.
    No-op if the identity already exists.

    Args:
        identity_id: The new identity name.

    Raises:
        ValueError: If identity_id contains forbidden characters.
    """
    _validate_identity_id(identity_id)

    db  = get_db()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    db["identities"].update_one(
        {"identity_id": identity_id},
        {"$setOnInsert": {"identity_id": identity_id, "created_at": now}},
        upsert=True,
    )


def load_history(identity_id: str) -> list[dict]:
    """Return the message history for identity_id in chronological order.

    Behaviour:
    - Validates identity_id for path-traversal characters.
    - Returns an empty list [] if no messages exist for this identity.
    - Returns entries in insertion order (natural MongoDB order), which is
      the same chronological order the previous file-based logger used.
    - Each entry is a plain dict with keys: timestamp, role, content.
      The MongoDB _id field is stripped so the shape is identical to the
      old JSON format callers expect.

    Args:
        identity_id: The identity whose history to load.

    Returns:
        A list of {timestamp, role, content} dicts in chronological order.

    Raises:
        ValueError: If identity_id contains forbidden characters.
    """
    _validate_identity_id(identity_id)

    db = get_db()
    cursor = db["messages"].find(
        {"identity_id": identity_id},
        {"_id": 0, "identity_id": 0},   # strip internal fields
    )
    return list(cursor)


def clear_identity(identity_id: str) -> None:
    """Delete all messages for identity_id from MongoDB.

    Behaviour:
    - Validates identity_id for path-traversal characters.
    - Deletes all documents where identity_id matches.
    - No-op if no documents exist — does not raise.
    - Does NOT affect any other identity's messages.

    Args:
        identity_id: The identity whose messages should be deleted.

    Raises:
        ValueError: If identity_id contains forbidden characters.
    """
    _validate_identity_id(identity_id)

    db = get_db()
    db["messages"].delete_many({"identity_id": identity_id})
