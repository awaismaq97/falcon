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
import falcon.judge    as Judge
import falcon.summarizer as Summarizer
import falcon.dual_run as DualRun
from falcon.db import get_db
from falcon.export_utils import make_export_envelope, to_json_str

# Testing tab — import lazily inside the render function to avoid breaking
# the app if the tests folder isn't on sys.path yet. We add it here.
import sys as _sys, os as _os
_tests_dir = _os.path.join(_os.path.dirname(__file__), "tests")
if _tests_dir not in _sys.path:
    _sys.path.insert(0, _tests_dir)


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
        # Persona
        "use_persona":              True,    # on by default — persona memory injected when available
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
        # History mode: "raw" | "summary" | "hybrid"
        "history_mode":                "raw",
        # Last context snapshot (annotated)
        "last_context_snapshot":    None,
        # Judge
        "use_judge":                False,
        "judge_model":              Config.available_models[0] if Config.available_models else Config.default_model,
        # Pre-send payload preview — holds assembled context before user confirms generation
        "_pending_send":            None,   # dict | None
        "_pending_confirmed":       False,
        "_pending_cancelled":       False,
        # Payload review toggle — when False, messages go directly to the model
        "payload_review_enabled":   False,
        # Three-stage send state machine used when review is ON:
        #   None        → idle
        #   "preview"   → dialog open, waiting for user action
        #   "execute"   → dialog closed, generation about to run this render
        "_send_stage":              None,
        # Dual-run logging
        "dual_run_enabled":         False,
        "dual_run_state_tag":       "Neutral",
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
# Pre-send payload preview builder (pure — no DB writes, no API calls)
# ---------------------------------------------------------------------------

def _build_send_preview(user_input: str) -> dict:
    """Assemble the payload for user_input without logging or calling the model.

    Returns a dict stored in st.session_state._pending_send so the dialog can
    display it before the user confirms. Nothing is written to DB here.
    """
    identity_id = st.session_state.identity_id
    model       = st.session_state.selected_model
    gen         = _get_gen_settings()

    if st.session_state.get("use_system_prompt", False):
        system_prompt = st.session_state.get("system_prompt_text", "").strip()
    else:
        system_prompt = ""

    history_max_turns = st.session_state.get("history_max_turns", Config.history_max_turns)
    history_mode = st.session_state.get("history_mode", "raw")

    # Fetch summary if needed for this history mode
    history_summary: str | None = None
    if history_mode in ("summary", "hybrid"):
        history_summary = Summarizer.get_summary(identity_id)

    # Memory retrieval (same logic as _handle_send)
    try:
        retrieval = Memory.retrieve_for_generation(
            identity_id=identity_id,
            query=user_input,
            top_k_per_type=Config.top_k_per_type,
            recency_weight=Config.recency_weight,
            relevance_weight=Config.relevance_weight,
        )
        retrieved_entries = retrieval.entries
    except Exception:
        retrieval = None
        retrieved_entries = []

    if not st.session_state.get("use_persona", True):
        retrieved_entries = [e for e in retrieved_entries if e.get("memory_type") != "persona"]

    messages = list(st.session_state.history) + [{"role": "user", "content": user_input}]
    messages_for_model = [{"role": m["role"], "content": m["content"]} for m in messages]

    try:
        annotated_payload, context_snapshot = Engine.build_annotated_payload(
            system_prompt=system_prompt,
            messages=messages_for_model,
            memory_block=retrieved_entries,
            truncation_strategy="last-n-turns",
            history_max_turns=history_max_turns,
            history_mode=history_mode,
            history_summary=history_summary,
        )
        if retrieval is not None:
            context_snapshot["retrieval_result"] = retrieval.to_display_dict()
        raw_payload = [{"role": e["role"], "content": e["content"]} for e in annotated_payload]
    except Exception:
        annotated_payload = []
        raw_payload = Engine.build_payload(system_prompt, messages_for_model)
        context_snapshot = Engine.build_context_view(
            system_prompt=system_prompt,
            messages=messages_for_model,
            retrieved_memory=retrieved_entries,
        )

    return {
        "user_input":         user_input,
        "identity_id":        identity_id,
        "model":              model,
        "gen":                gen,
        "system_prompt":      system_prompt,
        "retrieved_entries":  retrieved_entries,
        "raw_payload":        raw_payload,
        "annotated_payload":  annotated_payload,
        "context_snapshot":   context_snapshot,
        "history_max_turns":  history_max_turns,
        "history_mode":       history_mode,
        "history_summary":    history_summary,
    }


# ---------------------------------------------------------------------------
# Payload preview dialog
# ---------------------------------------------------------------------------

# Source → colour mapping (matches the Context tab)
_SOURCE_COLORS = {
    "system-prompt":   "#3b82f6",
    "persona":         "#a855f7",
    "memory":          "#f59e0b",
    "history":         "#64748b",
    "user-input":      "#22c55e",
    "history-summary": "#06b6d4",
}
_SOURCE_LABELS = {
    "system-prompt":   "System Prompt",
    "persona":         "Persona",
    "memory":          "Memory",
    "history":         "History",
    "user-input":      "User Input",
    "history-summary": "History Summary",
}


def _render_send_preview_inline(preview: dict) -> None:
    """Render the payload preview inline in the chat tab (no modal/dialog).

    Displays the assembled payload colour-coded by source with Confirm and
    Cancel buttons. Because this is plain inline Streamlit — not a dialog —
    button clicks cause a normal full-page rerun and session state changes
    take effect immediately on the next render.
    """
    annotated = preview.get("annotated_payload") or []
    raw       = preview.get("raw_payload") or []
    model     = preview.get("model", "")
    gen       = preview.get("gen", {})
    sp        = preview.get("system_prompt", "")
    sp_on     = bool(sp and sp.strip())
    n_mem     = sum(1 for e in annotated if e.get("source") == "memory")
    n_hist    = sum(1 for e in annotated if e.get("source") == "history")
    dropped   = preview.get("context_snapshot", {}).get("history_dropped_turns", 0)

    st.markdown(
        "<div style='border:1px solid #2d3748;border-radius:12px;"
        "background:#0d1117;padding:18px 20px;margin:12px 0'>",
        unsafe_allow_html=True,
    )

    st.markdown(
        "<span style='color:#f59e0b;font-size:0.72rem;font-weight:700;"
        "font-family:monospace;text-transform:uppercase;letter-spacing:0.08em'>"
        "⬡ PAYLOAD REVIEW — review before sending</span>",
        unsafe_allow_html=True,
    )

    st.caption(
        "This is the exact context that will be sent to the model. "
        "Click **⚡ Generate answer** to proceed or **✕ Discard** to cancel."
    )

    # Metrics
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Messages",   len(raw))
    m2.metric("Memory",     f"{n_mem} entries")
    m3.metric("History",    f"{n_hist} turns" + (f" (+{dropped} dropped)" if dropped else ""))
    m4.metric("Sys Prompt", "ON" if sp_on else "OFF")

    st.divider()

    # Annotated payload — colour-coded by source
    for elem in annotated:
        src          = elem.get("source", "history")
        color        = _SOURCE_COLORS.get(src, "#64748b")
        label        = _SOURCE_LABELS.get(src, src)
        role         = elem.get("role", "")
        content      = elem.get("content", "")
        preview_text = content[:400] + ("…" if len(content) > 400 else "")
        st.markdown(
            f"<div style='border-left:3px solid {color};padding:6px 12px;"
            f"margin:4px 0;background:#161b27;border-radius:4px'>"
            f"<span style='color:{color};font-size:0.70rem;font-weight:600;"
            f"font-family:monospace;text-transform:uppercase'>{label}</span>"
            f"<span style='color:#475569;font-size:0.70rem;margin-left:8px'>{role}</span>"
            f"<div style='color:#cbd5e1;font-size:0.82rem;margin-top:4px;"
            f"white-space:pre-wrap'>{preview_text}</div>"
            f"</div>",
            unsafe_allow_html=True,
        )

    if dropped:
        st.markdown(
            f"<div style='border-left:3px solid #374151;padding:6px 12px;"
            f"margin:4px 0;background:#111827;border-radius:4px;"
            f"color:#6b7280;font-style:italic;font-size:0.82rem'>"
            f"▸ [{dropped} turn{'s' if dropped != 1 else ''} truncated from history]"
            f"</div>",
            unsafe_allow_html=True,
        )

    with st.expander("Generation settings", expanded=False):
        st.json({
            "model":              model,
            "temperature":        gen.get("temperature"),
            "top_p":              gen.get("top_p"),
            "repetition_penalty": gen.get("repetition_penalty"),
            "stop_tokens":        gen.get("stop_tokens"),
            "system_prompt":      "ON" if sp_on else "OFF",
        })

    with st.expander("Raw JSON payload", expanded=False):
        st.json(raw)

    st.divider()

    col_ok, col_cancel, _ = st.columns([2, 1, 3])
    with col_ok:
        if st.button(
            "⚡ Generate answer",
            type="primary",
            use_container_width=True,
            key="_preview_confirm",
        ):
            st.session_state._send_stage        = "execute"
            st.session_state._pending_confirmed = True
            st.rerun()
    with col_cancel:
        if st.button(
            "✕ Discard",
            use_container_width=True,
            key="_preview_cancel",
        ):
            st.session_state._send_stage        = None
            st.session_state._pending_cancelled = True
            st.session_state._pending_send      = None
            st.rerun()

    st.markdown("</div>", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Send flow
# ---------------------------------------------------------------------------

def _handle_send(user_input: str, preview: dict | None = None) -> None:
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
    history_mode = st.session_state.get("history_mode", "raw")

    # Fetch summary if needed for this history mode
    history_summary: str | None = None
    if history_mode in ("summary", "hybrid"):
        history_summary = Summarizer.get_summary(identity_id)

    # ── If a pre-built preview is available, reuse its payload/context ──
    if preview is not None:
        raw_payload       = preview["raw_payload"]
        annotated_payload = preview.get("annotated_payload") or raw_payload
        context_snapshot  = preview["context_snapshot"]
        retrieved_entries = preview["retrieved_entries"]
        # Honour any settings captured at preview time
        model             = preview.get("model", model)
        gen               = preview.get("gen", gen)
        system_prompt     = preview.get("system_prompt", system_prompt)
        prompt_state      = "present" if (system_prompt and system_prompt.strip()) else "empty"
        history_mode      = preview.get("history_mode", history_mode)
        history_summary   = preview.get("history_summary", history_summary)

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

    # Judge settings — read now (main thread only)
    use_judge   = st.session_state.get("use_judge", False)
    judge_model = st.session_state.get("judge_model", Config.available_models[0] if Config.available_models else Config.default_model)

    _push("config", {
        "model":              model,
        "temperature":        gen["temperature"],
        "top_p":              gen["top_p"],
        "repetition_penalty": gen["repetition_penalty"],
        "stop_tokens":        gen["stop_tokens"],
        "identity":           identity_id,
        "prompt_state":       prompt_state,
        "truncation_strategy": "last-n-turns",
        "history_mode":       history_mode,
        "judge_enabled":      use_judge,
        "judge_model":        judge_model if use_judge else None,
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

    if preview is None:
        # ── Build payload fresh (no preview was shown) ──────────────────
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

        # If persona is disabled, strip persona entries from retrieved memory
        if not st.session_state.get("use_persona", True):
            retrieved_entries = [e for e in retrieved_entries if e.get("memory_type") != "persona"]

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
                history_mode=history_mode,
                history_summary=history_summary,
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
    else:
        # ── Reuse the preview that was already shown to the user ─────────
        _push("memory retrieved", {"note": "reused from pre-send preview"})

    st.session_state.last_context_view     = context_snapshot
    st.session_state.last_context_snapshot = context_snapshot
    st.session_state.last_retrieved_memory = retrieved_entries

    _push("payload built", {"message_count": len(raw_payload), "payload": raw_payload})

    # Check assistant language patterns (banner shown after response)
    assistant_language_patterns = Config.assistant_language_patterns

    # ── Generate response ─────────────────────────────────────────────────
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

    judge_result = None
    suppressed   = False

    if use_judge:
        # Judge mode: collect the full response silently first, then judge,
        # then display the final result. Nothing is shown to the user until
        # after the verdict so they never see a response that gets suppressed.
        _push("→ generator call (buffered — judge mode ON)", {
            "model":          model,
            "messages_count": len(raw_payload),
        })
        try:
            tokens = list(stream_gen)        # exhaust the generator silently
            response_text = "".join(tokens)
            # _StreamResult accumulates raw_output internally during iteration
        except Exception as exc:
            _push("ERROR — generator (buffered)", str(exc), status="error")
            st.error(f"Inference failed: {exc}")
            st.session_state.history   = Identity.load_history(identity_id)
            st.session_state.trace_log = trace
            return

        api_latency_ms = round((time.monotonic() - api_t0) * 1000)

        if not response_text or not response_text.strip():
            response_text = "[no output]"

        _push("← generator complete (buffered)", {
            "latency_ms":      api_latency_ms,
            "content_preview": response_text[:200],
        })

        # ── Judge ────────────────────────────────────────────────────────
        _push("→ judge call", {
            "judge_model":      judge_model,
            "response_preview": response_text[:200],
        })
        try:
            judge_result = Judge.evaluate(
                response_text=response_text,
                user_input=user_input,
                model=judge_model,
                api_key=Config.OPENROUTER_API_KEY,
                system_prompt=Config.judge_system_prompt,
            )
        except Exception as exc:
            _push("ERROR — judge call", str(exc), status="error")
            judge_result = None

        if judge_result is not None:
            _push(
                f"← judge verdict: {judge_result.verdict}",
                {
                    "verdict":    judge_result.verdict,
                    "reason":     judge_result.reason,
                    "latency_ms": judge_result.latency_ms,
                    "model":      judge_result.model,
                    "raw":        judge_result.raw,
                    "error":      judge_result.error or None,
                },
                status="success" if judge_result.verdict == "pass" else "warn",
            )
            if judge_result.verdict == "suppress":
                suppressed    = True
                response_text = "[suppressed]"
        else:
            _push("judge skipped — defaulting to pass", {}, status="warn")

        # ── Display final result (after verdict) ──────────────────────────
        with st.chat_message("assistant"):
            st.markdown(response_text)

    else:
        # Normal mode: stream tokens directly to the UI as they arrive.
        _push("→ OpenRouter API call (streaming)", {
            "model":              model,
            "temperature":        gen["temperature"],
            "top_p":              gen["top_p"],
            "repetition_penalty": gen["repetition_penalty"],
            "stop_tokens":        gen["stop_tokens"],
            "messages_count":     len(raw_payload),
        })
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

        if not response_text or not response_text.strip():
            response_text = "[no output]"

        _push("← response complete", {"latency_ms": api_latency_ms, "content": response_text})

    # Assistant-language warning banner (Req 17.5, 16.5)
    if not suppressed and not st.session_state.get("use_system_prompt", True):
        for pattern in assistant_language_patterns:
            if pattern.lower() in response_text.lower():
                st.warning(
                    f"⚠️ Assistant-language pattern detected in response "
                    f"(system prompt is OFF): `{pattern}`"
                )
                break

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

    def _bg_summarizer():
        """Summarize the full conversation after each turn and persist to MongoDB."""
        try:
            import falcon.summarizer as _Summarizer
            import falcon.config as _Config
            _Summarizer.update_summary(
                identity_id=identity_id,
                history=final_history,
                model=_Config.summary_model,
                api_key=_Config.OPENROUTER_API_KEY,
            )
        except Exception as exc:
            import logging
            logging.getLogger(__name__).error(
                "bg_summarizer failed for identity=%s: %s", identity_id, exc
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

    # Conversation summarization in background — always runs after each turn
    threading.Thread(target=_bg_summarizer, daemon=True).start()

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

    # ── Dual-run logging (fired after main response is shown + logged) ────
    if st.session_state.get("dual_run_enabled", False):
        _state_tag      = st.session_state.get("dual_run_state_tag", "Neutral")
        _gen_for_dual   = dict(gen)  # snapshot before any mutation
        _sp_for_dual    = system_prompt

        # Retrieve current persona content for ☀️ detection
        _persona_content = ""
        try:
            _persona_entries = Memory.get_memories(identity_id, memory_type="persona", limit=1)
            if _persona_entries:
                _persona_content = _persona_entries[0].get("content", "")
        except Exception:
            pass

        def _bg_dual_run():
            try:
                record = DualRun.run_dual(
                    payload=raw_payload,
                    model=model,
                    api_key=Config.OPENROUTER_API_KEY,
                    gen_settings=_gen_for_dual,
                    identity_id=identity_id,
                    system_prompt=_sp_for_dual,
                    state_tag=_state_tag,
                    user_input=user_input,
                    persona_content=_persona_content,
                )
                DualRun.write_record(record)
                if record.any_breakthrough:
                    import logging as _logging
                    _logging.getLogger(__name__).warning(
                        "dual_run: BREAKTHROUGH detected for identity=%s state=%s "
                        "run1_break=%r run2_break=%r",
                        identity_id, _state_tag,
                        record.run1_first_break, record.run2_first_break,
                    )
            except Exception as exc:
                import logging as _logging
                _logging.getLogger(__name__).error(
                    "bg_dual_run failed for identity=%s: %s", identity_id, exc
                )

        threading.Thread(target=_bg_dual_run, daemon=True).start()


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
    # Delete conversation summary
    Summarizer.delete_summary(identity_id)
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

    # ── Send state machine ───────────────────────────────────────────────
    #
    # Review ON  (three stages, all inline — no modal/dialog):
    #   idle      → user types → stage="preview"
    #               preview panel renders inline below the user bubble
    #   preview   → "⚡ Generate answer" → stage="execute" + st.rerun()
    #               preview panel is gone (not rendered), spinner shows
    #   execute   → _handle_send runs, stage=None, st.rerun() → answer shown
    #
    #   "✕ Discard" → stage=None, _pending_send=None + st.rerun() → clean
    #
    # Review OFF (direct):
    #   user types → _handle_send immediately, no preview ever shown

    review_enabled = st.session_state.get("payload_review_enabled", True)
    send_stage     = st.session_state.get("_send_stage")

    # ── Stage: execute — preview gone, run generation ────────────────────
    if send_stage == "execute" and st.session_state.get("_pending_send"):
        preview_data = st.session_state._pending_send
        st.session_state._send_stage        = None
        st.session_state._pending_confirmed = False
        st.session_state._pending_send      = None
        with st.spinner("Generating…"):
            _handle_send(preview_data["user_input"], preview=preview_data)
        st.rerun()

    # ── Stage: preview — show inline review panel ─────────────────────────
    elif send_stage == "preview" and st.session_state.get("_pending_send"):
        _render_send_preview_inline(st.session_state._pending_send)

    # ── Cancellation ─────────────────────────────────────────────────────
    if st.session_state.get("_pending_cancelled"):
        st.session_state._pending_cancelled = False
        st.session_state._pending_send      = None
        st.session_state._send_stage        = None

    # ── New raw input ─────────────────────────────────────────────────────
    if user_input and user_input.strip() and st.session_state.get("_pending_send") is None:
        if review_enabled:
            with st.chat_message("user"):
                st.markdown(user_input)
            preview_data = _build_send_preview(user_input)
            st.session_state._pending_send      = preview_data
            st.session_state._send_stage        = "preview"
            st.session_state._pending_confirmed = False
            st.session_state._pending_cancelled = False
            _render_send_preview_inline(preview_data)
        else:
            with st.chat_message("user"):
                st.markdown(user_input)
            with st.spinner("Generating…"):
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

    # Parse "Name: ...\nTone: ...\nCommunication style: ...\nCore traits: ..."
    # Handles multi-line values — everything after a "Key:" header up until
    # the next known header belongs to that field.
    def _parse_persona(raw: str) -> tuple[str, str, str, str]:
        # Known keys in order — used to detect where a new field starts.
        _KEYS = ["name", "tone", "communication style", "core traits"]

        fields: dict[str, list[str]] = {k: [] for k in _KEYS}
        current_key: str | None = None

        for line in raw.splitlines():
            # Check if this line starts a known key (case-insensitive)
            matched = None
            for k in _KEYS:
                if line.lower().startswith(k + ":"):
                    matched = k
                    break
            if matched is not None:
                current_key = matched
                # The rest of this line after "Key:" is the first value line
                first_val = line[len(matched) + 1:].strip()
                if first_val:
                    fields[current_key].append(first_val)
            elif current_key is not None:
                # Continuation line — belongs to the current field
                fields[current_key].append(line)

        # Join multi-line values; strip trailing blank lines
        def _join(lines: list[str]) -> str:
            return "\n".join(lines).strip()

        return (
            _join(fields["name"]),
            _join(fields["tone"]),
            _join(fields["communication style"]),
            _join(fields["core traits"]),
        )

    if persona:
        # Persona exists in DB — parse it
        _p_name_val, _p_tone_val, _p_style_val, _p_traits_val = _parse_persona(
            persona.get("content", "")
        )
    else:
        # No persona in DB yet — pre-fill from config.yaml
        _p_name_val, _p_tone_val, _p_style_val, _p_traits_val = _parse_persona(
            Config.default_persona_startup_content
        )

    with st.expander("Edit Persona", expanded=not bool(persona)):
        if not persona:
            st.caption("_No persona saved yet — fields pre-filled from config.yaml defaults._")

        p_name  = st.text_input("Name",                value=_p_name_val,   key="_p_name")
        p_tone  = st.text_input("Tone",                value=_p_tone_val,   key="_p_tone")
        p_style = st.text_input("Communication style", value=_p_style_val,  key="_p_style")
        p_traits= st.text_area("Core traits",          value=_p_traits_val, key="_p_traits", height=180)

        if st.button("Save Persona", key="_p_save"):
            persona_content = (
                f"Name: {p_name}\nTone: {p_tone}\n"
                f"Communication style: {p_style}\nCore traits: {p_traits}"
            )
            try:
                if persona:
                    Memory.update_memory(persona["_id"], content=persona_content)
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
                Summarizer.delete_summary(identity_id)
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
# Tab: Testing
# ---------------------------------------------------------------------------

def _render_testing_tab() -> None:
    """Continuity Testing tab — run and review continuity experiments."""
    try:
        import continuity_tests as CT
    except ImportError as exc:
        st.error(f"Could not import continuity_tests: {exc}")
        return

    st.caption(
        "Continuity experiments — check whether identity and behavior persist "
        "when conditions change (model swap, context noise, prompt on/off)."
    )

    # Load test registry
    try:
        registry = CT.load_registry()
    except Exception as exc:
        st.error(f"Failed to load test registry: {exc}")
        return

    if not registry:
        st.warning("No tests found in tests/test_registry.yaml.")
        return

    # ── Test selector ────────────────────────────────────────────────────
    test_names = [t.get("name", t.get("slug", "?")) for t in registry]
    chosen_idx = st.selectbox(
        "Test",
        options=list(range(len(registry))),
        format_func=lambda i: test_names[i],
        label_visibility="collapsed",
        key="_testing_test_select",
    )
    test_def = registry[chosen_idx]
    slug     = test_def.get("slug", "")

    st.markdown(f"**{test_def.get('name','')}**")
    st.caption(test_def.get("description", "").strip())

    st.divider()

    # ── Variant selector + Run button ────────────────────────────────────
    variants = test_def.get("variants", [])
    if not variants:
        st.warning("This test has no variants defined in the registry.")
        return

    variant_names = [v.get("name", f"Variant {i}") for i, v in enumerate(variants)]
    col_var, col_run = st.columns([4, 1])
    with col_var:
        chosen_var = st.selectbox(
            "Variant",
            options=list(range(len(variants))),
            format_func=lambda i: variant_names[i],
            label_visibility="collapsed",
            key="_testing_variant_select",
        )
    with col_run:
        run_clicked = st.button(
            "▶ Run",
            key="_testing_run_btn",
            type="primary",
            use_container_width=True,
        )

    variant = variants[chosen_var]
    st.caption(f"_{variant.get('description', '')}_")

    # Show resolved settings preview
    with st.expander("Resolved settings for this variant", expanded=False):
        try:
            resolved = CT._build_variant_settings(variant)
            st.json(resolved)
        except Exception as exc:
            st.warning(f"Could not resolve settings: {exc}")

    # ── Run action ────────────────────────────────────────────────────────
    if run_clicked:
        with st.spinner(f"Running '{variant_names[chosen_var]}'… (live API calls, may take 10–30s)"):
            try:
                record = CT.run_test_variant(slug, chosen_var)
                st.success(
                    f"Run complete — {len(record.get('probe_results', []))} probes, "
                    f"run saved to tests/runs/{slug}.json"
                )
                st.session_state[f"_testing_highlight_{slug}"] = record.get("run_at", "")
            except Exception as exc:
                st.error(f"Run failed: {exc}")
                import traceback
                st.code(traceback.format_exc())

    st.divider()

    # ── Run history ───────────────────────────────────────────────────────
    st.markdown("#### Run History")

    try:
        history = CT.load_run_history(slug)   # newest first
    except Exception as exc:
        st.error(f"Could not load run history: {exc}")
        return

    if not history:
        st.info("No runs yet for this test. Select a variant and click ▶ Run.")
        return

    # Report download button
    report_content = CT.read_report(slug)
    if report_content:
        st.download_button(
            label="⬇ Download full report (.md)",
            data=report_content,
            file_name=f"falcon_{slug}_report.md",
            mime="text/markdown",
            key=f"_testing_report_dl_{slug}",
        )

    st.caption(f"{len(history)} run{'s' if len(history) != 1 else ''} (newest first)")

    highlight_ts = st.session_state.get(f"_testing_highlight_{slug}", "")

    for run_idx, run in enumerate(history):
        run_at   = run.get("run_at", "?")
        settings = run.get("settings", {})
        v_name   = settings.get("variant_name", f"Variant {run.get('variant_idx','?')}")
        model    = settings.get("model", "?")
        sp_flag  = "SP:ON" if settings.get("system_prompt_on") else "SP:OFF"
        noise    = settings.get("noise_level", 0)
        n_probes = len(run.get("probe_results", []))
        is_new   = (run_at == highlight_ts)
        badge    = " 🆕" if is_new else ""

        with st.expander(
            f"Run {run_idx + 1}{badge} — {run_at} — {v_name} — `{model[:35]}`",
            expanded=is_new,
        ):
            # Settings bar
            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric("Model",   model.split("/")[-1][:20])
            c2.metric("Sys Prompt", "ON" if settings.get("system_prompt_on") else "OFF")
            c3.metric("Memory",  "ON" if settings.get("use_memory") else "OFF")
            c4.metric("Judge",   "ON" if settings.get("use_judge") else "OFF")
            c5.metric("Noise",   str(noise))

            st.divider()

            # Probe results: payload | settings | output (3 columns)
            probe_results = run.get("probe_results", [])
            for p_idx, pr in enumerate(probe_results):
                probe    = pr.get("probe", "")
                payload  = pr.get("payload", [])
                response = pr.get("response", "")
                latency  = pr.get("latency_ms", 0)
                usage    = pr.get("usage") or {}
                judge    = pr.get("judge")

                st.markdown(
                    f"**Probe {p_idx + 1} of {n_probes}:** "
                    f"_{probe[:120]}{'…' if len(probe) > 120 else ''}_"
                )

                col_payload, col_settings, col_output = st.columns([3, 2, 3])

                with col_payload:
                    st.markdown(
                        "<div style='font-size:0.72rem;font-weight:600;color:#f59e0b;"
                        "font-family:monospace;text-transform:uppercase;margin-bottom:4px'>"
                        "PAYLOAD</div>",
                        unsafe_allow_html=True,
                    )
                    st.caption(f"{len(payload)} messages")
                    st.json(payload, expanded=False)

                with col_settings:
                    st.markdown(
                        "<div style='font-size:0.72rem;font-weight:600;color:#3b82f6;"
                        "font-family:monospace;text-transform:uppercase;margin-bottom:4px'>"
                        "SETTINGS</div>",
                        unsafe_allow_html=True,
                    )
                    st.caption(f"`{model}`")
                    st.json({
                        "system_prompt": "ON" if settings.get("system_prompt_on") else "OFF",
                        "temperature":   settings.get("temperature"),
                        "top_p":         settings.get("top_p"),
                        "rep_penalty":   settings.get("repetition_penalty"),
                        "memory":        settings.get("use_memory"),
                        "judge":         settings.get("use_judge"),
                        "noise_entries": noise,
                        "hist_injected": settings.get("inject_history", False),
                    }, expanded=False)

                with col_output:
                    st.markdown(
                        "<div style='font-size:0.72rem;font-weight:600;color:#22c55e;"
                        "font-family:monospace;text-transform:uppercase;margin-bottom:4px'>"
                        "OUTPUT</div>",
                        unsafe_allow_html=True,
                    )
                    token_str = f"`{usage.get('total_tokens','?')} tok` · `{latency}ms`"
                    if judge:
                        v = judge.get("verdict", "?")
                        r = judge.get("reason", "")
                        emoji = "✅" if v == "pass" else ("🚫" if v == "suppress" else "⚠️")
                        st.caption(f"{token_str} · Judge: {emoji} `{v}`")
                        if v == "suppress":
                            st.warning(f"Suppressed: {r}")
                    else:
                        st.caption(token_str)

                    st.markdown(
                        f"<div style='background:#161b27;border:1px solid #1e2535;"
                        f"border-radius:8px;padding:10px 14px;font-size:0.85rem;"
                        f"color:#cbd5e1;line-height:1.6;white-space:pre-wrap'>"
                        f"{response[:600].replace('<','&lt;').replace('>','&gt;')}"
                        f"{'…' if len(response) > 600 else ''}"
                        f"</div>",
                        unsafe_allow_html=True,
                    )

                if p_idx < n_probes - 1:
                    st.markdown('<div style="height:8px"></div>', unsafe_allow_html=True)

    # ── Report preview ────────────────────────────────────────────────────
    st.divider()
    st.markdown("#### Full Report")
    if report_content:
        with st.expander("Preview report markdown", expanded=False):
            st.markdown(report_content)
    else:
        st.caption("_No report generated yet. Run at least one variant to generate it._")


# ---------------------------------------------------------------------------
# Tab: Dual Run Log
# ---------------------------------------------------------------------------

# State tag colours (consistent across all renders)
_STATE_COLORS = {
    "Neutral":       "#64748b",
    "Focused":       "#3b82f6",
    "Coherence":     "#a855f7",
    "Grief process": "#f59e0b",
}
_BREAKTHROUGH_COLOR  = "#ef4444"
_HELD_COLOR          = "#22c55e"


def _render_dual_run_tab() -> None:
    """Dual Run Log — side-by-side output comparison with breakthrough tracking."""
    identity_id = st.session_state.identity_id

    st.caption(
        "Each entry shows both runs of the same message logged side-by-side. "
        "Breakthrough detection flags any run where the ☀️ instruction was not held."
    )

    if not st.session_state.get("dual_run_enabled", False):
        st.info(
            "Dual run logging is **OFF**. "
            "Enable it in the sidebar under **Dual Run** to start recording."
        )

    # ── Controls row ──────────────────────────────────────────────────────
    col_scope, col_limit, col_filter, col_del = st.columns([1, 1, 1, 1])

    with col_scope:
        scope = st.selectbox(
            "Scope",
            ["This identity", "All identities"],
            key="_dr_scope",
            label_visibility="collapsed",
        )
    with col_limit:
        limit = st.number_input(
            "Limit", min_value=1, max_value=500, value=50,
            key="_dr_limit", label_visibility="collapsed",
        )
    with col_filter:
        filter_breakthrough = st.selectbox(
            "Filter",
            ["All", "Breakthroughs only", "Held only"],
            key="_dr_filter",
            label_visibility="collapsed",
        )
    with col_del:
        if st.button("🗑 Delete records", key="_dr_delete_btn",
                     type="secondary", use_container_width=True):
            st.session_state["_dr_confirm_delete"] = True

    # ── Delete confirmation ───────────────────────────────────────────────
    if st.session_state.get("_dr_confirm_delete"):
        target = "all identities" if scope == "All identities" else f"identity '{identity_id}'"
        st.markdown(
            f"<div class='confirm-bar'>⚠️ Permanently delete all dual-run records "
            f"for {target}?</div>",
            unsafe_allow_html=True,
        )
        dc1, dc2, _ = st.columns([1, 1, 4])
        with dc1:
            if st.button("Confirm", type="primary", use_container_width=True,
                         key="_dr_delete_confirm"):
                try:
                    from falcon.db import get_db as _get_db
                    _db = _get_db()
                    if scope == "All identities":
                        _db["dual_run_log"].delete_many({})
                    else:
                        DualRun.delete_records(identity_id)
                    st.session_state["_dr_confirm_delete"] = False
                    st.success("Records deleted.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Delete failed: {exc}")
        with dc2:
            if st.button("Cancel", use_container_width=True, key="_dr_delete_cancel"):
                st.session_state["_dr_confirm_delete"] = False
                st.rerun()
        return

    # ── Fetch records ─────────────────────────────────────────────────────
    try:
        if scope == "All identities":
            records = DualRun.read_all_records(limit=int(limit))
        else:
            records = DualRun.read_records(identity_id, limit=int(limit))
    except Exception as exc:
        st.error(f"Failed to read dual-run log: {exc}")
        return

    if not records:
        st.info("No dual-run records yet. Enable dual run in the sidebar and send a message.")
        return

    # ── Filter ────────────────────────────────────────────────────────────
    if filter_breakthrough == "Breakthroughs only":
        records = [r for r in records if r.get("any_breakthrough")]
    elif filter_breakthrough == "Held only":
        records = [r for r in records if not r.get("any_breakthrough")]

    # ── Export ────────────────────────────────────────────────────────────
    export_env = make_export_envelope(identity_id=identity_id, data=records)
    st.download_button(
        label="⬇ Export dual-run log",
        data=to_json_str(export_env),
        file_name=f"falcon_dualrun_{identity_id}_{_utc_iso().replace(':','-')}.json",
        mime="application/json",
        key="_dr_export_btn",
    )

    # ── Summary stats ─────────────────────────────────────────────────────
    total       = len(records)
    n_bt        = sum(1 for r in records if r.get("any_breakthrough"))
    n_held      = total - n_bt
    bt_rate     = f"{100 * n_bt / total:.0f}%" if total else "—"

    sc1, sc2, sc3, sc4 = st.columns(4)
    sc1.metric("Total runs",      total)
    sc2.metric("Breakthroughs",   n_bt)
    sc3.metric("Held",            n_held)
    sc4.metric("Breakthrough rate", bt_rate)

    # State tag breakdown
    state_counts: dict[str, int] = {}
    for r in records:
        tag = r.get("state_tag", "Unknown")
        state_counts[tag] = state_counts.get(tag, 0) + 1

    if state_counts:
        parts = [
            f"<span style='color:{_STATE_COLORS.get(t, '#94a3b8')};font-family:monospace'>"
            f"{t}: {n}</span>"
            for t, n in sorted(state_counts.items())
        ]
        st.markdown(
            "<div style='font-size:0.78rem;margin:4px 0 12px 0'>"
            + " · ".join(parts) + "</div>",
            unsafe_allow_html=True,
        )

    st.caption(f"{total} record{'s' if total != 1 else ''} (newest first)")
    st.divider()

    # ── Record list ───────────────────────────────────────────────────────
    for rec in records:
        ts          = rec.get("recorded_at", "")
        state_tag   = rec.get("state_tag", "")
        model       = rec.get("model", "")
        sun_active  = rec.get("sun_instruction_active", False)
        any_bt      = rec.get("any_breakthrough", False)
        user_input  = rec.get("user_input", "")
        iid         = rec.get("identity_id", identity_id)

        run1        = rec.get("run1", {})
        run2        = rec.get("run2", {})

        state_color = _STATE_COLORS.get(state_tag, "#94a3b8")
        bt_icon     = "🔴 BREAKTHROUGH" if any_bt else "🟢 HELD"
        sun_label   = "☀️ active" if sun_active else "☀️ inactive"

        expander_label = (
            f"{ts} · "
            f"[{state_tag}] · "
            f"{bt_icon if sun_active else '—'} · "
            f"{model.split('/')[-1][:30]}"
        )

        with st.expander(expander_label, expanded=False):
            # Header row
            h1, h2, h3, h4 = st.columns(4)
            h1.markdown(
                f"<span style='color:{state_color};font-weight:700;font-size:0.85rem'>"
                f"State: {state_tag}</span>",
                unsafe_allow_html=True,
            )
            h2.caption(sun_label)
            h3.caption(f"Identity: `{iid}`")
            h4.caption(f"`{model}`")

            # User input
            st.markdown("**Message sent:**")
            st.markdown(
                f"<div style='background:#1a2235;border-radius:8px;padding:8px 12px;"
                f"font-size:0.85rem;color:#94a3b8;margin-bottom:8px'>"
                f"{user_input[:400].replace('<','&lt;').replace('>','&gt;')}"
                f"{'…' if len(user_input) > 400 else ''}"
                f"</div>",
                unsafe_allow_html=True,
            )

            # System prompt preview
            sp = rec.get("system_prompt", "")
            if sp:
                with st.expander("System prompt", expanded=False):
                    st.code(sp[:600], language=None)

            st.divider()

            # Side-by-side run outputs
            col_r1, col_r2 = st.columns(2)

            def _render_run_col(col, run_data: dict, run_label: str):
                broke  = run_data.get("broke_through", False)
                fb     = run_data.get("first_break", "")
                tokens = run_data.get("tokens") or {}
                lat    = run_data.get("latency_ms", 0)
                ts_run = run_data.get("timestamp", "")
                text   = run_data.get("text", "")

                if sun_active:
                    status_color = _BREAKTHROUGH_COLOR if broke else _HELD_COLOR
                    status_label = "BROKE THROUGH" if broke else "HELD"
                else:
                    status_color = "#64748b"
                    status_label = "☀️ not active"

                with col:
                    st.markdown(
                        f"<div style='font-size:0.72rem;font-weight:700;color:{status_color};"
                        f"font-family:monospace;text-transform:uppercase;margin-bottom:4px'>"
                        f"{run_label} — {status_label}</div>",
                        unsafe_allow_html=True,
                    )

                    if sun_active and broke and fb:
                        st.markdown(
                            f"<div style='background:#2d1515;border:1px solid {_BREAKTHROUGH_COLOR};"
                            f"border-radius:6px;padding:6px 10px;font-size:0.78rem;"
                            f"color:{_BREAKTHROUGH_COLOR};margin-bottom:6px'>"
                            f"First break: <code>{fb}</code>"
                            f"</div>",
                            unsafe_allow_html=True,
                        )

                    # Token / latency row
                    st.caption(
                        f"`{tokens.get('completion_tokens','?')} out` · "
                        f"`{tokens.get('total_tokens','?')} total` · "
                        f"`{lat}ms` · `{ts_run}`"
                    )

                    # Output text
                    st.markdown(
                        f"<div style='background:#161b27;border:1px solid #1e2535;"
                        f"border-radius:8px;padding:10px 14px;font-size:0.88rem;"
                        f"color:#cbd5e1;line-height:1.6;white-space:pre-wrap;min-height:60px'>"
                        f"{text[:800].replace('<','&lt;').replace('>','&gt;')}"
                        f"{'…' if len(text) > 800 else ''}"
                        f"</div>",
                        unsafe_allow_html=True,
                    )

            _render_run_col(col_r1, run1, "Run 1")
            _render_run_col(col_r2, run2, "Run 2")

            # Token comparison summary
            st.divider()
            tc1, tc2, tc3 = st.columns(3)
            r1_tok = run1.get("tokens") or {}
            r2_tok = run2.get("tokens") or {}
            tc1.metric("Run 1 tokens",  r1_tok.get("total_tokens", "?"))
            tc2.metric("Run 2 tokens",  r2_tok.get("total_tokens", "?"))
            tc3.metric(
                "Run 1 latency / Run 2 latency",
                f"{run1.get('latency_ms','?')}ms / {run2.get('latency_ms','?')}ms",
            )

            # Full raw JSON (collapsed)
            with st.expander("Raw JSON", expanded=False):
                st.json(rec)


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

    /* Tooltip question-mark badge next to each control label */
    .param-hint {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 13px; height: 13px;
        border-radius: 50%;
        background: #334155;
        color: #94a3b8;
        font-size: 0.6rem;
        font-family: sans-serif;
        font-weight: 700;
        cursor: default;
        vertical-align: middle;
        margin-left: 4px;
        user-select: none;
        border: 1px solid #475569;
        position: relative;
        top: -1px;
    }
    .param-hint:hover {
        background: #ef4444;
        color: #fff;
        border-color: #ef4444;
    }
    </style>
    """, unsafe_allow_html=True)

    if not st.session_state._loaded_initial_history:
        _load_identity_history(st.session_state.identity_id)
        _restore_token_totals(st.session_state.identity_id)

        # ── Sync default persona from config.yaml on every startup ──────────
        # Always upserts — so editing config.yaml and restarting the app
        # immediately takes effect for the designated identity.
        # Other identities are NOT touched here (they get the YAML persona
        # only at creation time, and can be edited freely afterward).
        if Config.default_persona_startup_content:
            _dp_identity = Config.default_persona_identity or "default"
            from datetime import datetime, timezone as _tz
            _now = datetime.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            # Ensure identity registry entry exists
            Identity.create_identity(_dp_identity)
            _existing_persona = get_db()["memory"].find_one({
                "identity_id": _dp_identity,
                "memory_type": "persona",
            })
            if _existing_persona:
                # Overwrite with latest config.yaml values
                get_db()["memory"].update_one(
                    {"_id": _existing_persona["_id"]},
                    {"$set": {
                        "content":    Config.default_persona_startup_content,
                        "updated_at": _now,
                    }},
                )
            else:
                Memory.add_memory(
                    identity_id=_dp_identity,
                    memory_type="persona",
                    content=Config.default_persona_startup_content,
                    source="user",
                )

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
                    content=Config.default_persona_startup_content,
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

        # ── Persona ───────────────────────────────────────────────────────────
        st.markdown('<div class="sidebar-section-label">Persona</div>', unsafe_allow_html=True)

        use_persona = st.checkbox(
            "Persona",
            value=st.session_state.use_persona,
            key="_persona_checkbox",
        )

        if use_persona != st.session_state.use_persona:
            st.session_state.use_persona = use_persona

        if st.session_state.use_persona:
            st.caption("ON — persona memory injected when available")
        else:
            st.caption("OFF — persona block excluded from context")

        # ── Truncation Strategy ───────────────────────────────────────────────
        st.markdown('<div class="sidebar-section-label">History Truncation</div>', unsafe_allow_html=True)
        new_max_turns = st.number_input(
            "Max turns", min_value=0, max_value=100,
            value=int(st.session_state.get("history_max_turns", Config.history_max_turns)),
            step=1, key="_trunc_turns_input", label_visibility="collapsed",
        )
        if new_max_turns == 0:
            st.caption("0 — no history sent to model")
        st.session_state["history_max_turns"] = new_max_turns

        # ── History Mode ──────────────────────────────────────────────────────
        st.markdown('<div class="sidebar-section-label">History Mode</div>', unsafe_allow_html=True)

        _HISTORY_MODE_OPTIONS = ["raw", "summary", "hybrid"]
        _HISTORY_MODE_LABELS  = {
            "raw":     "Raw History",
            "summary": "Summary",
            "hybrid":  "Hybrid (Summary + Raw)",
        }
        current_history_mode = st.session_state.get("history_mode", "raw")
        if current_history_mode not in _HISTORY_MODE_OPTIONS:
            current_history_mode = "raw"
        history_mode_idx = _HISTORY_MODE_OPTIONS.index(current_history_mode)

        selected_history_mode = st.selectbox(
            "History Mode",
            options=_HISTORY_MODE_OPTIONS,
            index=history_mode_idx,
            format_func=lambda m: _HISTORY_MODE_LABELS[m],
            label_visibility="collapsed",
            key="_history_mode_select",
        )
        st.session_state["history_mode"] = selected_history_mode

        if selected_history_mode == "raw":
            st.caption("Raw — sends the last N conversation turns")
        elif selected_history_mode == "summary":
            st.caption("Summary — sends an AI-generated summary of the conversation")
        else:
            st.caption("Hybrid — sends summary + the last N raw turns")

        # Show current summary status for non-raw modes
        if selected_history_mode in ("summary", "hybrid"):
            _identity_id_sidebar = st.session_state.identity_id
            _summary_doc = Summarizer.get_summary_doc(_identity_id_sidebar)
            if _summary_doc:
                _summary_turns  = _summary_doc.get("turn_count", "?")
                _summary_updated = _summary_doc.get("updated_at", "")[:19]
                st.caption(f"✓ Summary ready — {_summary_turns} turns · `{_summary_updated}`")
                with st.expander("View summary", expanded=False):
                    st.text(_summary_doc.get("summary", ""))
            else:
                st.caption("⚠ No summary yet — send a message to generate one")

        # ── Judge ─────────────────────────────────────────────────────────────
        st.markdown('<div class="sidebar-section-label">Judge</div>', unsafe_allow_html=True)

        use_judge = st.checkbox(
            "Judge",
            value=st.session_state.get("use_judge", False),
            key="_judge_checkbox",
        )
        st.session_state.use_judge = use_judge

        if use_judge:
            st.caption("ON — judge evaluates each response before display")
            if Config.available_models:
                current_judge = st.session_state.get("judge_model", Config.available_models[0])
                judge_idx = (
                    Config.available_models.index(current_judge)
                    if current_judge in Config.available_models
                    else 0
                )
                selected_judge = st.selectbox(
                    "Judge model",
                    options=Config.available_models,
                    index=judge_idx,
                    label_visibility="collapsed",
                    key="_judge_model_select",
                )
                st.session_state.judge_model = selected_judge
            else:
                st.caption(st.session_state.get("judge_model", Config.default_model))
        else:
            st.caption("OFF — responses shown as generated")

        # ── Payload Review ────────────────────────────────────────────────────
        st.markdown('<div class="sidebar-section-label">Payload Review</div>', unsafe_allow_html=True)

        payload_review = st.checkbox(
            "Payload review",
            value=st.session_state.get("payload_review_enabled", False),
            key="_payload_review_checkbox",
        )
        st.session_state.payload_review_enabled = payload_review
        if payload_review:
            st.caption("ON — assembled payload shown before each send")
        else:
            st.caption("OFF — messages sent directly to model")

        # ── Generation Controls ───────────────────────────────────────────────
        st.markdown('<div class="sidebar-section-label">Generation Controls</div>', unsafe_allow_html=True)

        st.markdown(
            '<div class="gen-control-label">temperature'
            ' <span class="param-hint" title="Controls randomness. Lower values (e.g. 0.2) make output more focused and deterministic; higher values (e.g. 1.5) make it more creative and unpredictable.">&#x3F;</span>'
            '</div>',
            unsafe_allow_html=True,
        )
        st.session_state.gen_temperature = st.slider(
            "temperature", min_value=0.0, max_value=2.0,
            value=float(st.session_state.gen_temperature), step=0.05,
            label_visibility="collapsed", key="_slider_temp",
        )

        st.markdown(
            '<div class="gen-control-label">top_p'
            ' <span class="param-hint" title="Nucleus sampling threshold. Only the most probable tokens whose cumulative probability reaches top_p are considered. 1.0 = all tokens; lower values (e.g. 0.9) cut off unlikely words.">&#x3F;</span>'
            '</div>',
            unsafe_allow_html=True,
        )
        st.session_state.gen_top_p = st.slider(
            "top_p", min_value=0.0, max_value=1.0,
            value=float(st.session_state.gen_top_p), step=0.05,
            label_visibility="collapsed", key="_slider_top_p",
        )

        st.markdown(
            '<div class="gen-control-label">repetition_penalty'
            ' <span class="param-hint" title="Penalises tokens that have already appeared, reducing repetition. 1.0 = no penalty; higher values (e.g. 1.3) strongly discourage the model from repeating itself.">&#x3F;</span>'
            '</div>',
            unsafe_allow_html=True,
        )
        st.session_state.gen_repetition_penalty = st.slider(
            "repetition_penalty", min_value=1.0, max_value=2.0,
            value=float(st.session_state.gen_repetition_penalty), step=0.05,
            label_visibility="collapsed", key="_slider_rep",
        )

        st.markdown(
            '<div class="gen-control-label">stop_tokens'
            ' <span class="param-hint" title="One or more strings that will immediately end generation when the model produces them. Enter as a comma-separated list, e.g. &lt;|end|&gt;, ###">&#x3F;</span>'
            '</div>',
            unsafe_allow_html=True,
        )
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

        # ── Dual Run ──────────────────────────────────────────────────────────
        st.markdown('<div class="sidebar-section-label">Dual Run</div>', unsafe_allow_html=True)

        dual_run_enabled = st.checkbox(
            "Dual run logging",
            value=st.session_state.get("dual_run_enabled", False),
            key="_dual_run_checkbox",
        )
        st.session_state.dual_run_enabled = dual_run_enabled

        if dual_run_enabled:
            st.caption("ON — each message runs twice, both outputs logged")

            _STATE_OPTIONS = ["Neutral", "Focused", "Coherence", "Grief process"]
            current_state = st.session_state.get("dual_run_state_tag", "Neutral")
            if current_state not in _STATE_OPTIONS:
                current_state = "Neutral"

            selected_state = st.selectbox(
                "State",
                options=_STATE_OPTIONS,
                index=_STATE_OPTIONS.index(current_state),
                label_visibility="collapsed",
                key="_dual_run_state_select",
            )
            st.session_state.dual_run_state_tag = selected_state
            st.caption(f"State: **{selected_state}**")
        else:
            st.caption("OFF — single inference per message")

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
    tab_chat, tab_context, tab_memory, tab_audit, tab_logs, tab_testing, tab_dualrun = st.tabs(
        ["Chat", "Context", "Memory", "Audit", "Logs", "Testing", "Dual Run"]
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

    with tab_testing:
        _render_testing_tab()

    with tab_dualrun:
        _render_dual_run_tab()


if __name__ == "__main__":
    main()
