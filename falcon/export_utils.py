"""
export_utils.py — Export JSON envelope serialisation helper.

Produces self-contained JSON exports for conversation, memory, audit,
and context snapshot data. All MongoDB ObjectId values are serialised
as strings. All exports carry a falcon_export_version field.

Public API:
    make_export_envelope(identity_id, data) -> dict
        Produces {"falcon_export_version":"1","exported_at":...,"identity_id":...,"data":...}
        with all ObjectId values serialised as strings.

    to_json_str(envelope) -> str
        Serialise the envelope to a JSON string safe for download.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _serialise(obj: Any) -> Any:
    """Recursively serialise a value, converting ObjectId → str."""
    # Check for bson.ObjectId without requiring bson at import time
    # (tests may use mongomock which provides its own ObjectId)
    type_name = type(obj).__name__
    if type_name == "ObjectId":
        return str(obj)

    if isinstance(obj, dict):
        return {k: _serialise(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serialise(v) for v in obj]
    # datetime → ISO string
    if isinstance(obj, datetime):
        return obj.strftime("%Y-%m-%dT%H:%M:%SZ")
    return obj


def make_export_envelope(identity_id: str, data: Any) -> dict:
    """Produce a self-contained export envelope.

    Args:
        identity_id: The identity the data belongs to.
        data: The payload — list or dict of Falcon records.

    Returns:
        Dict with keys: falcon_export_version, exported_at, identity_id, data.
        All ObjectId values are serialised as strings.
    """
    exported_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    clean_data = _serialise(data)

    return {
        "falcon_export_version": "1",
        "exported_at":           exported_at,
        "identity_id":           str(identity_id),
        "data":                  clean_data,
    }


def to_json_str(envelope: dict) -> str:
    """Serialise the export envelope to a JSON string.

    Args:
        envelope: Dict produced by make_export_envelope.

    Returns:
        UTF-8 JSON string, sorted keys, 2-space indent.
    """
    import json
    return json.dumps(envelope, ensure_ascii=False, indent=2, default=str)
