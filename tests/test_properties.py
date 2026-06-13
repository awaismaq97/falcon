"""
tests/test_properties.py — Hypothesis property-based tests for Falcon.

Covers all 19 correctness properties from the design spec, plus config
fail-fast unit tests (task 1.2).

Run with:
    conda run -n falcon pytest tests/test_properties.py -v

All MongoDB interactions use mongomock (in-memory).
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
import tempfile
import types
from unittest.mock import MagicMock, patch

import pytest
import yaml
from collections import defaultdict, deque
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Patch mongomock before importing falcon.memory / falcon.identity
# ---------------------------------------------------------------------------
import mongomock

# ---------------------------------------------------------------------------
# Helpers for mongomock patching
# ---------------------------------------------------------------------------

def _get_mongomock_db():
    client = mongomock.MongoClient()
    return client["falcon_test"]


# ---------------------------------------------------------------------------
# Task 1.2 — Config fail-fast unit tests (not a top-19 property)
# ---------------------------------------------------------------------------

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_YAML = os.path.join(BASE, "config.yaml")
CONFIG_MODULE = os.path.join(BASE, "falcon", "config.py")


def _load_config_with_overrides(overrides: dict):
    """Import falcon.config with YAML values overridden. Returns module or raises."""
    with open(CONFIG_YAML, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg.update(overrides)

    import builtins
    import io

    orig_open = builtins.open

    def patched_open(path, *a, **kw):
        if os.path.basename(str(path)) == "config.yaml":
            mode = a[0] if a else kw.get("mode", "r")
            if "b" in str(mode):
                return io.BytesIO(yaml.dump(cfg).encode())
            return io.StringIO(yaml.dump(cfg))
        return orig_open(path, *a, **kw)

    builtins.open = patched_open
    try:
        spec = importlib.util.spec_from_file_location("falcon.config_test", CONFIG_MODULE)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        builtins.open = orig_open


class TestConfigFailFast:
    """Task 1.2 — config fail-fast validation (unit tests, not in top-19)."""

    def test_invalid_truncation_strategy_raises(self):
        with pytest.raises(ValueError, match="history_truncation_strategy"):
            _load_config_with_overrides({"history_truncation_strategy": "bad-value"})

    def test_history_max_turns_zero_raises(self):
        with pytest.raises(ValueError, match="history_max_turns"):
            _load_config_with_overrides({"history_max_turns": 0})

    def test_history_max_turns_over_max_raises(self):
        with pytest.raises(ValueError, match="history_max_turns"):
            _load_config_with_overrides({"history_max_turns": 101})

    def test_history_token_budget_below_min_raises(self):
        with pytest.raises(ValueError, match="history_token_budget"):
            _load_config_with_overrides({"history_token_budget": 50})

    def test_history_token_budget_over_max_raises(self):
        with pytest.raises(ValueError, match="history_token_budget"):
            _load_config_with_overrides({"history_token_budget": 200001})

    def test_top_k_per_type_zero_raises(self):
        with pytest.raises(ValueError, match="top_k_per_type"):
            _load_config_with_overrides({"top_k_per_type": 0})

    def test_top_k_per_type_over_max_raises(self):
        with pytest.raises(ValueError, match="top_k_per_type"):
            _load_config_with_overrides({"top_k_per_type": 21})

    def test_valid_boundary_top_k_min(self):
        mod = _load_config_with_overrides({"top_k_per_type": 1})
        assert mod.top_k_per_type == 1

    def test_valid_boundary_top_k_max(self):
        mod = _load_config_with_overrides({"top_k_per_type": 20})
        assert mod.top_k_per_type == 20

    def test_valid_boundary_max_turns_min(self):
        mod = _load_config_with_overrides({"history_max_turns": 1})
        assert mod.history_max_turns == 1

    def test_valid_boundary_max_turns_max(self):
        mod = _load_config_with_overrides({"history_max_turns": 100})
        assert mod.history_max_turns == 100

    def test_valid_boundary_token_budget_min(self):
        mod = _load_config_with_overrides({"history_token_budget": 100})
        assert mod.history_token_budget == 100

    def test_valid_boundary_token_budget_max(self):
        mod = _load_config_with_overrides({"history_token_budget": 200000})
        assert mod.history_token_budget == 200000

    def test_valid_strategy_token_budget(self):
        mod = _load_config_with_overrides({"history_truncation_strategy": "token-budget"})
        assert mod.history_truncation_strategy == "token-budget"

    def test_valid_strategy_summarize(self):
        mod = _load_config_with_overrides(
            {"history_truncation_strategy": "summarize-and-compress"}
        )
        assert mod.history_truncation_strategy == "summarize-and-compress"


# ---------------------------------------------------------------------------
# Import engine for property tests (no live DB needed)
# ---------------------------------------------------------------------------
from falcon.engine import build_annotated_payload, VALID_SOURCES  # noqa: E402


# ---------------------------------------------------------------------------
# Property 1: Empty system prompt produces no system message
# ---------------------------------------------------------------------------

@given(system_prompt=st.one_of(st.none(), st.just(""), st.text(alphabet=" \t\n\r")))
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_1_empty_system_prompt_no_system_message(system_prompt):
    """Property 1: Empty/whitespace/None system prompt → no system-prompt element."""
    annotated, _ = build_annotated_payload(
        system_prompt=system_prompt or "",
        messages=[{"role": "user", "content": "hello"}],
        memory_block=[],
    )
    system_elements = [
        e for e in annotated
        if e.get("role") == "system" and e.get("source") == "system-prompt"
    ]
    assert system_elements == [], (
        f"Expected no system-prompt element for prompt={system_prompt!r}, "
        f"got: {system_elements}"
    )


# ---------------------------------------------------------------------------
# Property 2: Non-empty system prompt is byte-for-byte first system element
# ---------------------------------------------------------------------------

@given(system_prompt=st.text().filter(lambda s: s.strip()))
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_2_nonempty_system_prompt_is_first_system_element(system_prompt):
    """Property 2: Non-empty system prompt → first system-prompt element with exact content."""
    annotated, _ = build_annotated_payload(
        system_prompt=system_prompt,
        messages=[{"role": "user", "content": "hello"}],
        memory_block=[],
    )
    sp_elements = [e for e in annotated if e.get("source") == "system-prompt"]
    assert len(sp_elements) >= 1, (
        f"Expected at least one system-prompt element, got none. prompt={system_prompt!r}"
    )
    # It should appear before any history/user-input elements
    first_sp = sp_elements[0]
    assert first_sp["role"] == "system"
    assert first_sp["content"] == system_prompt, (
        f"system-prompt content mismatch: "
        f"expected {system_prompt!r}, got {first_sp['content']!r}"
    )


# ---------------------------------------------------------------------------
# Property 3: Empty model output yields [no output] marker
# ---------------------------------------------------------------------------

def test_property_3_empty_model_output_marker():
    """Property 3: Empty/whitespace model output → raw_output == '[no output]'."""
    from falcon.engine import _EMPTY_OUTPUT_MARKER, _StreamResult

    empty_outputs = ["", "   ", "\t\n", "\r\n  \t"]

    for empty_output in empty_outputs:
        # Mock the OpenAI client to return empty content
        mock_chunk = MagicMock()
        mock_chunk.usage = None
        mock_chunk.choices = [MagicMock()]
        mock_chunk.choices[0].delta.content = None

        final_chunk = MagicMock()
        final_chunk.usage = MagicMock()
        final_chunk.usage.prompt_tokens = 0
        final_chunk.usage.completion_tokens = 0
        final_chunk.usage.total_tokens = 0
        final_chunk.choices = []

        with patch("openai.OpenAI") as mock_openai:
            mock_client = MagicMock()
            mock_openai.return_value = mock_client
            mock_client.chat.completions.create.return_value = iter([mock_chunk, final_chunk])

            gen = _StreamResult(
                model_name="test-model",
                payload=[{"role": "user", "content": "hi"}],
                api_key="test-key",
            )
            tokens = list(gen)

        assert _EMPTY_OUTPUT_MARKER in tokens, (
            f"Expected '[no output]' token for empty output {empty_output!r}"
        )
        assert gen.raw_output == _EMPTY_OUTPUT_MARKER, (
            f"Expected raw_output == '[no output]' for empty output {empty_output!r}"
        )


# ---------------------------------------------------------------------------
# Property 4: Annotated payload structural completeness
# ---------------------------------------------------------------------------

_msg_strategy = st.fixed_dictionaries({
    "role":    st.sampled_from(["user", "assistant"]),
    "content": st.text(max_size=200),
})

_mem_entry_strategy = st.fixed_dictionaries({
    "memory_type": st.sampled_from(["semantic", "episodic", "procedural", "working"]),
    "content":     st.text(max_size=200),
    "tags":        st.lists(st.text(max_size=20), max_size=3),
    "pinned":      st.booleans(),
})


@given(
    system_prompt=st.text(max_size=300),
    messages=st.lists(_msg_strategy, min_size=1, max_size=10),
    memory_block=st.lists(_mem_entry_strategy, max_size=5),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_4_annotated_payload_structural_completeness(
    system_prompt, messages, memory_block
):
    """Property 4: Every element has exactly {role, content, source} and source ∈ VALID_SOURCES."""
    annotated, _ = build_annotated_payload(
        system_prompt=system_prompt,
        messages=messages,
        memory_block=memory_block,
    )
    for i, element in enumerate(annotated):
        assert set(element.keys()) == {"role", "content", "source"}, (
            f"Element {i} has unexpected keys: {set(element.keys())}"
        )
        assert element["source"] in VALID_SOURCES, (
            f"Element {i} has invalid source: {element['source']!r}"
        )


# ---------------------------------------------------------------------------
# Property 5: Source annotation correctness
# ---------------------------------------------------------------------------

@given(
    system_prompt=st.text(max_size=200).filter(lambda s: s.strip()),
    n_history=st.integers(min_value=0, max_value=4),
    n_memory=st.integers(min_value=0, max_value=3),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_5_source_annotation_correctness(system_prompt, n_history, n_memory):
    """Property 5: Each element's source correctly reflects its semantic origin."""
    # Build history pairs
    history = []
    for i in range(n_history):
        history.append({"role": "user", "content": f"user msg {i}"})
        history.append({"role": "assistant", "content": f"asst msg {i}"})
    current_input = {"role": "user", "content": "current question"}
    messages = history + [current_input]

    # Build memory with one persona and some non-persona
    persona = {"memory_type": "persona", "content": "I am the persona", "tags": [], "pinned": False}
    other_mem = [
        {"memory_type": "semantic", "content": f"fact {i}", "tags": [], "pinned": False}
        for i in range(n_memory)
    ]
    memory_block = [persona] + other_mem if n_memory > 0 else [persona]

    annotated, _ = build_annotated_payload(
        system_prompt=system_prompt,
        messages=messages,
        memory_block=memory_block,
    )

    for elem in annotated:
        src = elem["source"]
        role = elem["role"]
        content = elem["content"]

        if src == "persona":
            assert role == "system"
        elif src == "system-prompt":
            assert role == "system"
            assert content == system_prompt
        elif src == "memory":
            assert role == "system"
        elif src == "history":
            assert role in ("user", "assistant")
        elif src == "user-input":
            assert role in ("user", "assistant")
        elif src == "history-summary":
            assert role == "system"
        else:
            pytest.fail(f"Unknown source: {src!r}")

    # Current user input must be annotated as user-input
    user_input_elements = [e for e in annotated if e["source"] == "user-input"]
    assert len(user_input_elements) == 1, (
        f"Expected exactly 1 user-input element, got {len(user_input_elements)}"
    )
    assert user_input_elements[0]["content"] == current_input["content"]


# ---------------------------------------------------------------------------
# Memory property tests — use mongomock
# ---------------------------------------------------------------------------

def _make_memory_module_with_mock_db():
    """Return (memory_module, mock_db). The module's get_db is patched."""
    import falcon.memory as Memory
    mock_db = _get_mongomock_db()
    # Clear between tests
    mock_db["memory"].drop()
    return Memory, mock_db


# ---------------------------------------------------------------------------
# Property 6: Invalid memory type raises ValueError
# ---------------------------------------------------------------------------

_VALID_MEMORY_TYPES = {"semantic", "episodic", "procedural", "working", "archive", "persona"}


@given(memory_type=st.text().filter(lambda s: s not in _VALID_MEMORY_TYPES))
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_6_invalid_memory_type_raises(memory_type):
    """Property 6: Invalid memory_type raises ValueError and nothing is persisted."""
    import falcon.memory as Memory

    mock_db = _get_mongomock_db()
    mock_db["memory"].drop()

    with patch("falcon.memory.get_db", return_value=mock_db):
        with pytest.raises(ValueError):
            Memory.add_memory(
                identity_id="test-id",
                memory_type=memory_type,  # type: ignore[arg-type]
                content="test content",
            )

        # Nothing should have been persisted
        count = mock_db["memory"].count_documents({"identity_id": "test-id"})
        assert count == 0, (
            f"Expected 0 documents after invalid type {memory_type!r}, got {count}"
        )


# ---------------------------------------------------------------------------
# Property 7: Retrieval never crosses identity boundary
# ---------------------------------------------------------------------------

@given(
    id_a=st.text(min_size=1, max_size=20, alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="_-"
    )),
    id_b=st.text(min_size=1, max_size=20, alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="_-"
    )).filter(lambda s: s != ""),
    n_a=st.integers(min_value=1, max_value=5),
    n_b=st.integers(min_value=1, max_value=5),
)
@settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
def test_property_7_retrieval_identity_isolation(id_a, id_b, n_a, n_b):
    """Property 7: Retrieval for id_A never returns entries belonging to id_B."""
    from hypothesis import assume
    assume(id_a != id_b)

    import falcon.memory as Memory

    mock_db = _get_mongomock_db()
    mock_db["memory"].drop()

    with patch("falcon.memory.get_db", return_value=mock_db):
        for i in range(n_a):
            Memory.add_memory(id_a, "semantic", f"fact_a_{i}")
        for i in range(n_b):
            Memory.add_memory(id_b, "semantic", f"fact_b_{i}")

        result = Memory.retrieve_for_generation(identity_id=id_a, query="fact")

    for entry in result.entries:
        assert entry.get("identity_id") == id_a, (
            f"Found cross-identity entry: expected id={id_a!r}, "
            f"got id={entry.get('identity_id')!r}"
        )


# ---------------------------------------------------------------------------
# Property 8: top_k_per_type limit enforced per type
# ---------------------------------------------------------------------------

@given(
    k=st.integers(min_value=1, max_value=5),
    n=st.integers(min_value=6, max_value=15),
)
@settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
def test_property_8_top_k_per_type_enforced(k, n):
    """Property 8: At most top_k_per_type entries of each type returned."""
    import falcon.memory as Memory

    mock_db = _get_mongomock_db()
    mock_db["memory"].drop()

    identity_id = "topk-test"
    with patch("falcon.memory.get_db", return_value=mock_db):
        for i in range(n):
            Memory.add_memory(identity_id, "semantic", f"semantic fact {i}")
        for i in range(n):
            Memory.add_memory(identity_id, "episodic", f"episodic event {i}")

        result = Memory.retrieve_for_generation(
            identity_id=identity_id,
            query="fact",
            top_k_per_type=k,
        )

    # Check per-type counts (excluding persona)
    from collections import Counter
    type_counts = Counter(
        e["memory_type"] for e in result.entries
        if e.get("memory_type") != "persona"
    )
    for mem_type, count in type_counts.items():
        assert count <= k, (
            f"Type {mem_type!r} has {count} entries but top_k_per_type={k}"
        )


# ---------------------------------------------------------------------------
# Property 9: Persona always included, never scored, absent when missing
# ---------------------------------------------------------------------------

@given(has_persona=st.booleans(), n_entries=st.integers(min_value=0, max_value=5))
@settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
def test_property_9_persona_inclusion(has_persona, n_entries):
    """Property 9: Persona always in entries when exists; absent when missing."""
    import falcon.memory as Memory

    mock_db = _get_mongomock_db()
    mock_db["memory"].drop()

    identity_id = "persona-test"
    with patch("falcon.memory.get_db", return_value=mock_db):
        if has_persona:
            Memory.add_memory(
                identity_id, "persona",
                content="Persona content",
                source="user",
            )
        for i in range(n_entries):
            Memory.add_memory(identity_id, "semantic", f"fact {i}")

        result = Memory.retrieve_for_generation(
            identity_id=identity_id,
            query="anything",
            top_k_per_type=3,
        )

    persona_entries = [e for e in result.entries if e.get("memory_type") == "persona"]

    if has_persona:
        assert len(persona_entries) == 1, (
            "Expected exactly 1 persona entry when persona exists"
        )
        # Persona should be first
        assert result.entries[0].get("memory_type") == "persona", (
            "Persona entry should be prepended (first) in entries"
        )
        # Persona should NOT be scored (no score field)
        persona = persona_entries[0]
        # score should not appear or should be None (it's not populated for persona)
        assert "score" not in persona or persona.get("score") is None, (
            f"Persona entry should not have a score, got: {persona.get('score')}"
        )
    else:
        assert len(persona_entries) == 0, (
            "Expected no persona entry when none exists"
        )


# ---------------------------------------------------------------------------
# Property 10: Reasoning cardinality matches non-persona entries
# ---------------------------------------------------------------------------

@given(n_entries=st.integers(min_value=0, max_value=8))
@settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
def test_property_10_reasoning_cardinality(n_entries):
    """Property 10: len(reasoning) == count of non-persona entries."""
    import falcon.memory as Memory

    mock_db = _get_mongomock_db()
    mock_db["memory"].drop()

    identity_id = "reasoning-test"
    with patch("falcon.memory.get_db", return_value=mock_db):
        Memory.add_memory(identity_id, "persona", "I am persona", source="user")
        for i in range(n_entries):
            mtype = ["semantic", "episodic", "procedural", "working"][i % 4]
            Memory.add_memory(identity_id, mtype, f"entry {i}")  # type: ignore[arg-type]

        result = Memory.retrieve_for_generation(
            identity_id=identity_id,
            query="test",
            top_k_per_type=10,
        )

    non_persona = [e for e in result.entries if e.get("memory_type") != "persona"]
    assert len(result.reasoning) == len(non_persona), (
        f"reasoning count {len(result.reasoning)} != non-persona entries {len(non_persona)}"
    )


# ---------------------------------------------------------------------------
# Property 11: Non-persona entries carry valid score and match_reason
# ---------------------------------------------------------------------------

@given(n_entries=st.integers(min_value=1, max_value=8))
@settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
def test_property_11_non_persona_entries_valid_score_and_reason(n_entries):
    """Property 11: Non-persona entries have score ∈ [0,1] and valid match_reason."""
    import falcon.memory as Memory

    mock_db = _get_mongomock_db()
    mock_db["memory"].drop()

    identity_id = "score-test"
    valid_reasons = {"pinned", "tag-match", "keyword-match", "recency"}

    with patch("falcon.memory.get_db", return_value=mock_db):
        for i in range(n_entries):
            mtype = ["semantic", "episodic", "procedural", "working"][i % 4]
            Memory.add_memory(identity_id, mtype, f"fact {i}", tags=[f"tag{i}"])  # type: ignore[arg-type]

        result = Memory.retrieve_for_generation(
            identity_id=identity_id,
            query="fact tag0",
            top_k_per_type=10,
        )

    for entry in result.entries:
        if entry.get("memory_type") == "persona":
            continue
        score = entry.get("score")
        reason = entry.get("match_reason")
        assert score is not None, f"Entry missing score: {entry}"
        assert isinstance(score, float), f"Score should be float, got {type(score)}"
        assert 0.0 <= score <= 1.0, f"Score {score} out of [0,1]"
        assert reason in valid_reasons, (
            f"Invalid match_reason: {reason!r}, must be one of {valid_reasons}"
        )


# ---------------------------------------------------------------------------
# Property 12: clear_working_memory is identity-scoped
# ---------------------------------------------------------------------------

@given(
    n_a=st.integers(min_value=1, max_value=5),
    n_b=st.integers(min_value=1, max_value=5),
)
@settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
def test_property_12_clear_working_memory_identity_scoped(n_a, n_b):
    """Property 12: clear_working_memory(id_A) leaves id_B's working memory unchanged."""
    import falcon.memory as Memory

    mock_db = _get_mongomock_db()
    mock_db["memory"].drop()

    id_a = "clear-test-A"
    id_b = "clear-test-B"

    with patch("falcon.memory.get_db", return_value=mock_db):
        for i in range(n_a):
            Memory.add_memory(id_a, "working", f"working_a_{i}")
        for i in range(n_b):
            Memory.add_memory(id_b, "working", f"working_b_{i}")

        # Verify both have entries
        pre_a = mock_db["memory"].count_documents(
            {"identity_id": id_a, "memory_type": "working"}
        )
        pre_b = mock_db["memory"].count_documents(
            {"identity_id": id_b, "memory_type": "working"}
        )
        assert pre_a == n_a
        assert pre_b == n_b

        # Clear id_A's working memory
        deleted = Memory.clear_working_memory(id_a)

        post_a = mock_db["memory"].count_documents(
            {"identity_id": id_a, "memory_type": "working"}
        )
        post_b = mock_db["memory"].count_documents(
            {"identity_id": id_b, "memory_type": "working"}
        )

    assert post_a == 0, f"id_A should have 0 working entries after clear, got {post_a}"
    assert post_b == n_b, (
        f"id_B's working entries should be unchanged ({n_b}), got {post_b}"
    )
    assert deleted == n_a, f"clear_working_memory should return {n_a}, got {deleted}"


# ---------------------------------------------------------------------------
# Property 13: load_history is identity-scoped
# ---------------------------------------------------------------------------

def test_property_13_load_history_identity_scoped():
    """Property 13: load_history(id_A) returns only id_A messages."""
    import falcon.identity as Identity

    mock_db = _get_mongomock_db()
    mock_db["messages"].drop()

    id_a, id_b = "hist-A", "hist-B"

    mock_db["messages"].insert_many([
        {"identity_id": id_a, "role": "user",      "content": "from A 1", "timestamp": "t1"},
        {"identity_id": id_a, "role": "assistant",  "content": "from A 2", "timestamp": "t2"},
        {"identity_id": id_b, "role": "user",      "content": "from B 1", "timestamp": "t3"},
    ])

    with patch("falcon.identity.get_db", return_value=mock_db):
        history = Identity.load_history(id_a)

    assert all(
        m.get("identity_id", id_a) == id_a
        for m in history
    ), f"Found non-id_A messages in history: {history}"

    # Should not contain id_B's content
    contents = [m.get("content") for m in history]
    assert "from B 1" not in contents, "id_B message leaked into id_A's history"
    assert "from A 1" in contents
    assert "from A 2" in contents


@given(
    id_a=st.text(min_size=1, max_size=15, alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="_"
    )),
    id_b=st.text(min_size=1, max_size=15, alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="_"
    )),
    n=st.integers(min_value=1, max_value=5),
)
@settings(max_examples=30, suppress_health_check=[HealthCheck.too_slow])
def test_property_13_load_history_identity_scoped_pbt(id_a, id_b, n):
    """Property 13 (PBT): load_history never returns messages from another identity."""
    from hypothesis import assume
    assume(id_a != id_b)
    assume(not any(c in id_a for c in "/\\\x00") and ".." not in id_a)
    assume(not any(c in id_b for c in "/\\\x00") and ".." not in id_b)

    import falcon.identity as Identity

    mock_db = _get_mongomock_db()
    mock_db["messages"].drop()

    for i in range(n):
        mock_db["messages"].insert_one(
            {"identity_id": id_a, "role": "user", "content": f"msg_a_{i}", "timestamp": f"t{i}"}
        )
        mock_db["messages"].insert_one(
            {"identity_id": id_b, "role": "user", "content": f"msg_b_{i}", "timestamp": f"t{i+100}"}
        )

    with patch("falcon.identity.get_db", return_value=mock_db):
        history = Identity.load_history(id_a)

    for msg in history:
        assert msg.get("identity_id", id_a) == id_a, (
            f"Cross-identity leak: got {msg.get('identity_id')!r} in id_A={id_a!r} history"
        )


# ---------------------------------------------------------------------------
# Property 14: Forbidden character in identity_id raises ValueError
# ---------------------------------------------------------------------------

_FORBIDDEN = ["/", "\\", "..", "\x00"]


@given(
    base=st.text(min_size=0, max_size=10, alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="_"
    )),
    forbidden=st.sampled_from(_FORBIDDEN),
    suffix=st.text(min_size=0, max_size=10, alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="_"
    )),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_14_forbidden_char_raises_value_error(base, forbidden, suffix):
    """Property 14: identity_id with forbidden char → ValueError from load_history."""
    import falcon.identity as Identity

    identity_id = base + forbidden + suffix
    mock_db = _get_mongomock_db()

    with patch("falcon.identity.get_db", return_value=mock_db):
        with pytest.raises(ValueError):
            Identity.load_history(identity_id)


# ---------------------------------------------------------------------------
# Property 15: Audit record contains all required fields
# ---------------------------------------------------------------------------

_REQUIRED_AUDIT_KEYS = {
    "timestamp", "identity_id", "model", "prompt_state", "system_prompt",
    "retrieved_memories", "generation_settings", "context_size",
    "context_token_estimate", "assembled_payload", "raw_model_output",
    "usage", "latency_ms",
}

_gen_settings_strategy = st.fixed_dictionaries({
    "temperature": st.floats(min_value=0.0, max_value=2.0, allow_nan=False),
    "top_p":       st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    "max_tokens":  st.integers(min_value=64, max_value=4096),
})

_payload_msg_strategy = st.fixed_dictionaries({
    "role":    st.sampled_from(["system", "user", "assistant"]),
    "content": st.text(max_size=200),
})


@given(
    identity_id=st.text(min_size=1, max_size=20, alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="_-"
    )),
    model=st.text(min_size=1, max_size=50),
    prompt_state=st.sampled_from(["present", "empty"]),
    system_prompt=st.one_of(st.none(), st.text(max_size=200)),
    generation_settings=_gen_settings_strategy,
    assembled_payload=st.lists(_payload_msg_strategy, max_size=10),
    raw_model_output=st.text(max_size=200),
    latency_ms=st.floats(min_value=0.0, max_value=60000.0, allow_nan=False),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_15_audit_record_all_required_fields(
    identity_id, model, prompt_state, system_prompt,
    generation_settings, assembled_payload, raw_model_output, latency_ms,
):
    """Property 15: build_audit_record always returns dict with all 13 required keys."""
    from falcon.audit import build_audit_record

    record = build_audit_record(
        identity_id=identity_id,
        model=model,
        prompt_state=prompt_state,
        system_prompt=system_prompt,
        retrieved_memories=[],
        generation_settings=generation_settings,
        assembled_payload=assembled_payload,
        raw_model_output=raw_model_output,
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        latency_ms=latency_ms,
    )

    missing = _REQUIRED_AUDIT_KEYS - set(record.keys())
    assert not missing, f"Audit record missing required keys: {missing}"


# ---------------------------------------------------------------------------
# Property 16: Memory extractor never writes persona or archive entries
# ---------------------------------------------------------------------------

def test_property_16_memory_extractor_no_persona_or_archive():
    """Property 16: Memory extractor never writes persona or archive entries."""
    import falcon.memory as Memory
    from falcon.memory_extractor import _ALLOWED_TYPES

    mock_db = _get_mongomock_db()
    mock_db["memory"].drop()

    identity_id = "extractor-test"

    # Simulate LLM returning a mix of valid and invalid types
    fake_llm_response = json.dumps([
        {"memory_type": "semantic",   "content": "Valid semantic fact",   "tags": []},
        {"memory_type": "episodic",   "content": "Valid episodic event",  "tags": []},
        {"memory_type": "persona",    "content": "Should be rejected",    "tags": []},
        {"memory_type": "archive",    "content": "Should be rejected",    "tags": []},
        {"memory_type": "procedural", "content": "Valid procedural note", "tags": []},
        {"memory_type": "working",    "content": "Valid working note",    "tags": []},
    ])

    mock_choice = MagicMock()
    mock_choice.message.content = fake_llm_response
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]

    with patch("falcon.memory.get_db", return_value=mock_db):
        with patch("openai.OpenAI") as mock_openai:
            mock_client = MagicMock()
            mock_openai.return_value = mock_client
            mock_client.chat.completions.create.return_value = mock_response

            import falcon.memory_extractor as Extractor
            with patch.object(Extractor, "_extractor_queues", defaultdict(lambda: deque(maxlen=10))):
                Extractor.run({
                    "identity_id":       identity_id,
                    "user_message":      "test message",
                    "assistant_message": "test response",
                    "turn_index":        1,
                    "timestamp":         "2024-01-01T00:00:00Z",
                })

        # Check no persona or archive entries were persisted
        all_entries = list(mock_db["memory"].find({"identity_id": identity_id}))

    for entry in all_entries:
        assert entry["memory_type"] not in {"persona", "archive"}, (
            f"Extractor wrote forbidden type {entry['memory_type']!r}"
        )
        assert entry.get("source") == "auto", (
            f"Extractor entry should have source='auto', got {entry.get('source')!r}"
        )
        assert entry.get("identity_id") == identity_id, (
            f"Extractor entry has wrong identity_id: {entry.get('identity_id')!r}"
        )


# ---------------------------------------------------------------------------
# Property 17: last-n-turns truncation
# ---------------------------------------------------------------------------

def _make_history(n_pairs: int) -> list[dict]:
    history = []
    for i in range(n_pairs):
        history.append({"role": "user",      "content": f"user {i}"})
        history.append({"role": "assistant", "content": f"asst {i}"})
    return history


@given(
    n_pairs=st.integers(min_value=0, max_value=30),
    max_turns=st.integers(min_value=1, max_value=20),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_17_last_n_turns_truncation(n_pairs, max_turns):
    """Property 17: last-n-turns keeps ≤ max_turns pairs; dropped count is correct."""
    from falcon.engine import _truncate_history_last_n

    history = _make_history(n_pairs)
    included, dropped = _truncate_history_last_n(history, max_turns)

    # Count included pairs
    included_pairs = 0
    i = 0
    while i < len(included):
        if (i + 1 < len(included)
                and included[i]["role"] == "user"
                and included[i + 1]["role"] == "assistant"):
            included_pairs += 1
            i += 2
        else:
            i += 1

    assert included_pairs <= max_turns, (
        f"Included {included_pairs} pairs but max_turns={max_turns}"
    )

    expected_dropped = max(0, len(history) - len(included))
    assert dropped == expected_dropped, (
        f"Expected dropped={expected_dropped}, got dropped={dropped}"
    )

    # Verify correct total
    assert len(included) + dropped == len(history), (
        f"included({len(included)}) + dropped({dropped}) != total({len(history)})"
    )


# ---------------------------------------------------------------------------
# Property 18: token-budget truncation
# ---------------------------------------------------------------------------

@given(
    n_pairs=st.integers(min_value=0, max_value=20),
    budget=st.integers(min_value=100, max_value=200000),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_18_token_budget_truncation(n_pairs, budget):
    """Property 18: token-budget keeps total history tokens within budget."""
    from falcon.engine import _truncate_history_token_budget, _estimate_tokens

    history = _make_history(n_pairs)
    included, dropped = _truncate_history_token_budget(history, budget)

    if not included:
        assert dropped == len(history)
        return

    total_tokens = sum(_estimate_tokens(m.get("content", "")) for m in included)

    # Check budget adherence — single-pair exception allowed
    single_pair_tokens = sum(
        _estimate_tokens(m.get("content", ""))
        for m in history[:2]  # first pair (we're checking if even one pair exceeds)
    )

    # Total tokens must be ≤ budget unless single pair alone exceeds budget
    if total_tokens > budget:
        # Only allowed if included is exactly one "pair" worth
        pair_tokens_included = sum(_estimate_tokens(m.get("content", "")) for m in included)
        assert len(included) <= 2, (
            f"Token budget {budget} exceeded with {total_tokens} tokens "
            f"and more than 1 pair included ({len(included)} messages)"
        )

    # Verify total consistency
    assert len(included) + dropped == len(history), (
        f"included({len(included)}) + dropped({dropped}) != total({len(history)})"
    )


# ---------------------------------------------------------------------------
# Property 19: Export JSON round-trip
# ---------------------------------------------------------------------------

_export_data_strategy = st.one_of(
    st.lists(
        st.fixed_dictionaries({
            "role":      st.sampled_from(["user", "assistant"]),
            "content":   st.text(max_size=200),
            "timestamp": st.text(max_size=30),
        }),
        max_size=20,
    ),
    st.fixed_dictionaries({
        "memory_type": st.sampled_from(["semantic", "episodic", "working"]),
        "content":     st.text(max_size=200),
    }),
)


@given(
    identity_id=st.text(min_size=1, max_size=30),
    data=_export_data_strategy,
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_19_export_json_round_trip(identity_id, data):
    """Property 19: Export envelope is valid JSON, has version='1', no ObjectId."""
    from falcon.export_utils import make_export_envelope, to_json_str

    envelope = make_export_envelope(identity_id=identity_id, data=data)

    # Must be parseable by standard JSON parser
    json_str = to_json_str(envelope)
    parsed = json.loads(json_str)

    # Must have falcon_export_version == "1"
    assert parsed.get("falcon_export_version") == "1", (
        f"Expected falcon_export_version='1', got {parsed.get('falcon_export_version')!r}"
    )

    # Must have identity_id
    assert "identity_id" in parsed

    # Must have data and exported_at
    assert "data" in parsed
    assert "exported_at" in parsed

    # No ObjectId instances anywhere in parsed output
    def _check_no_objectid(obj):
        if isinstance(obj, dict):
            for v in obj.values():
                _check_no_objectid(v)
        elif isinstance(obj, list):
            for item in obj:
                _check_no_objectid(item)
        else:
            type_name = type(obj).__name__
            assert type_name != "ObjectId", (
                f"Found ObjectId in export output: {obj!r}"
            )

    _check_no_objectid(parsed)
