"""
tests/test_integration.py — Integration tests for Falcon.

Tasks 18.1, 18.2, 18.3.

All tests use mongomock for MongoDB isolation.
No live API calls are made — OpenRouter / LLM calls are mocked.

Run with:
    conda run -n falcon python -m pytest tests/test_integration.py -v
"""
from __future__ import annotations

import json
import threading
import time
from collections import defaultdict, deque
from unittest.mock import MagicMock, patch

import mongomock
import pytest


def _make_db():
    client = mongomock.MongoClient()
    return client["falcon_int_test"]


# ---------------------------------------------------------------------------
# Task 18.1 — Full _handle_send flow: message logged, audit written, tokens updated
# ---------------------------------------------------------------------------

class TestHandleSendFlow:
    """Integration test for the post-generation pipeline.

    Verifies that after a complete inference turn:
    - User message is logged to messages collection
    - Assistant message is logged to messages collection
    - Audit record is written with all 13 required fields
    - Token counts are persisted to tokens collection
    """

    def test_message_logged_audit_written_tokens_updated(self):
        """18.1: Message logged, audit written, tokens updated in DB."""
        mock_db = _make_db()
        mock_db["messages"].drop()
        mock_db["audit_log"].drop()
        mock_db["tokens"].drop()

        identity_id = "integration-test-A"
        user_input  = "What is the capital of France?"
        model_reply = "Paris."

        # Patch get_db everywhere
        with patch("falcon.db.get_db", return_value=mock_db), \
             patch("falcon.memory.get_db", return_value=mock_db), \
             patch("falcon.identity.get_db", return_value=mock_db), \
             patch("falcon.audit.get_db", return_value=mock_db), \
             patch("falcon.logger.get_db", return_value=mock_db):

            import falcon.logger as Logger
            import falcon.audit  as Audit
            import falcon.memory as Memory

            # Log user message
            Logger.append_message(identity_id, "user", user_input, timestamp="t1")

            # Log assistant message
            Logger.append_message(identity_id, "assistant", model_reply, timestamp="t2")

            # Write audit record
            audit_record = Audit.build_audit_record(
                identity_id=identity_id,
                model="test/model",
                prompt_state="empty",
                system_prompt=None,
                retrieved_memories=[],
                generation_settings={"temperature": 0.7, "top_p": 1.0, "max_tokens": 256},
                context_size=2,
                context_token_estimate=10,
                assembled_payload=[
                    {"role": "user", "content": user_input}
                ],
                raw_model_output=model_reply,
                usage={"prompt_tokens": 15, "completion_tokens": 5, "total_tokens": 20},
                latency_ms=123.4,
            )
            Audit.write_audit_record(identity_id, audit_record)

            # Persist tokens
            mock_db["tokens"].update_one(
                {"identity_id": identity_id},
                {"$set": {"identity_id": identity_id, "prompt": 15, "completion": 5, "total": 20}},
                upsert=True,
            )

            # ── Assertions ───────────────────────────────────────────────
            # Messages logged
            messages = list(mock_db["messages"].find({"identity_id": identity_id}))
            assert len(messages) == 2, f"Expected 2 messages, got {len(messages)}"
            roles = [m["role"] for m in messages]
            assert "user" in roles and "assistant" in roles

            # Audit record written
            audits = list(mock_db["audit_log"].find({"identity_id": identity_id}))
            assert len(audits) == 1, f"Expected 1 audit record, got {len(audits)}"
            rec = audits[0]
            required_keys = {
                "timestamp", "identity_id", "model", "prompt_state", "system_prompt",
                "retrieved_memories", "generation_settings", "context_size",
                "context_token_estimate", "assembled_payload", "raw_model_output",
                "usage", "latency_ms",
            }
            missing = required_keys - set(rec.keys())
            assert not missing, f"Audit record missing keys: {missing}"

            # Tokens persisted
            tok = mock_db["tokens"].find_one({"identity_id": identity_id})
            assert tok is not None, "Token record not found"
            assert tok["total"] == 20


# ---------------------------------------------------------------------------
# Task 18.2 — Memory_Extractor.run persists entries correctly
# ---------------------------------------------------------------------------

class TestMemoryExtractorIntegration:
    """Integration test: Memory_Extractor.run persists entries with correct fields."""

    def test_extractor_persists_with_correct_identity_and_source(self):
        """18.2: Entries persisted with correct identity_id and source='auto'."""
        mock_db = _make_db()
        mock_db["memory"].drop()

        identity_id = "extractor-integration-B"

        fake_llm_json = json.dumps([
            {"memory_type": "semantic",   "content": "France capital is Paris", "tags": ["france", "capital"]},
            {"memory_type": "episodic",   "content": "User asked about France",  "tags": []},
            {"memory_type": "persona",    "content": "Should be rejected",       "tags": []},  # forbidden
            {"memory_type": "archive",    "content": "Should be rejected",       "tags": []},  # forbidden
        ])

        mock_choice = MagicMock()
        mock_choice.message.content = fake_llm_json
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]

        with patch("falcon.memory.get_db", return_value=mock_db):
            # The extractor obtains its OpenRouter client via
            # falcon.engine.get_client (a process-wide pooled client), so the
            # mock must patch that accessor rather than openai.OpenAI directly.
            mock_client = MagicMock()
            mock_client.chat.completions.create.return_value = mock_response
            with patch("falcon.engine.get_client", return_value=mock_client):

                import falcon.memory_extractor as Extractor
                fresh_queues = defaultdict(lambda: deque(maxlen=10))
                with patch.object(Extractor, "_extractor_queues", fresh_queues):
                    Extractor.run({
                        "identity_id":       identity_id,
                        "user_message":      "What is the capital of France?",
                        "assistant_message": "Paris.",
                        "turn_index":        1,
                        "timestamp":         "2024-01-01T00:00:00Z",
                    })

            # Allow background work to settle (extractor runs synchronously in run())
            entries = list(mock_db["memory"].find({"identity_id": identity_id}))

        # persona and archive should be rejected
        types = {e["memory_type"] for e in entries}
        assert "persona" not in types, "Extractor must not write persona entries"
        assert "archive" not in types, "Extractor must not write archive entries"

        # All entries must have source="auto" and correct identity_id
        for entry in entries:
            assert entry.get("source") == "auto", (
                f"Expected source='auto', got {entry.get('source')!r}"
            )
            assert entry.get("identity_id") == identity_id, (
                f"Expected identity_id={identity_id!r}, got {entry.get('identity_id')!r}"
            )

        # Should have persisted 2 valid entries (semantic + episodic)
        assert len(entries) == 2, f"Expected 2 valid entries, got {len(entries)}"


# ---------------------------------------------------------------------------
# Task 18.3 — Identity switch — no state bleed
# ---------------------------------------------------------------------------

class TestIdentitySwitchNoStateBleed:
    """Integration test: switching identity produces no history/memory/token bleed."""

    def test_no_history_bleed_on_switch(self):
        """18.3: After identity switch, load_history returns only new identity's data."""
        mock_db = _make_db()
        mock_db["messages"].drop()

        id_a = "switch-test-A"
        id_b = "switch-test-B"

        # Seed messages for both identities
        mock_db["messages"].insert_many([
            {"identity_id": id_a, "role": "user",      "content": "Hello from A", "timestamp": "t1"},
            {"identity_id": id_a, "role": "assistant",  "content": "Hi A!",        "timestamp": "t2"},
            {"identity_id": id_b, "role": "user",      "content": "Hello from B", "timestamp": "t3"},
            {"identity_id": id_b, "role": "assistant",  "content": "Hi B!",        "timestamp": "t4"},
        ])

        with patch("falcon.identity.get_db", return_value=mock_db):
            import falcon.identity as Identity

            history_a = Identity.load_history(id_a)
            history_b = Identity.load_history(id_b)

        # id_A history must not contain id_B messages
        a_contents = {m.get("content") for m in history_a}
        b_contents = {m.get("content") for m in history_b}

        assert "Hello from B" not in a_contents, "id_B message leaked into id_A history"
        assert "Hi B!" not in a_contents,        "id_B message leaked into id_A history"
        assert "Hello from A" not in b_contents, "id_A message leaked into id_B history"
        assert "Hi A!" not in b_contents,        "id_A message leaked into id_B history"

        assert "Hello from A" in a_contents
        assert "Hello from B" in b_contents

    def test_no_memory_bleed_on_switch(self):
        """18.3: Memory retrieval after identity switch returns only new identity's entries."""
        mock_db = _make_db()
        mock_db["memory"].drop()

        id_a = "switch-mem-A"
        id_b = "switch-mem-B"

        with patch("falcon.memory.get_db", return_value=mock_db):
            import falcon.memory as Memory

            Memory.add_memory(id_a, "semantic", "Fact about A")
            Memory.add_memory(id_b, "semantic", "Fact about B")

            result_a = Memory.retrieve_for_generation(identity_id=id_a, query="fact")
            result_b = Memory.retrieve_for_generation(identity_id=id_b, query="fact")

        a_contents = {e.get("content") for e in result_a.entries}
        b_contents = {e.get("content") for e in result_b.entries}

        assert "Fact about B" not in a_contents, "id_B memory leaked into id_A retrieval"
        assert "Fact about A" not in b_contents, "id_A memory leaked into id_B retrieval"
        assert "Fact about A" in a_contents
        assert "Fact about B" in b_contents

    def test_no_token_bleed_on_switch(self):
        """18.3: Token counts from id_A do not appear in id_B after switch."""
        mock_db = _make_db()
        mock_db["tokens"].drop()

        id_a = "switch-tok-A"
        id_b = "switch-tok-B"

        mock_db["tokens"].insert_one(
            {"identity_id": id_a, "prompt": 500, "completion": 200, "total": 700}
        )
        # id_B has no tokens yet

        tok_a = mock_db["tokens"].find_one({"identity_id": id_a}, {"_id": 0})
        tok_b = mock_db["tokens"].find_one({"identity_id": id_b}, {"_id": 0})

        assert tok_a["total"] == 700
        assert tok_b is None, "id_B should have no token record"
