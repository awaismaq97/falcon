"""
db.py — MongoDB connection singleton for Falcon V1.

Provides a single get_db() function that returns the Falcon database handle.
The connection is created once per process and reused across all callers.

Requires MONGODB_URI in the .env file (loaded by config.py before this
module is imported).

Collections used:
  messages  — {identity_id, timestamp, role, content}
  traces    — {identity_id, user_timestamp, send_timestamp, user, steps}
  tokens    — {identity_id, prompt, completion, total}
"""

import os

from pymongo import MongoClient
from pymongo.database import Database

def get_db() -> Database:
    """Return the Falcon MongoDB database, connecting on first call.

    The client is cached at the process level via Streamlit's cache_resource
    so the TCP connection is reused across all reruns instead of being
    re-established every time the script executes.
    """
    return _get_cached_db()


def _get_cached_db() -> Database:
    # Import here to avoid a hard dependency on streamlit in unit tests.
    try:
        import streamlit as st

        @st.cache_resource
        def _connect() -> Database:
            return _make_db()

        return _connect()
    except ImportError:
        # Fallback for non-Streamlit contexts (tests, CLI)
        return _make_db()


def _make_db() -> Database:
    uri = os.environ.get("MONGODB_URI", "")
    if not uri or not uri.strip():
        raise ValueError(
            "MONGODB_URI is not set. "
            "Add your MongoDB Atlas connection string to the .env file:\n"
            "  MONGODB_URI=mongodb+srv://<user>:<password>@<cluster>.mongodb.net/?retryWrites=true&w=majority"
        )
    client = MongoClient(
        uri,
        # Keep the connection alive; don't wait more than 5 s to connect.
        serverSelectionTimeoutMS=5000,
        connectTimeoutMS=5000,
        socketTimeoutMS=10000,
    )
    db = client["falcon"]
    # Ensure indexes exist (no-op if already present)
    db["messages"].create_index("identity_id")
    db["traces"].create_index("identity_id")
    db["tokens"].create_index("identity_id", unique=True)
    # Audit trail indexes
    db["audit_log"].create_index("identity_id")
    db["audit_log"].create_index("recorded_at")
    # Memory indexes
    db["memory"].create_index("identity_id")
    db["memory"].create_index([("identity_id", 1), ("memory_type", 1)])
    db["memory"].create_index([("identity_id", 1), ("pinned", -1)])
    # Conversation summary index
    db["conversation_summaries"].create_index("identity_id", unique=True)
    # Dual-run log indexes
    db["dual_run_log"].create_index("identity_id")
    db["dual_run_log"].create_index("recorded_at")
    db["dual_run_log"].create_index([("identity_id", 1), ("state_tag", 1)])
    return db
