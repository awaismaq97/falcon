"""
Logger module for Falcon V1.

Provides append_message() — the sole write path for conversation logs.
Messages are stored in the MongoDB 'messages' collection with documents:
  {identity_id, timestamp, role, content}

The collection is ordered by insertion order (natural order), which preserves
chronological sequence exactly as the previous file-based implementation did.
"""

from datetime import datetime, timezone

from falcon.db import get_db

# Characters that are forbidden in identity_id values.
_FORBIDDEN_CHARS = {"/", "\\"}
_FORBIDDEN_SEQUENCES = {".."}
_FORBIDDEN_BYTES = {"\x00"}


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


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string ending with 'Z'."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append_message(identity_id: str, role: str, content: str, timestamp: str = "") -> None:
    """Append one message entry to the MongoDB 'messages' collection.

    Args:
        identity_id: Scoping key for the conversation.
        role: Must be "user" or "assistant".
        content: Message text (may be an empty string).
        timestamp: Optional ISO 8601 timestamp. If omitted, current UTC time is used.

    Raises:
        ValueError: If identity_id contains forbidden characters.
        ValueError: If role is not "user" or "assistant".
    """
    _validate_identity_id(identity_id)

    if role not in ("user", "assistant"):
        raise ValueError(
            f"role must be 'user' or 'assistant', got: {role!r}"
        )

    db = get_db()
    db["messages"].insert_one({
        "identity_id": identity_id,
        "timestamp":   timestamp if timestamp else _utc_now_iso(),
        "role":        role,
        "content":     content,
    })
