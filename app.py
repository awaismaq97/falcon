"""
app.py — Falcon Streamlit UI

Tabs: Chat | Context | Memory | Audit | Logs

Falcon is a transparent inference environment.
It is not an assistant, chatbot, agent, or coach.
It is an inference layer with full context visibility.

Design principles enforced in this UI:
  - No default assistant fallback: empty system prompt = empty instruction context.
  - Always output: the system never fails silently for valid input.
  - Full transparency: every component entering generation is visible.
  - Generation controls: temperature, top_p, repetition_penalty, max_tokens,
    stop_tokens — all visible and adjustable.
  - Identity isolation: identities do not contaminate each other.
  - Audit trail: every inference event is logged completely.
  - Memory: user-controlled, visible retrieval with reasoning.
"""

import json
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Process-level extraction completion flags (thread-safe, outside session state)
# key = identity_id, value = timestamp of last completed extraction
# ---------------------------------------------------------------------------
_extraction_done: dict[str, float] = defaultdict(float)

import streamlit as st

# ---------------------------------------------------------------------------
# Config — fail fast with a clear message, never silently
# ---------------------------------------------------------------------------
try:
    import falcon.config as Config
except ValueError as exc:
    st.error(str(exc))
    st.stop()

import falcon.engine   as Engine
import falcon.identity as Identity
import falcon.logger   as Logger
import falcon.audit    as Audit
import falcon.memory   as Memory
from falcon.db import get_db
from falcon.export_utils import make_export_envelope, to_json_str


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3] + "Z"


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log_path(identity_id: str) -> str:
    return f"mongodb://falcon/messages [{identity_id}]"


# ---------------------------------------------------------------------------
# Generation settings helpers
# ---------------------------------------------------------------------------

def _get_gen_settings() -> dict:
    """Return current generation settings from session state."""
    return {
        "temperature":        st.session_state.get("gen_temperature",        Config.generation_temperature),
        "top_p":              st.session_state.get("gen_top_p",              Config.generation_top_p),
        "repetition_penalty": st.session_state.get("gen_repetition_penalty", Config.generation_repetition_penalty),
        "stop_tokens":        st.session_state.get("gen_stop_tokens",        Config.generation_stop_tokens),
    }


# ---------------------------------------------------------------------------
# Token persistence
# ---------------------------------------------------------------------------

def _load_persisted_tokens(identity_id: str) -> dict:
    db  = get_db()
    doc = db["tokens"].find_one({"identity_id": identity_id}, {"_id": 0})
    if not doc:
        return {"prompt": 0, "completion": 0, "total": 0}
    return {
        "prompt":     doc.get("prompt", 0),
        "completion": doc.get("completion", 0),
        "total":      doc.get("total", 0),
    }


def _persist_tokens(identity_id: str, tokens: dict) -> None:
    db = get_db()
    db["tokens"].update_one(
        {"identity_id": identity_id},
        {"$set": {
            "identity_id": identity_id,
            "prompt":      tokens.get("prompt", 0),
            "completion":  tokens.get("completion", 0),
            "total":       tokens.get("total", 0),
        }},
        upsert=True,
    )


# ---------------------------------------------------------------------------
# Per-rerun in-memory cache
# ---------------------------------------------------------------------------

def _cache_key_messages(identity_id: str) -> str:
    return f"_cache_messages_{identity_id}"

def _cache_key_traces(identity_id: str) -> str:
    return f"_cache_traces_{identity_id}"

def _invalidate_cache(identity_id: str) -> None:
    st.session_state.pop(_cache_key_messages(identity_id), None)
    st.session_state.pop(_cache_key_traces(identity_id), None)


# ---------------------------------------------------------------------------
# Message log helpers
# ---------------------------------------------------------------------------

def _read_log_entries(identity_id: str) -> list[dict] | None:
    key = _cache_key_messages(identity_id)
    if key not in st.session_state:
        try:
            st.session_state[key] = Identity.load_history(identity_id)
        except Exception:
            return None
    return st.session_state[key]


def _read_log_raw(identity_id: str) -> str:
    entries = _read_log_entries(identity_id) or []
    return json.dumps(entries, indent=2, ensure_ascii=False)


def _save_entries(identity_id: str, entries: list[dict]) -> None:
    db = get_db()
    db["messages"].delete_many({"identity_id": identity_id})
    if entries:
        db["messages"].insert_many([
            {
                "identity_id": identity_id,
                "timestamp":   e.get("timestamp", ""),
                "role":        e.get("role", "user"),
                "content":     e.get("content", ""),
            }
            for e in entries
        ])
    st.session_state[_cache_key_messages(identity_id)] = list(entries)


# ---------------------------------------------------------------------------
# Trace helpers
# ---------------------------------------------------------------------------

def _read_traces(identity_id: str) -> list[dict]:
    key = _cache_key_traces(identity_id)
    if key not in st.session_state:
        db     = get_db()
        cursor = db["traces"].find(
            {"identity_id": identity_id},
            {"_id": 0, "identity_id": 0},
        )
        st.session_state[key] = list(cursor)
    return st.session_state[key]


def _append_trace(identity_id: str, snapshot: dict) -> None:
    db = get_db()
    db["traces"].insert_one({"identity_id": identity_id, **snapshot})
    key = _cache_key_traces(identity_id)
    if key in st.session_state:
        st.session_state[key].append(snapshot)


def _delete_trace_for_timestamp(identity_id: str, user_ts: str) -> None:
    db = get_db()
    db["traces"].delete_one({"identity_id": identity_id, "user_timestamp": user_ts})
    key = _cache_key_traces(identity_id)
    if key in st.session_state:
        st.session_state[key] = [
            t for t in st.session_state[key]
            if t.get("user_timestamp") != user_ts
        ]


# ---------------------------------------------------------------------------
# History pair helpers
# ---------------------------------------------------------------------------

def _pair_count(history: list) -> int:
    pairs, i = 0, 0
    while i < len(history):
        if (i + 1 < len(history)
                and history[i].get("role") == "user"
                and history[i + 1].get("role") == "assistant"):
            i += 2
        else:
            i += 1
        pairs += 1
    return pairs


def _build_pairs(entries: list[dict]) -> list[tuple[int, list[int], bool]]:
    pairs, i, pnum = [], 0, 1
    while i < len(entries):
        if (i + 1 < len(entries)
                and entries[i].get("role") == "user"
                and entries[i + 1].get("role") == "assistant"):
            pairs.append((pnum, [i, i + 1], True))
            i += 2
        else:
            pairs.append((pnum, [i], False))
            i += 1
        pnum += 1
    return pairs


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

def _init_session_state() -> None:
    defaults = {
        "identity_id":              "default",
        "history":                  [],
        "selected_model":           Config.default_model,
        "last_payload":             None,
        "last_response":            None,
        "last_context_view":        None,
        "last_retrieved_memory":    None,
        "trace_log":                [],
        "session_tokens":           {"prompt": 0, "completion": 0, "total": 0},
        "_loaded_initial_history":  False,
        "_confirm_clear":           False,
        "_confirm_pair":            None,
        "_delete_confirmed":        False,
        "_confirm_delete_identity": False,
        "_view_trace_ts":           None,
        "_view_payload_ts":         None,
        "_show_context_viewer":     False,
        "_confirm_audit_delete":    False,
        # System prompt
        "use_system_prompt":        True,    # on by default — neutral text-processor prompt active
        "system_prompt_text":       Config.default_system_prompt,
        # Generation controls
        "gen_temperature":          Config.generation_temperature,
        "gen_top_p":                Config.generation_top_p,
        "gen_repetition_penalty":   Config.generation_repetition_penalty,
        "gen_stop_tokens":          Config.generation_stop_tokens,
        # Memory
        "_memory_tab_type":         "episodic",
        "_memory_add_content":      "",
        # History truncation
        "history_max_turns":           Config.history_max_turns,
        # Last context snapshot (annotated)
        "last_context_snapshot":    None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def _load_identity_history(identity_id: str) -> None:
    try:
        st.session_state.history = Identity.load_history(identity_id)
    except Exception as exc:
        st.error(f"Failed to load history for '{identity_id}': {exc}")


def _restore_token_totals(identity_id: str) -> None:
    st.session_state.session_tokens = _load_persisted_tokens(identity_id)


# ---------------------------------------------------------------------------
# Trace renderer
# ---------------------------------------------------------------------------

def _render_trace_steps(steps: list[dict]) -> None:
    sections = []
    for entry in steps:
        t, stage, data = entry["t"], entry["stage"], entry["data"]
        status  = entry.get("status", "info")
        elapsed = entry.get("elapsed_ms")
        icon    = {"info": "◦", "success": "✓", "error": "✗", "warn": "⚠"}.get(status, "◦")
        estr    = f" `+{elapsed}ms`" if elapsed is not None else ""
        if isinstance(data, (dict, list)):
            sections.append(
                f"**`{t}`**{estr} {icon} **{stage}**\n"
                f"```json\n{json.dumps(data, indent=2, ensure_ascii=False)}\n```"
            )
        else:
            sections.append(f"**`{t}`**{estr} {icon} **{stage}**\n```\n{data}\n```")
    st.markdown("\n\n---\n\n".join(sections))


# ---------------------------------------------------------------------------
# Raw Context Viewer
# ---------------------------------------------------------------------------

def _render_context_view(cv: dict) -> None:
    """Render the assembled context with color-coded source annotation."""
    prompt_state = cv.get("prompt_state", "empty")
    sp_color = "#22c55e" if prompt_state == "present" else "#ef4444"
    sp_label = f"<span style='color:{sp_color};font-family:monospace'>{prompt_state}</span>"

    st.markdown(f"**Prompt state:** {sp_label}", unsafe_allow_html=True)

    # Source color map
    SOURCE_COLORS = {
        "system-prompt":  "#3b82f6",   # blue
        "persona":        "#a855f7",   # purple
        "memory":         "#f59e0b",   # amber
        "history":        "#64748b",   # slate
        "user-input":     "#22c55e",   # green
        "history-summary": "#06b6d4",  # cyan
    }
    SOURCE_LABELS = {
        "system-prompt":  "Platform / System",
        "persona":        "Persona",
        "memory":         "Platform / Memory",
        "history":        "History",
        "user-input":     "User",
        "history-summary": "History Summary",
    }

    # Use annotated_payload if available, fall back to assembled_payload
    annotated = cv.get("annotated_payload") or []
    assembled = cv.get("assembled_payload") or []

    # Metrics row
    history_included = cv.get("history_included") or []
    history_dropped  = cv.get("history_dropped_turns", 0)
    memory_entries   = cv.get("memory_entries") or []

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Messages in payload", cv.get("message_count", len(assembled)))
    col2.metric("History included", len(history_included))
    col3.metric("Dropped turns", history_dropped)
    col4.metric("Memory entries", len(memory_entries))

    st.divider()

    if annotated:
        st.markdown("**Annotated Payload** (color-coded by source origin)")
        for i, elem in enumerate(annotated):
            src     = elem.get("source", "history")
            color   = SOURCE_COLORS.get(src, "#64748b")
            label   = SOURCE_LABELS.get(src, src)
            role    = elem.get("role", "")
            content = elem.get("content", "")
            preview = content[:300] + ("…" if len(content) > 300 else "")

            st.markdown(
                f"<div style='border-left:3px solid {color};padding:6px 12px;"
                f"margin:4px 0;background:#161b27;border-radius:4px'>"
                f"<span style='color:{color};font-size:0.72rem;font-weight:600;"
                f"font-family:monospace;text-transform:uppercase'>{label}</span>"
                f"<span style='color:#475569;font-size:0.72rem;margin-left:8px'>{role}</span>"
                f"<div style='color:#cbd5e1;font-size:0.83rem;margin-top:4px'>{preview}</div>"
                f"</div>",
                unsafe_allow_html=True,
            )

        # Dropped-turn placeholder (Req 21.8)
        if history_dropped and history_dropped > 0:
            st.markdown(
                f"<div style='border-left:3px solid #374151;padding:6px 12px;"
                f"margin:4px 0;background:#111827;border-radius:4px;"
                f"color:#6b7280;font-style:italic;font-size:0.82rem'>"
                f"▸ [{history_dropped} turn{'s' if history_dropped != 1 else ''} truncated]"
                f"</div>",
                unsafe_allow_html=True,
            )
    else:
        # Fallback: simple system prompt + history display
        with st.expander("① System Prompt", expanded=True):
            sp = cv.get("system_prompt")
            if sp:
                st.code(sp, language=None)
            else:
                st.caption("_None — empty instruction context. No assistant persona injected._")

        with st.expander(f"② Conversation History ({len(cv.get('conversation_history',[]))} entries)"):
            hist = cv.get("conversation_history", [])
            if hist:
                st.json(hist)
            else:
                st.caption("_Empty_")

        with st.expander(f"③ Retrieved Memory ({len(cv.get('retrieved_memory',[]))} entries)"):
            mem = cv.get("retrieved_memory", [])
            if mem:
                st.json(mem)
            else:
                st.caption("_No memory retrieved_")

    st.divider()

    # Full assembled payload JSON
    with st.expander("Full Assembled Payload (exact input to model)", expanded=False):
        st.json(assembled or annotated, expanded=True)


# ---------------------------------------------------------------------------
# Send flow
# ---------------------------------------------------------------------------

def _handle_send(user_input: str) -> None:
    identity_id = st.session_state.identity_id
    model       = st.session_state.selected_model
    gen         = _get_gen_settings()

    # Falcon spec: empty system prompt = empty instruction context.
    if st.session_state.get("use_system_prompt", False):
        system_prompt = st.session_state.get("system_prompt_text", "").strip()
    else:
        system_prompt = ""

    prompt_state = "present" if system_prompt else "empty"

    # Truncation settings from session state
    history_max_turns = st.session_state.get("history_max_turns", Config.history_max_turns)

    trace: list[dict] = []
    t0 = time.monotonic()

    def _push(stage: str, data, status: str = "info"):
        trace.append({
            "t":          _ts(),
            "stage":      stage,
            "data":       data,
            "status":     status,
            "elapsed_ms": round((time.monotonic() - t0) * 1000),
        })

    _push("config", {
        "model":              model,
        "temperature":        gen["temperature"],
        "top_p":              gen["top_p"],
        "repetition_penalty": gen["repetition_penalty"],
        "stop_tokens":        gen["stop_tokens"],
        "identity":           identity_id,
        "prompt_state":       prompt_state,
        "truncation_strategy": "last-n-turns",
    })

    user_ts = _utc_iso()

    # Log user message
    try:
        Logger.append_message(identity_id, "user", user_input, timestamp=user_ts)
    except Exception as exc:
        _push("ERROR — log user message", str(exc), status="error")
        st.error(f"Failed to log user message: {exc}")
        st.session_state.trace_log = trace
        return

    _push("user → logged", {"collection": "messages", "identity": identity_id,
                             "entry": {"role": "user", "content": user_input}})

    # Memory retrieval — visible, with reasoning
    try:
        retrieval = Memory.retrieve_for_generation(
            identity_id=identity_id,
            query=user_input,
            top_k_per_type=Config.top_k_per_type,
            recency_weight=Config.recency_weight,
            relevance_weight=Config.relevance_weight,
        )
        retrieved_entries = retrieval.entries
        _push("memory retrieved", retrieval.to_display_dict())
    except Exception as exc:
        retrieval = None
        retrieved_entries = []
        _push("memory retrieval failed", str(exc), status="warn")

    # Build messages for model (full history + current user turn)
    messages = list(st.session_state.history) + [
        {"role": "user", "content": user_input}
    ]
    messages_for_model = [{"role": m["role"], "content": m["content"]} for m in messages]

    # Build annotated payload with truncation and source annotation
    try:
        annotated_payload, context_snapshot = Engine.build_annotated_payload(
            system_prompt=system_prompt,
            messages=messages_for_model,
            memory_block=retrieved_entries,
            truncation_strategy="last-n-turns",
            history_max_turns=history_max_turns,
        )
        if retrieval is not None:
            context_snapshot["retrieval_result"] = retrieval.to_display_dict()
        raw_payload = [{"role": e["role"], "content": e["content"]} for e in annotated_payload]
    except Exception as exc:
        _push("ERROR — build_annotated_payload", str(exc), status="error")
        # Fallback to simple build_payload
        raw_payload = Engine.build_payload(system_prompt, messages_for_model)
        context_snapshot = Engine.build_context_view(
            system_prompt=system_prompt,
            messages=messages_for_model,
            retrieved_memory=retrieved_entries,
        )
        annotated_payload = raw_payload

    st.session_state.last_context_view     = context_snapshot
    st.session_state.last_context_snapshot = context_snapshot
    st.session_state.last_retrieved_memory = retrieved_entries

    _push("payload built", {"message_count": len(raw_payload), "payload": raw_payload})

    # Check assistant language patterns (banner shown after response)
    assistant_language_patterns = Config.assistant_language_patterns

    _push("→ OpenRouter API call (streaming)", {
        "model":              model,
        "temperature":        gen["temperature"],
        "top_p":              gen["top_p"],
        "repetition_penalty": gen["repetition_penalty"],
        "stop_tokens":        gen["stop_tokens"],
        "messages_count":     len(raw_payload),
    })

    # Stream response
    api_t0 = time.monotonic()
    response_text = ""
    stream_gen = Engine.stream_inference(
        model_name=model,
        payload=raw_payload,
        api_key=Config.OPENROUTER_API_KEY,
        temperature=gen["temperature"],
        top_p=gen["top_p"],
        repetition_penalty=gen["repetition_penalty"],
        stop_tokens=gen["stop_tokens"],
    )

    try:
        with st.chat_message("assistant"):
            response_text = st.write_stream(stream_gen)
    except Exception as exc:
        _push("ERROR — OpenRouter API", str(exc), status="error")
        st.error(f"Inference failed: {exc}")
        st.session_state.history   = Identity.load_history(identity_id)
        st.session_state.trace_log = trace
        return

    api_latency_ms = round((time.monotonic() - api_t0) * 1000)

    # Always-output guarantee
    if not response_text or not response_text.strip():
        response_text = "[no output]"

    # Assistant-language warning banner (Req 17.5, 16.5)
    if not st.session_state.get("use_system_prompt", True):
        for pattern in assistant_language_patterns:
            if pattern.lower() in response_text.lower():
                st.warning(
                    f"⚠️ Assistant-language pattern detected in response "
                    f"(system prompt is OFF): `{pattern}`"
                )
                break

    _push("← response complete", {"latency_ms": api_latency_ms, "content": response_text})

    # Token usage
    usage = stream_gen.usage
    if usage:
        st.session_state.session_tokens["prompt"]     += usage.get("prompt_tokens", 0)
        st.session_state.session_tokens["completion"] += usage.get("completion_tokens", 0)
        st.session_state.session_tokens["total"]      += usage.get("total_tokens", 0)

    _push("token usage", {
        "this_call": usage,
        "session_cumulative": {
            "prompt_tokens":     st.session_state.session_tokens["prompt"],
            "completion_tokens": st.session_state.session_tokens["completion"],
            "total_tokens":      st.session_state.session_tokens["total"],
        },
    })

    asst_ts = _utc_iso()

    # Build final history without a DB fetch
    final_history = list(st.session_state.history) + [
        {"timestamp": user_ts,  "role": "user",      "content": user_input},
        {"timestamp": asst_ts,  "role": "assistant",  "content": response_text},
    ]
    _invalidate_cache(identity_id)

    _push("session state updated", {
        "total_entries":  len(final_history),
        "identity":       identity_id,
    }, status="success")

    # Persist trace snapshot
    snapshot = {
        "user_timestamp":  user_ts,
        "send_timestamp":  _ts(),
        "user":            user_input,
        "steps":           trace,
        "context_snapshot": context_snapshot,
    }
    _append_trace(identity_id, snapshot)

    # ── Post-generation background tasks (Req 22.1–22.4) ─────────────────
    # Build immutable turn snapshot for extractor
    turn_snapshot = {
        "identity_id":       identity_id,
        "user_message":      user_input,
        "assistant_message": response_text,
        "turn_index":        len(final_history) // 2,
        "timestamp":         asst_ts,
    }

    # Capture token values NOW (in main thread) before launching background threads.
    # Background threads must never access st.session_state.
    _token_snapshot = {
        "prompt":     st.session_state.session_tokens.get("prompt", 0),
        "completion": st.session_state.session_tokens.get("completion", 0),
        "total":      st.session_state.session_tokens.get("total", 0),
    }

    def _bg_audit():
        try:
            if Config.audit_enabled:
                audit_record = Audit.build_audit_record(
                    identity_id=identity_id,
                    model=model,
                    prompt_state=prompt_state,
                    system_prompt=system_prompt if system_prompt else None,
                    retrieved_memories=[
                        {"type": e.get("memory_type"), "content": e.get("content")}
                        for e in retrieved_entries
                    ],
                    generation_settings=gen,
                    context_size=len(raw_payload),
                    context_token_estimate=context_snapshot.get("context_token_estimate", 0),
                    assembled_payload=raw_payload,
                    raw_model_output=getattr(stream_gen, "raw_output", response_text),
                    usage=usage or {},
                    latency_ms=api_latency_ms,
                )
                Audit.write_audit_record(identity_id, audit_record)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).error(
                "bg_audit failed for identity=%s: %s", identity_id, exc
            )

    def _bg_tokens():
        try:
            _persist_tokens(identity_id, _token_snapshot)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).error(
                "bg_tokens failed for identity=%s: %s", identity_id, exc
            )

    def _bg_extractor():
        try:
            if Config.memory_extraction_enabled:
                import falcon.memory_extractor as MemoryExtractor
                MemoryExtractor.run(turn_snapshot)
                # Signal completion via process-level dict (thread-safe).
                # Session state cannot be written from background threads.
                _extraction_done[identity_id] = time.monotonic()
        except Exception as exc:
            import logging
            logging.getLogger(__name__).error(
                "bg_extractor failed for identity=%s: %s", identity_id, exc
            )

    # Log assistant message synchronously first (before threads)
    try:
        Logger.append_message(identity_id, "assistant", response_text, timestamp=asst_ts)
    except Exception as exc:
        _push("ERROR — log assistant response", str(exc), status="error")

    _push("assistant → logged", {"collection": "messages", "identity": identity_id,
                                  "entry": {"role": "assistant", "content": response_text}})

    # Audit and token persist in background (no UI dependency)
    threading.Thread(target=_bg_audit,  daemon=True).start()
    threading.Thread(target=_bg_tokens, daemon=True).start()

    # Memory extraction — run synchronously so the Memory tab reflects new
    # entries immediately after st.rerun() (called by _render_chat_tab).
    # It's fast enough (1-3s LLM call) and runs after the response is shown.
    if Config.memory_extraction_enabled:
        try:
            import falcon.memory_extractor as MemoryExtractor
            MemoryExtractor.run(turn_snapshot)
            # Signal completion so the _bg_poller fragment can also trigger a
            # rerun if the synchronous path finishes after the rerun boundary.
            _extraction_done[identity_id] = time.monotonic()
        except Exception as exc:
            import logging
            logging.getLogger(__name__).error(
                "extractor failed for identity=%s: %s", identity_id, exc
            )
            st.warning(f"⚠️ Memory extraction failed: {exc}")

    st.session_state.last_payload  = raw_payload
    st.session_state.last_response = response_text
    st.session_state.history       = final_history
    st.session_state.trace_log     = trace


# ---------------------------------------------------------------------------
# Clear
# ---------------------------------------------------------------------------

def _handle_clear() -> None:
    identity_id = st.session_state.identity_id
    db = get_db()
    try:
        db["messages"].delete_many({"identity_id": identity_id})
    except Exception as exc:
        st.error(f"Failed to clear conversation: {exc}")
        return
    db["traces"].delete_many({"identity_id": identity_id})
    db["tokens"].delete_one({"identity_id": identity_id})
    db["audit_log"].delete_many({"identity_id": identity_id})
    # Delete all memory except persona — persona is identity-level, not session-level
    db["memory"].delete_many({
        "identity_id": identity_id,
        "memory_type": {"$ne": "persona"},
    })
    _invalidate_cache(identity_id)
    st.session_state.history        = []
    st.session_state.last_payload   = None
    st.session_state.last_response  = None
    st.session_state.last_context_view     = None
    st.session_state.last_context_snapshot = None
    st.session_state.last_retrieved_memory = None
    st.session_state.trace_log      = []
    st.session_state.session_tokens = {"prompt": 0, "completion": 0, "total": 0}


# ---------------------------------------------------------------------------
# Tab: Chat
# ---------------------------------------------------------------------------

def _render_chat_tab(user_input: str | None) -> None:
    history     = st.session_state.history
    identity_id = st.session_state.identity_id
    traces      = _read_traces(identity_id)
    trace_by_ts: dict[str, list[dict]] = {
        t["user_timestamp"]: t["steps"]
        for t in traces
        if "user_timestamp" in t
    }

    def _payload_for_ts(user_ts: str) -> list[dict] | None:
        steps = trace_by_ts.get(user_ts)
        if not steps:
            return None
        for step in steps:
            if step.get("stage") == "payload built":
                data = step.get("data", {})
                if isinstance(data, dict):
                    return data.get("payload")
        return None

    # Empty state
    if not history and not user_input:
        st.markdown("""
        <div class="empty-chat">
            <div class="icon">🦅</div>
            <div class="title">Falcon</div>
            <div class="sub">Transparent inference environment. Type a message to begin.</div>
        </div>
        """, unsafe_allow_html=True)
    else:
        i = 0
        while i < len(history):
            entry = history[i]
            role  = entry.get("role", "user")

            if (role == "user"
                    and i + 1 < len(history)
                    and history[i + 1].get("role") == "assistant"):

                with st.chat_message("user"):
                    st.markdown(entry.get("content", ""))

                asst = history[i + 1]
                with st.chat_message("assistant"):
                    st.markdown(asst.get("content", ""))

                user_ts = entry.get("timestamp", "")
                payload = _payload_for_ts(user_ts)
                if payload is not None:
                    btn_key = f"_chat_payload_{user_ts.replace(':', '').replace('.', '')}"
                    if st.button(
                        "⌥ context",
                        key=btn_key,
                        help="Show the exact assembled context sent to the model for this turn",
                        type="secondary",
                    ):
                        st.session_state._view_payload_ts = user_ts
                        st.rerun()

                i += 2
            else:
                with st.chat_message(role):
                    st.markdown(entry.get("content", ""))
                i += 1

        # Context dialog trigger
        if st.session_state.get("_view_payload_ts") is not None:
            vts     = st.session_state._view_payload_ts
            payload = _payload_for_ts(vts)
            if payload is not None:
                _show_payload_dialog(payload)
            st.session_state._view_payload_ts = None

    # New message
    if user_input and user_input.strip():
        with st.chat_message("user"):
            st.markdown(user_input)
        _handle_send(user_input)
        st.rerun()

    # Footer controls
    if history:
        st.markdown('<div style="height:4px"></div>', unsafe_allow_html=True)
        col_clear, col_spacer = st.columns([1, 5])
        with col_clear:
            st.markdown('<div class="clear-btn">', unsafe_allow_html=True)
            if st.button("Clear conversation", key="_clear_btn", use_container_width=True):
                st.session_state._confirm_clear = True
            st.markdown('</div>', unsafe_allow_html=True)

    if st.session_state._confirm_clear:
        st.markdown("""
        <div class="confirm-bar">
            ⚠️ This will permanently delete the conversation, traces, tokens, audit records, and all memory entries (except Persona).
        </div>
        """, unsafe_allow_html=True)
        c1, c2, _ = st.columns([1, 1, 4])
        with c1:
            if st.button("Confirm", type="primary", use_container_width=True, key="_clear_confirm"):
                _handle_clear()
                st.session_state._confirm_clear = False
                st.rerun()
        with c2:
            if st.button("Cancel", use_container_width=True, key="_clear_cancel"):
                st.session_state._confirm_clear = False
                st.rerun()


# ---------------------------------------------------------------------------
# Tab: Context Viewer
# ---------------------------------------------------------------------------

def _render_context_tab() -> None:
    """Raw Context Viewer — shows every component entering generation."""
    st.caption(
        "Every component that entered the last generation call. "
        "Nothing is hidden. Send a message to populate."
    )

    cv = st.session_state.get("last_context_snapshot") or st.session_state.get("last_context_view")
    if cv is None:
        st.info("No generation has run yet in this session. Send a message to see the assembled context.")
        return

    # Export context snapshot button (Req 10.4)
    identity_id = st.session_state.identity_id
    envelope = make_export_envelope(identity_id=identity_id, data=cv)
    st.download_button(
        label="⬇ Export context snapshot",
        data=to_json_str(envelope),
        file_name=f"falcon_context_{identity_id}_{_utc_iso().replace(':','-')}.json",
        mime="application/json",
        key="_ctx_export_btn",
    )

    _render_context_view(cv)


# ---------------------------------------------------------------------------
# Tab: Memory
# ---------------------------------------------------------------------------

def _render_memory_tab() -> None:
    identity_id = st.session_state.identity_id

    st.caption(f"User-controlled memory for identity `{identity_id}`. All retrieval is visible.")

    # Disabled extraction banner (Req 19.10)
    if not Config.memory_extraction_enabled:
        st.warning("🔕 Automatic memory extraction is disabled.")

    # Export memory button (Req 10.2)
    all_entries = Memory.get_memories(identity_id, limit=1000)
    export_env  = make_export_envelope(identity_id=identity_id, data=all_entries)
    st.download_button(
        label="⬇ Export memory",
        data=to_json_str(export_env),
        file_name=f"falcon_memory_{identity_id}_{_utc_iso().replace(':','-')}.json",
        mime="application/json",
        key="_mem_export_btn",
    )

    # Test retrieval (Req 9.9)
    with st.expander("🔍 Test Retrieval", expanded=False):
        test_q = st.text_input("Query", placeholder="Type a query to test retrieval…", key="_mem_test_query")
        if st.button("Run retrieval", key="_mem_test_btn"):
            if test_q.strip():
                try:
                    result = Memory.retrieve_for_generation(
                        identity_id=identity_id,
                        query=test_q,
                        top_k_per_type=Config.top_k_per_type,
                        recency_weight=Config.recency_weight,
                        relevance_weight=Config.relevance_weight,
                    )
                    st.success(f"Found {len(result.entries)} entries")
                    st.json(result.to_display_dict())
                except Exception as exc:
                    st.error(f"Retrieval failed: {exc}")
            else:
                st.warning("Enter a query first.")

    st.divider()

    # ── Persona section ────────────────────────────────────────────────────
    st.markdown("##### Persona")
    persona_entries = Memory.get_memories(identity_id, memory_type="persona", limit=1)
    persona = persona_entries[0] if persona_entries else None

    # Parse persona content string into individual fields for display.
    # Content is stored as "Name: ...\nTone: ...\nCommunication style: ...\nCore traits: ..."
    def _parse_persona(p: dict | None) -> tuple[str, str, str, str]:
        if not p:
            return "", "", "", ""
        raw = p.get("content", "")
        fields: dict[str, str] = {}
        for line in raw.splitlines():
            if ":" in line:
                key, _, val = line.partition(":")
                fields[key.strip().lower()] = val.strip()
        return (
            fields.get("name", ""),
            fields.get("tone", ""),
            fields.get("communication style", ""),
            fields.get("core traits", ""),
        )

    _p_name_val, _p_tone_val, _p_style_val, _p_traits_val = _parse_persona(persona)

    with st.expander("Edit Persona", expanded=not bool(persona)):
        if not persona:
            st.caption("_No persona defined. Fill in the fields below to create one._")

        p_name  = st.text_input("Name",               value=_p_name_val,   key="_p_name")
        p_tone  = st.text_input("Tone",               value=_p_tone_val,   key="_p_tone")
        p_style = st.text_input("Communication style", value=_p_style_val,  key="_p_style")
        p_traits= st.text_input("Core traits",        value=_p_traits_val, key="_p_traits")

        if st.button("Save Persona", key="_p_save"):
            persona_content = (
                f"Name: {p_name}\nTone: {p_tone}\n"
                f"Communication style: {p_style}\nCore traits: {p_traits}"
            )
            try:
                if persona:
                    Memory.update_memory(persona["_id"], content=persona_content)
                    # Also update the extra fields if stored flat
                else:
                    Memory.add_memory(
                        identity_id=identity_id,
                        memory_type="persona",
                        content=persona_content,
                        source="user",
                    )
                st.success("Persona saved.")
                st.rerun()
            except Exception as exc:
                st.error(f"Failed to save persona: {exc}")

    st.divider()

    # ── Non-persona memory sections ────────────────────────────────────────
    MEM_TYPES = ["semantic", "episodic", "procedural", "working", "archive"]
    type_tabs  = st.tabs([t.capitalize() for t in MEM_TYPES])

    for tab, t_name in zip(type_tabs, MEM_TYPES):
        with tab:
            entries = Memory.get_memories(identity_id, memory_type=t_name, limit=200)

            # "Add entry" form (Req 18.7)
            with st.expander("＋ Add entry", expanded=False):
                new_content  = st.text_area(
                    "Content (max 10,000 chars)", height=80,
                    key=f"_add_{t_name}_content", max_chars=10000,
                )
                new_tags_raw = st.text_input("Tags (comma-separated)", key=f"_add_{t_name}_tags")
                new_pinned   = st.checkbox("Pin", key=f"_add_{t_name}_pinned")
                if st.button("Add", key=f"_add_{t_name}_btn", type="primary"):
                    if new_content.strip():
                        try:
                            tags = [t.strip() for t in new_tags_raw.split(",") if t.strip()]
                            Memory.add_memory(
                                identity_id=identity_id,
                                memory_type=t_name,  # type: ignore[arg-type]
                                content=new_content.strip(),
                                tags=tags,
                                pinned=new_pinned,
                                source="manual",
                            )
                            st.success("Entry added.")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Failed to add entry: {exc}")
                    else:
                        st.error("Content cannot be empty.")

            # "Clear type" button (Req 18.6) — not for persona
            if entries and t_name != "persona":
                clear_key = f"_clear_type_{t_name}"
                confirm_key = f"_confirm_clear_{t_name}"
                if st.button(f"🗑 Clear all {t_name}", key=clear_key, type="secondary"):
                    st.session_state[confirm_key] = True
                if st.session_state.get(confirm_key):
                    st.warning(f"Delete all {t_name} entries for `{identity_id}`?")
                    c1, c2, _ = st.columns([1, 1, 4])
                    with c1:
                        if st.button("Confirm", key=f"_clearok_{t_name}", type="primary"):
                            try:
                                db = get_db()
                                db["memory"].delete_many(
                                    {"identity_id": identity_id, "memory_type": t_name}
                                )
                                st.session_state[confirm_key] = False
                                st.success(f"Cleared all {t_name} entries.")
                                st.rerun()
                            except Exception as exc:
                                st.error(f"Failed to clear: {exc}")
                    with c2:
                        if st.button("Cancel", key=f"_clearcancel_{t_name}"):
                            st.session_state[confirm_key] = False
                            st.rerun()

            # Entry list
            if not entries:
                st.caption(f"_No {t_name} memory entries._")
            else:
                st.caption(f"{len(entries)} entr{'y' if len(entries)==1 else 'ies'}")
                for entry in entries:
                    eid     = entry.get("_id", "")
                    created = entry.get("created_at", "")
                    pinned  = entry.get("pinned", False)
                    tags    = entry.get("tags", [])
                    content = entry.get("content", "")
                    score   = entry.get("score")
                    reason  = entry.get("match_reason")
                    pin_icon = "📌 " if pinned else ""
                    tag_str  = f" `{'` `'.join(tags)}`" if tags else ""

                    edit_mode_key = f"_edit_mode_{eid}"

                    with st.expander(
                        f"{pin_icon}{created} — {content[:60]}{'…' if len(content)>60 else ''}",
                        expanded=False,
                    ):
                        if score is not None:
                            st.caption(f"score={score:.4f}  reason={reason}")

                        if st.session_state.get(edit_mode_key):
                            # Edit mode
                            new_c = st.text_area(
                                "Content", value=content, height=100,
                                key=f"_edit_content_{eid}",
                            )
                            new_t = st.text_input(
                                "Tags (comma-separated)",
                                value=", ".join(tags),
                                key=f"_edit_tags_{eid}",
                            )
                            col_save, col_cancel = st.columns(2)
                            with col_save:
                                if st.button("Save", key=f"_esave_{eid}", type="primary",
                                             use_container_width=True):
                                    try:
                                        new_tags = [t.strip() for t in new_t.split(",") if t.strip()]
                                        Memory.update_memory(eid, content=new_c, tags=new_tags)
                                        st.session_state[edit_mode_key] = False
                                        st.rerun()
                                    except Exception as exc:
                                        st.error(f"Save failed: {exc}")
                            with col_cancel:
                                if st.button("Cancel", key=f"_ecancel_{eid}",
                                             use_container_width=True):
                                    st.session_state[edit_mode_key] = False
                                    st.rerun()
                        else:
                            # View mode
                            st.text(content)
                            if tags:
                                st.caption(f"Tags:{tag_str}")

                            col_edit, col_pin, col_del = st.columns([1, 1, 1])
                            with col_edit:
                                if st.button("Edit", key=f"_edit_{eid}", use_container_width=True):
                                    st.session_state[edit_mode_key] = True
                                    st.rerun()
                            with col_pin:
                                pin_label = "Unpin" if pinned else "Pin"
                                if st.button(pin_label, key=f"_pin_{eid}", use_container_width=True):
                                    try:
                                        Memory.update_memory(eid, pinned=not pinned)
                                        st.rerun()
                                    except Exception as exc:
                                        st.error(f"Failed: {exc}")
                            with col_del:
                                del_confirm_key = f"_del_confirm_{eid}"
                                if not st.session_state.get(del_confirm_key):
                                    if st.button("Delete", key=f"_del_{eid}",
                                                  type="secondary", use_container_width=True):
                                        st.session_state[del_confirm_key] = True
                                        st.rerun()
                                else:
                                    st.warning("Delete this entry?")
                                    dc1, dc2 = st.columns(2)
                                    with dc1:
                                        if st.button("Confirm", key=f"_delok_{eid}",
                                                      type="primary", use_container_width=True):
                                            try:
                                                Memory.delete_memory(eid)
                                                st.session_state.pop(del_confirm_key, None)
                                                st.rerun()
                                            except Exception as exc:
                                                st.error(f"Delete failed: {exc}")
                                    with dc2:
                                        if st.button("Cancel", key=f"_delcancel_{eid}",
                                                      use_container_width=True):
                                            st.session_state[del_confirm_key] = False
                                            st.rerun()


# ---------------------------------------------------------------------------
# Tab: Audit Trail
# ---------------------------------------------------------------------------

def _render_audit_tab() -> None:
    identity_id = st.session_state.identity_id
    st.caption(
        f"Complete inference audit trail for identity `{identity_id}`. "
        "Every generation event is logged — model, prompt state, context, output, tokens, latency."
    )

    col_scope, col_limit, col_del = st.columns([1, 1, 1])
    with col_scope:
        scope = st.selectbox("Scope", ["This identity", "All identities"],
                              key="_audit_scope", label_visibility="collapsed")
    with col_limit:
        limit = st.number_input("Limit", min_value=1, max_value=500, value=50,
                                 key="_audit_limit", label_visibility="collapsed")
    with col_del:
        if st.button("🗑 Delete audit records", key="_audit_delete_btn",
                     type="secondary", use_container_width=True):
            st.session_state._confirm_audit_delete = True

    # Confirm audit deletion
    if st.session_state.get("_confirm_audit_delete"):
        target = "all identities" if st.session_state.get("_audit_scope") == "All identities" else f"identity '{identity_id}'"
        st.markdown(f"""
        <div class="confirm-bar">
            ⚠️ Permanently delete all audit records for {target}? This cannot be undone.
        </div>
        """, unsafe_allow_html=True)
        ca1, ca2, _ = st.columns([1, 1, 4])
        with ca1:
            if st.button("Confirm", type="primary", use_container_width=True, key="_audit_delete_confirm"):
                db = get_db()
                if st.session_state.get("_audit_scope") == "All identities":
                    db["audit_log"].delete_many({})
                else:
                    db["audit_log"].delete_many({"identity_id": identity_id})
                st.session_state._confirm_audit_delete = False
                st.rerun()
        with ca2:
            if st.button("Cancel", use_container_width=True, key="_audit_delete_cancel"):
                st.session_state._confirm_audit_delete = False
                st.rerun()
        return

    try:
        if scope == "All identities":
            records = Audit.read_all_audit_records(limit=int(limit))
        else:
            records = Audit.read_audit_records(identity_id, limit=int(limit))
    except Exception as exc:
        st.error(f"Failed to read audit log: {exc}")
        return

    if not records:
        st.info("No audit records yet. Send a message to generate one.")
        return

    # Export audit log button (Req 10.3)
    audit_export = make_export_envelope(identity_id=identity_id, data=records)
    st.download_button(
        label="⬇ Export audit log",
        data=to_json_str(audit_export),
        file_name=f"falcon_audit_{identity_id}_{_utc_iso().replace(':','-')}.json",
        mime="application/json",
        key="_audit_export_btn",
    )

    st.caption(f"{len(records)} record{'s' if len(records)!=1 else ''} (newest first)")
    st.divider()

    for rec in records:
        ts           = rec.get("timestamp", "")
        model        = rec.get("model", "")
        identity     = rec.get("identity_id", "")
        prompt_state = rec.get("prompt_state", "")
        latency      = rec.get("latency_ms", 0)
        usage        = rec.get("usage") or {}
        ctx_size     = rec.get("context_size", 0)
        output_prev  = (rec.get("raw_model_output") or "")[:80]

        ps_color = "#22c55e" if prompt_state == "present" else "#64748b"
        label = (
            f"`{ts}` · `{identity}` · `{model}` · "
            f"<span style='color:{ps_color}'>prompt:{prompt_state}</span> · "
            f"`{latency}ms` · `{usage.get('total_tokens','?')} tok`"
        )
        with st.expander(f"{ts} — {model} — {prompt_state}", expanded=False):
            st.markdown(label, unsafe_allow_html=True)
            st.divider()

            cols = st.columns(4)
            cols[0].metric("Latency",     f"{latency}ms")
            cols[1].metric("Context",     f"{ctx_size} msgs")
            cols[2].metric("Prompt tok",  usage.get("prompt_tokens", "?"))
            cols[3].metric("Output tok",  usage.get("completion_tokens", "?"))

            # Generation settings
            gen_s = rec.get("generation_settings") or {}
            st.caption("**Generation Settings**")
            st.json(gen_s, expanded=False)

            # System prompt
            st.caption(f"**System Prompt** (state: `{prompt_state}`)")
            sp = rec.get("system_prompt")
            if sp:
                st.code(sp, language=None)
            else:
                st.caption("_None — empty instruction context_")

            # Retrieved memories
            mems = rec.get("retrieved_memories") or []
            st.caption(f"**Retrieved Memory** ({len(mems)} entries)")
            if mems:
                st.json(mems, expanded=False)
            else:
                st.caption("_None_")

            # Assembled payload
            st.caption("**Assembled Payload** (exact input to model)")
            st.json(rec.get("assembled_payload") or [], expanded=False)

            # Raw model output
            st.caption("**Raw Model Output**")
            raw_out = rec.get("raw_model_output") or ""
            st.code(raw_out[:2000] + ("…" if len(raw_out) > 2000 else ""), language=None)


# ---------------------------------------------------------------------------
# Tab: Logs
# ---------------------------------------------------------------------------

def _render_logs_tab() -> None:
    identity_id = st.session_state.identity_id

    st.caption(f"**Messages:** `mongodb://falcon/messages` · identity `{identity_id}`")
    st.caption(f"**Traces:** `mongodb://falcon/traces` · identity `{identity_id}`")

    entries = _read_log_entries(identity_id)
    traces  = _read_traces(identity_id)
    trace_by_ts: dict[str, list[dict]] = {
        t["user_timestamp"]: t["steps"]
        for t in traces
        if "user_timestamp" in t
    }

    st.caption(
        f"**Entries:** {len(entries) if entries is not None else 'parse error'} "
        f"| **Trace snapshots:** {len(traces)}"
    )

    # Export conversation button (Req 10.1)
    conv_export = make_export_envelope(identity_id=identity_id, data=entries or [])
    st.download_button(
        label="⬇ Export conversation",
        data=to_json_str(conv_export),
        file_name=f"falcon_conversation_{identity_id}_{_utc_iso().replace(':','-')}.json",
        mime="application/json",
        key="_conv_export_btn",
    )

    st.divider()

    raw_tab, structured_tab = st.tabs(["Raw JSON", "Structured"])

    with raw_tab:
        current_raw = _read_log_raw(identity_id)
        edited = st.text_area("JSON", value=current_raw, height=550,
                               label_visibility="collapsed")
        s_col, _ = st.columns([1, 3])
        with s_col:
            if st.button("Save", key="_logs_raw_save", use_container_width=True):
                _save_raw(identity_id, edited)

    with structured_tab:
        if entries is None:
            st.error("File is not valid JSON. Fix it in the Raw JSON tab.")
            return
        if len(entries) == 0:
            st.info("Log is empty.")
            return

        pairs = _build_pairs(entries)
        total = len(pairs)
        st.caption(f"**{total} message{'s' if total != 1 else ''}** ({len(entries)} entries)")

        if st.session_state.get("_delete_confirmed"):
            cp      = st.session_state._confirm_pair
            cp_data = next(((idxs, ip) for pn, idxs, ip in pairs if pn == cp), None)
            if cp_data:
                cp_idxs, _ = cp_data
                remaining  = [e for i, e in enumerate(entries) if i not in cp_idxs]
                _save_entries(identity_id, remaining)
                user_ts = entries[cp_idxs[0]].get("timestamp", "")
                _delete_trace_for_timestamp(identity_id, user_ts)
                st.session_state.history = Identity.load_history(identity_id)
                for k in list(st.session_state.keys()):
                    if k.startswith("_lc_") or k.startswith("_lr_"):
                        del st.session_state[k]
            st.session_state._confirm_pair    = None
            st.session_state._delete_confirmed = False
            st.rerun()

        if st.session_state._confirm_pair is not None:
            cp      = st.session_state._confirm_pair
            cp_data = next(((idxs, ip) for pn, idxs, ip in pairs if pn == cp), None)
            if cp_data:
                cp_idxs, cp_is_pair = cp_data
                _delete_confirm_dialog(cp, cp_idxs, entries, cp_is_pair)

        for p_num, raw_idxs, is_pair in pairs:
            ts_label   = entries[raw_idxs[0]].get("timestamp", "")
            user_ts    = entries[raw_idxs[0]].get("timestamp", "")
            turn_trace = trace_by_ts.get(user_ts)

            user_q = entries[raw_idxs[0]].get("content", "")
            user_q_preview = " ".join(user_q.split())
            if len(user_q_preview) > 60:
                user_q_preview = user_q_preview[:60].rstrip() + "…"

            with st.expander(f"#{p_num}  —  {ts_label}  —  {user_q_preview}", expanded=False):
                if is_pair:
                    u_idx, a_idx = raw_idxs
                    u_e, a_e     = entries[u_idx], entries[a_idx]
                    u_key = f"_lc_{u_idx}_{u_e.get('timestamp','').replace(':','').replace('.','')}"
                    a_key = f"_lc_{a_idx}_{a_e.get('timestamp','').replace(':','').replace('.','')}"

                    st.markdown(f"**Q** `{u_e.get('timestamp','')}`")
                    new_u = st.text_area("Q", value=u_e.get("content",""),
                                          height=90, key=u_key,
                                          label_visibility="collapsed")
                    st.divider()
                    st.markdown(f"**A** `{a_e.get('timestamp','')}`")
                    new_a = st.text_area("A", value=a_e.get("content",""),
                                          height=90, key=a_key,
                                          label_visibility="collapsed")
                else:
                    r_idx = raw_idxs[0]
                    e     = entries[r_idx]
                    role  = e.get("role", "user")
                    ts    = e.get("timestamp", "")
                    r_key = f"_lc_{r_idx}_{ts.replace(':','').replace('.','')}"
                    nr    = st.selectbox("Role", ["user", "assistant"],
                                         index=0 if role == "user" else 1,
                                         key=f"_lr_{r_key}")
                    nc    = st.text_area("Content", value=e.get("content",""),
                                         height=100, key=r_key,
                                         label_visibility="collapsed")

                st.divider()
                save_col, del_col, trace_col = st.columns([1, 1, 1])
                with save_col:
                    if st.button("Save", key=f"_save_{p_num}", use_container_width=True):
                        new_entries = list(entries)
                        if is_pair:
                            new_entries[u_idx] = {"timestamp": u_e.get("timestamp",""),
                                                   "role": "user", "content": new_u}
                            new_entries[a_idx] = {"timestamp": a_e.get("timestamp",""),
                                                   "role": "assistant", "content": new_a}
                        else:
                            new_entries[r_idx] = {"timestamp": ts, "role": nr, "content": nc}
                        _save_entries(identity_id, new_entries)
                        if identity_id == st.session_state.identity_id:
                            st.session_state.history = Identity.load_history(identity_id)
                        for k in list(st.session_state.keys()):
                            if k.startswith("_lc_") or k.startswith("_lr_"):
                                del st.session_state[k]
                        st.rerun()
                with del_col:
                    if st.button("Delete", key=f"_del_{p_num}",
                                  type="secondary", use_container_width=True):
                        st.session_state._confirm_pair = p_num
                        st.rerun()
                with trace_col:
                    if turn_trace:
                        if st.button("Trace", key=f"_trace_{p_num}", use_container_width=True):
                            st.session_state._view_trace_ts = user_ts
                            st.rerun()
                    else:
                        st.button("Trace", key=f"_trace_{p_num}",
                                   use_container_width=True, disabled=True)

        if st.session_state.get("_view_trace_ts") is not None:
            vts      = st.session_state._view_trace_ts
            vt_steps = trace_by_ts.get(vts)
            if vt_steps:
                _show_trace_dialog(vts, vt_steps)
            st.session_state._view_trace_ts = None


def _save_raw(identity_id: str, raw_text: str) -> None:
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        st.error(f"Invalid JSON — not saved: {exc}")
        return
    if not isinstance(parsed, list):
        st.error("Top-level value must be a JSON array — not saved.")
        return
    for idx, e in enumerate(parsed):
        if not isinstance(e, dict):
            st.error(f"Entry {idx} is not an object — not saved."); return
        if e.get("role") not in ("user", "assistant"):
            st.error(f"Entry {idx} invalid role {e.get('role')!r} — not saved."); return
        if "content" not in e:
            st.error(f"Entry {idx} missing 'content' — not saved."); return
        if "timestamp" not in e:
            st.error(f"Entry {idx} missing 'timestamp' — not saved."); return
    _save_entries(identity_id, parsed)
    if identity_id == st.session_state.identity_id:
        st.session_state.history = Identity.load_history(identity_id)
    st.success(f"Saved {len(parsed)} entries to MongoDB.")
    st.rerun()


# ---------------------------------------------------------------------------
# Dialogs
# ---------------------------------------------------------------------------

@st.dialog("Trace", width="large")
def _show_trace_dialog(user_ts: str, steps: list[dict]) -> None:
    st.caption(f"User message timestamp: `{user_ts}`")
    st.divider()
    _render_trace_steps(steps)


@st.dialog("Context", width="large")
def _show_payload_dialog(payload: list[dict]) -> None:
    st.caption(f"{len(payload)} message{'s' if len(payload) != 1 else ''} sent to model")
    st.divider()
    st.json(payload, expanded=True)


@st.dialog("Delete message", width="small")
def _delete_confirm_dialog(p_num: int, raw_idxs: list[int],
                            entries: list[dict], is_pair: bool) -> None:
    q_prev = entries[raw_idxs[0]].get("content", "")
    st.warning(
        f"Delete message **#{p_num}**?"
        + (" This removes both Q and A." if is_pair else "")
    )
    st.caption(f"Q: {q_prev[:120]}{'…' if len(q_prev) > 120 else ''}")
    if is_pair and len(raw_idxs) > 1:
        a_prev = entries[raw_idxs[1]].get("content", "")
        st.caption(f"A: {a_prev[:120]}{'…' if len(a_prev) > 120 else ''}")
    st.divider()
    yes_c, no_c = st.columns(2)
    with yes_c:
        if st.button("Delete", type="primary", use_container_width=True, key="_dlg_del_yes"):
            st.session_state._delete_confirmed = True
            st.rerun()
    with no_c:
        if st.button("Cancel", use_container_width=True, key="_dlg_del_no"):
            st.session_state._confirm_pair    = None
            st.session_state._delete_confirmed = False
            st.rerun()


@st.dialog("Delete identity", width="small")
def _confirm_delete_identity_dialog(identity_id: str) -> None:
    st.warning(
        f"Permanently delete identity **'{identity_id}'**?\n\n"
        "This removes all messages, traces, token data, memory, and audit records. "
        "This cannot be undone."
    )
    yes_c, no_c = st.columns(2)
    with yes_c:
        if st.button("Delete", type="primary", use_container_width=True, key="_del_identity_yes"):
            db = get_db()
            try:
                db["messages"].delete_many({"identity_id": identity_id})
                db["traces"].delete_many({"identity_id": identity_id})
                db["tokens"].delete_one({"identity_id": identity_id})
                db["memory"].delete_many({"identity_id": identity_id})
                db["audit_log"].delete_many({"identity_id": identity_id})
                db["identities"].delete_one({"identity_id": identity_id})
            except Exception:
                pass
            st.session_state.identity_id     = "default"
            st.session_state.history         = Identity.load_history("default")
            st.session_state.last_payload    = None
            st.session_state.last_response   = None
            st.session_state.last_context_view     = None
            st.session_state.last_retrieved_memory = None
            st.session_state.trace_log       = []
            st.session_state._confirm_pair   = None
            st.session_state.session_tokens  = {"prompt": 0, "completion": 0, "total": 0}
            st.session_state._confirm_delete_identity = False
            _restore_token_totals("default")
            st.rerun()
    with no_c:
        if st.button("Cancel", use_container_width=True, key="_del_identity_no"):
            st.session_state._confirm_delete_identity = False
            st.rerun()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(page_title="Falcon", layout="wide", page_icon="🦅")
    _init_session_state()

    # ── Global CSS ────────────────────────────────────────────────────────────
    st.markdown("""
    <style>
    [data-testid="stAppViewContainer"] { background: #0f1117; }
    [data-testid="stSidebar"] {
        background: #161b27;
        border-right: 1px solid #1e2535;
    }
    #MainMenu, footer, header { visibility: hidden; }
    [data-testid="stDecoration"] { display: none; }

    .falcon-header {
        display: flex; align-items: center; gap: 10px;
        padding: 18px 4px 10px 4px;
        border-bottom: 1px solid #1e2535; margin-bottom: 4px;
    }
    .falcon-header h1 {
        font-size: 1.35rem; font-weight: 700; color: #e2e8f0;
        margin: 0; letter-spacing: 0.02em;
    }

    [data-testid="stChatMessage"] {
        background: transparent !important; border: none !important; padding: 4px 0 !important;
    }
    [data-testid="stChatMessage"] [data-testid="stMarkdownContainer"] p {
        color: #cbd5e1; line-height: 1.65; font-size: 0.95rem;
    }
    [data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) {
        background: #1a2235 !important; border-radius: 12px !important;
        padding: 12px 16px !important; margin: 6px 0 !important;
    }
    [data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarAssistant"]) {
        background: transparent !important; border-radius: 12px !important;
        padding: 8px 4px !important; margin: 4px 0 !important;
    }

    [data-testid="stBottom"] {
        background: #0f1117; border-top: 1px solid #1e2535; padding: 10px 0 6px 0;
    }
    [data-testid="stChatInput"] textarea {
        background: #161b27 !important; border: 1px solid #2d3748 !important;
        border-radius: 10px !important; color: #e2e8f0 !important;
        font-size: 0.93rem !important; padding: 12px 16px !important;
    }
    [data-testid="stChatInput"] textarea:focus {
        border-color: #3b82f6 !important;
        box-shadow: 0 0 0 3px rgba(59,130,246,0.12) !important;
    }

    .clear-btn button {
        background: transparent !important; border: 1px solid #2d3748 !important;
        color: #64748b !important; border-radius: 8px !important;
        font-size: 0.78rem !important; padding: 4px 14px !important;
    }
    .clear-btn button:hover {
        border-color: #ef4444 !important; color: #ef4444 !important;
        background: rgba(239,68,68,0.06) !important;
    }

    .confirm-bar {
        background: #1e1a0f; border: 1px solid #78350f;
        border-radius: 10px; padding: 12px 16px; margin: 8px 0;
    }

    [data-testid="stTabs"] [data-baseweb="tab-list"] {
        background: transparent; border-bottom: 1px solid #1e2535; gap: 0;
    }
    [data-testid="stTabs"] [data-baseweb="tab"] {
        background: transparent !important; color: #64748b !important;
        font-size: 0.85rem !important; font-weight: 500 !important;
        padding: 8px 20px !important; border-bottom: 2px solid transparent !important;
    }
    [data-testid="stTabs"] [aria-selected="true"] {
        color: #e2e8f0 !important; border-bottom: 2px solid #3b82f6 !important;
    }

    .sidebar-section-label {
        font-size: 0.68rem; font-weight: 600; letter-spacing: 0.08em;
        text-transform: uppercase; color: #475569; margin: 14px 0 6px 0;
    }
    .token-row {
        display: flex; justify-content: space-between; align-items: center;
        padding: 3px 0; font-size: 0.8rem; color: #64748b;
    }
    .token-row span.val { font-family: monospace; color: #94a3b8; }

    [data-testid="stSelectbox"] > div > div,
    [data-testid="stTextInput"] input {
        background: #1e2535 !important; border: 1px solid #2d3748 !important;
        border-radius: 8px !important; color: #e2e8f0 !important; font-size: 0.85rem !important;
    }

    [data-testid="stExpander"] {
        background: #161b27 !important; border: 1px solid #1e2535 !important;
        border-radius: 10px !important; margin-bottom: 6px !important;
    }
    [data-testid="stExpander"] summary {
        color: #94a3b8 !important; font-size: 0.83rem !important; font-family: monospace !important;
    }

    .empty-chat {
        display: flex; flex-direction: column; align-items: center;
        justify-content: center; padding: 60px 20px;
        color: #334155; text-align: center;
    }
    .empty-chat .icon { font-size: 2.8rem; margin-bottom: 12px; }
    .empty-chat .title { font-size: 1.1rem; font-weight: 600; color: #475569; margin-bottom: 6px; }
    .empty-chat .sub { font-size: 0.83rem; color: #334155; }

    /* Generation controls slider label */
    .gen-control-label {
        font-size: 0.72rem; color: #64748b; margin-bottom: 2px;
        font-family: monospace; letter-spacing: 0.04em;
    }
    </style>
    """, unsafe_allow_html=True)

    if not st.session_state._loaded_initial_history:
        _load_identity_history(st.session_state.identity_id)
        _restore_token_totals(st.session_state.identity_id)
        st.session_state._loaded_initial_history = True

    # ── Sidebar ───────────────────────────────────────────────────────────────
    with st.sidebar:
        st.markdown('<div class="falcon-header"><h1>🦅 Falcon</h1></div>', unsafe_allow_html=True)

        # ── Identity ──────────────────────────────────────────────────────────
        st.markdown('<div class="sidebar-section-label">Identity</div>', unsafe_allow_html=True)

        existing_ids = [i for i in Identity.list_identities() if "." not in i]
        all_ids      = sorted(set(existing_ids) | {st.session_state.identity_id, "default"})
        current_idx  = all_ids.index(st.session_state.identity_id) if st.session_state.identity_id in all_ids else 0

        # Build identity options with message counts (Req 12.4, 12.5)
        db = get_db()
        def _msg_count(iid: str) -> int:
            try:
                return db["messages"].count_documents({"identity_id": iid})
            except Exception:
                return 0
        id_options_display = [f"{iid} ({_msg_count(iid)} msgs)" for iid in all_ids]
        id_option_map      = dict(zip(id_options_display, all_ids))

        current_display = next(
            (d for d, v in id_option_map.items() if v == st.session_state.identity_id),
            id_options_display[current_idx] if id_options_display else "default"
        )
        current_display_idx = id_options_display.index(current_display) if current_display in id_options_display else 0

        chosen_display = st.selectbox(
            "Identity", options=id_options_display, index=current_display_idx,
            label_visibility="collapsed",
        )
        chosen = id_option_map.get(chosen_display, st.session_state.identity_id)
        if chosen != st.session_state.identity_id:
            _invalidate_cache(chosen)
            st.session_state.identity_id          = chosen
            st.session_state.history              = Identity.load_history(chosen)
            st.session_state.last_payload         = None
            st.session_state.last_response        = None
            st.session_state.last_context_view    = None
            st.session_state.last_context_snapshot = None
            st.session_state.last_retrieved_memory = None
            st.session_state.trace_log            = []
            st.session_state._confirm_pair        = None
            _restore_token_totals(chosen)
            st.rerun()

        with st.form("_new_identity_form", clear_on_submit=False):
            new_name  = st.text_input(
                "New identity", placeholder="New identity name…",
                label_visibility="collapsed",
            )
            submitted = st.form_submit_button("＋ Create", use_container_width=True)

        if submitted:
            new_id = new_name.strip()
            if not new_id:
                st.error("Enter an identity name.")
            elif "." in new_id:
                st.error("Name cannot contain a dot.")
            elif any(c in new_id for c in ("/", "\\", "\x00")) or ".." in new_id:
                st.error("Invalid characters in name.")
            elif new_id in all_ids:
                st.session_state.identity_id   = new_id
                st.session_state.history       = Identity.load_history(new_id)
                st.session_state.last_payload  = None
                st.session_state.last_response = None
                st.session_state.last_context_view = None
                st.session_state.last_retrieved_memory = None
                st.session_state.trace_log     = []
                st.session_state._confirm_pair = None
                _restore_token_totals(new_id)
                st.rerun()
            else:
                # Persist identity immediately and seed default persona
                Identity.create_identity(new_id)
                Memory.add_memory(
                    identity_id=new_id,
                    memory_type="persona",
                    content=Config.default_persona_content,
                    source="user",
                )
                st.session_state.identity_id    = new_id
                st.session_state.history        = []
                st.session_state.last_payload   = None
                st.session_state.last_response  = None
                st.session_state.last_context_view = None
                st.session_state.last_retrieved_memory = None
                st.session_state.trace_log      = []
                st.session_state._confirm_pair  = None
                st.session_state.session_tokens = {"prompt": 0, "completion": 0, "total": 0}
                st.rerun()

        can_delete = st.session_state.identity_id != "default"
        if st.button(
            f"🗑 Delete '{st.session_state.identity_id}'",
            key="_delete_identity_btn", type="secondary",
            use_container_width=True, disabled=not can_delete,
        ):
            st.session_state._confirm_delete_identity = True

        if st.session_state.get("_confirm_delete_identity"):
            _confirm_delete_identity_dialog(st.session_state.identity_id)

        # ── Model ─────────────────────────────────────────────────────────────
        st.markdown('<div class="sidebar-section-label">Model</div>', unsafe_allow_html=True)
        if Config.available_models:
            sel = st.selectbox(
                "Model", options=Config.available_models,
                index=(Config.available_models.index(st.session_state.selected_model)
                       if st.session_state.selected_model in Config.available_models else 0),
                label_visibility="collapsed",
            )
            st.session_state.selected_model = sel
        else:
            st.caption(st.session_state.selected_model)

        # ── System Prompt ─────────────────────────────────────────────────────
        st.markdown('<div class="sidebar-section-label">System Prompt</div>', unsafe_allow_html=True)

        use_sp = st.checkbox(
            "System prompt",
            value=st.session_state.use_system_prompt,
            key="_sp_checkbox",
        )

        if use_sp != st.session_state.use_system_prompt:
            st.session_state.use_system_prompt = use_sp

        if st.session_state.use_system_prompt:
            st.caption("ON — prompt active")
            edited = st.text_area(
                "system_prompt_edit",
                value=st.session_state.system_prompt_text,
                height=140,
                key="_sp_textarea",
                label_visibility="collapsed",
                placeholder="Enter system prompt…",
            )
            st.session_state.system_prompt_text = edited
        else:
            st.caption("OFF — no platform prompt injected")

        # ── Truncation Strategy ───────────────────────────────────────────────
        st.markdown('<div class="sidebar-section-label">History Truncation</div>', unsafe_allow_html=True)
        new_max_turns = st.number_input(
            "Max turns", min_value=1, max_value=100,
            value=int(st.session_state.get("history_max_turns", Config.history_max_turns)),
            step=1, key="_trunc_turns_input", label_visibility="collapsed",
        )
        st.session_state["history_max_turns"] = new_max_turns

        # ── Generation Controls ───────────────────────────────────────────────
        st.markdown('<div class="sidebar-section-label">Generation Controls</div>', unsafe_allow_html=True)

        st.markdown('<div class="gen-control-label">temperature</div>', unsafe_allow_html=True)
        st.session_state.gen_temperature = st.slider(
            "temperature", min_value=0.0, max_value=2.0,
            value=float(st.session_state.gen_temperature), step=0.05,
            label_visibility="collapsed", key="_slider_temp",
        )

        st.markdown('<div class="gen-control-label">top_p</div>', unsafe_allow_html=True)
        st.session_state.gen_top_p = st.slider(
            "top_p", min_value=0.0, max_value=1.0,
            value=float(st.session_state.gen_top_p), step=0.05,
            label_visibility="collapsed", key="_slider_top_p",
        )

        st.markdown('<div class="gen-control-label">repetition_penalty</div>', unsafe_allow_html=True)
        st.session_state.gen_repetition_penalty = st.slider(
            "repetition_penalty", min_value=1.0, max_value=2.0,
            value=float(st.session_state.gen_repetition_penalty), step=0.05,
            label_visibility="collapsed", key="_slider_rep",
        )

        st.markdown('<div class="gen-control-label">stop_tokens</div>', unsafe_allow_html=True)
        stop_raw = st.text_input(
            "stop_tokens", placeholder="comma-separated, e.g. <|end|>, ###",
            value=", ".join(st.session_state.gen_stop_tokens or []),
            label_visibility="collapsed", key="_input_stop",
        )
        st.session_state.gen_stop_tokens = [s.strip() for s in stop_raw.split(",") if s.strip()]

        # Current gen settings display
        gen_now = _get_gen_settings()
        st.caption(
            f"`T={gen_now['temperature']}` `P={gen_now['top_p']}` "
            f"`R={gen_now['repetition_penalty']}`"
        )

        # ── Session Stats ─────────────────────────────────────────────────────
        st.markdown('<div class="sidebar-section-label">Session</div>', unsafe_allow_html=True)
        tok       = st.session_state.session_tokens
        msg_count = _pair_count(st.session_state.history)
        st.markdown(f"""
        <div class="token-row"><span>Messages</span><span class="val">{msg_count}</span></div>
        <div class="token-row"><span>Prompt tokens</span><span class="val">{tok.get('prompt',0):,}</span></div>
        <div class="token-row"><span>Completion tokens</span><span class="val">{tok.get('completion',0):,}</span></div>
        <div class="token-row" style="border-top:1px solid #1e2535;margin-top:4px;padding-top:6px">
            <span>Total</span><span class="val" style="color:#60a5fa">{tok.get('total',0):,}</span>
        </div>
        """, unsafe_allow_html=True)

        st.markdown('<div style="margin-top:16px"></div>', unsafe_allow_html=True)
        st.caption(f"`{_log_path(st.session_state.identity_id)}`")

    # ── Chat input — page-level for bottom-sticky behaviour ──────────────────
    user_input = st.chat_input("Message…")

    # ── Background extraction auto-refresh ────────────────────────────────────
    # _extraction_done[identity_id] is set by the background thread when the
    # LLM extraction finishes. We compare it to what we saw last render.
    # If it changed, call st.rerun() to refresh the page (including Memory tab).
    _cur_identity = st.session_state.identity_id
    _done_at = _extraction_done.get(_cur_identity, 0.0)
    _last_seen = st.session_state.get("_extraction_last_seen", 0.0)
    if _done_at > _last_seen:
        st.session_state["_extraction_last_seen"] = _done_at
        st.rerun()

    # ── Periodic rerun to pick up background extraction completions ───────────
    # Without this, the check above only fires when the user does something.
    # The fragment auto-reruns every 3s to catch the extraction completing.
    @st.fragment(run_every=3)
    def _bg_poller():
        cid   = st.session_state.identity_id
        done  = _extraction_done.get(cid, 0.0)
        seen  = st.session_state.get("_extraction_last_seen", 0.0)
        if done > seen:
            st.session_state["_extraction_last_seen"] = done
            st.rerun()  # scope="app" → full page rerun

    _bg_poller()

    # ── Main tabs ─────────────────────────────────────────────────────────────
    tab_chat, tab_context, tab_memory, tab_audit, tab_logs = st.tabs(
        ["Chat", "Context", "Memory", "Audit", "Logs"]
    )

    with tab_chat:
        _render_chat_tab(user_input)

    with tab_context:
        _render_context_tab()

    with tab_memory:
        _render_memory_tab()

    with tab_audit:
        _render_audit_tab()

    with tab_logs:
        _render_logs_tab()


if __name__ == "__main__":
    main()
