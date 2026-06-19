"""
engine.py — Inference layer for Falcon.

Design principles (from Falcon spec):
  - Neutrality: no content is injected when system_prompt is absent.
  - Transparency: build_annotated_payload() returns the exact assembled context
    that will be sent — nothing hidden, each element labelled with its source.
  - Always output: never return None, never return empty silently.
    If the model returns empty, a fallback marker is returned.
  - Replaceable model: all generation parameters are passed explicitly.
  - Visible generation controls: all explicit, never hidden.

Public API:
  build_payload(system_prompt, messages)
      → list[dict]

  build_annotated_payload(system_prompt, messages, memory_block, ...)
      → (list[dict], dict)  — annotated payload + context_snapshot.

  build_context_view(system_prompt, messages, retrieved_memory, documents)
      → dict

  stream_inference(model_name, payload, api_key, ...)
      → _StreamResult  — pass the pre-built payload directly.
"""
from __future__ import annotations

import concurrent.futures
import logging
import time
from typing import Callable

import openai
from openai import OpenAI

logger = logging.getLogger(__name__)

_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

_EMPTY_OUTPUT_MARKER = "[no output]"

# Module-level cached OpenAI client — one connection pool reused across all requests.
# Keyed by api_key so a key change gets a fresh client automatically.
_client_cache: dict[str, OpenAI] = {}


def _get_client(api_key: str) -> OpenAI:
    if api_key not in _client_cache:
        _client_cache[api_key] = OpenAI(
            api_key=api_key,
            base_url=_OPENROUTER_BASE_URL,
            default_headers={
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer":  "https://github.com/falcon",
                "X-Title":       "Falcon",
            },
        )
    return _client_cache[api_key]

VALID_SOURCES = frozenset({
    "system-prompt",
    "persona",
    "memory",
    "history",
    "user-input",
    "history-summary",
})

_RETRIEVAL_TIMEOUT_S = 0.5


# ---------------------------------------------------------------------------
# build_payload — backward-compatible simple assembler (no persona/memory)
# ---------------------------------------------------------------------------

def build_payload(system_prompt: str, messages: list[dict]) -> list[dict]:
    """Build the exact list of {role, content} dicts sent to the model.

    Falcon spec: if system_prompt is empty/whitespace, NO system message
    is prepended.

    NOTE: This function does NOT include persona or retrieved memory.
    Use build_annotated_payload for the full assembly path.
    """
    payload: list[dict] = []
    if system_prompt and system_prompt.strip():
        payload.append({"role": "system", "content": system_prompt})
    for message in messages:
        payload.append({"role": message["role"], "content": message["content"]})
    return payload


# ---------------------------------------------------------------------------
# Truncation helpers
# ---------------------------------------------------------------------------

def _estimate_tokens(content: str) -> int:
    return len(content) // 4


def _truncate_history_last_n(
    history: list[dict],
    max_turns: int,
) -> tuple[list[dict], int]:
    """Keep the most recent max_turns turn-pairs.

    max_turns=0 means no history is included — all messages are dropped.
    """
    if max_turns == 0:
        return [], len(history)

    pairs: list[list[dict]] = []
    i = 0
    while i < len(history):
        if (i + 1 < len(history)
                and history[i].get("role") == "user"
                and history[i + 1].get("role") == "assistant"):
            pairs.append([history[i], history[i + 1]])
            i += 2
        else:
            pairs.append([history[i]])
            i += 1

    if len(pairs) <= max_turns:
        return [msg for pair in pairs for msg in pair], 0

    dropped_pairs = pairs[:-max_turns]
    kept_pairs = pairs[-max_turns:]
    dropped_count = sum(len(p) for p in dropped_pairs)
    return [msg for pair in kept_pairs for msg in pair], dropped_count


# ---------------------------------------------------------------------------
# build_annotated_payload — primary assembly path (persona + memory + history)
# ---------------------------------------------------------------------------

def build_annotated_payload(
    system_prompt: str,
    messages: list[dict],
    memory_block: list[dict] | None = None,
    truncation_strategy: str = "last-n-turns",
    history_max_turns: int = 20,
    retrieve_fn: Callable | None = None,
    retrieval_kwargs: dict | None = None,
    history_mode: str = "raw",
    history_summary: str | None = None,
) -> tuple[list[dict], dict]:
    """Assemble and annotate the full payload sent to the model.

    Ordering:
        1. Persona block (if persona entry in memory_block)
        2. System prompt (if non-empty/non-whitespace)
        3. Memory block (non-persona entries as system message)
        4. History summary (for summary and hybrid modes)
        5. Conversation history turns (raw, for raw and hybrid modes)
        6. Current user input

    Args:
        history_mode:    One of "raw", "summary", "hybrid".
            "raw"     - send raw conversation history (truncated to history_max_turns)
            "summary" - send only AI-generated summary, no raw history turns
            "hybrid"  - send summary first, then recent raw turns
        history_summary: Pre-fetched summary text. Used for "summary" and "hybrid" modes.
                         If None and mode requires summary, the summary block is omitted.

    Returns:
        (annotated_payload, context_snapshot)
        annotated_payload: list of {role, content, source}
        context_snapshot: full transparency dict for the Context Viewer
    """
    _VALID_STRATEGIES = frozenset({"last-n-turns"})
    if truncation_strategy not in _VALID_STRATEGIES:
        raise ValueError(
            f"Invalid truncation_strategy: {truncation_strategy!r}. "
            f"Must be one of: {sorted(_VALID_STRATEGIES)}"
        )

    _VALID_HISTORY_MODES = frozenset({"raw", "summary", "hybrid"})
    if history_mode not in _VALID_HISTORY_MODES:
        history_mode = "raw"

    annotated: list[dict] = []
    memory_block = memory_block or []
    retrieval_timeout = False

    # Optional retrieval with 500ms timeout
    if retrieve_fn is not None:
        t_start = time.monotonic()
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(retrieve_fn, **(retrieval_kwargs or {}))
                try:
                    retrieval_result = future.result(timeout=_RETRIEVAL_TIMEOUT_S)
                    memory_block = getattr(retrieval_result, "entries", []) or []
                except concurrent.futures.TimeoutError:
                    elapsed_ms = round((time.monotonic() - t_start) * 1000)
                    logger.warning(
                        "retrieve_for_generation timed out after %dms; "
                        "proceeding with empty memory block.", elapsed_ms,
                    )
                    retrieval_timeout = True
                    memory_block = []
        except Exception as exc:
            logger.error("retrieve_for_generation raised: %s", exc)
            memory_block = []

    # Separate persona from other memory entries
    persona_entry: dict | None = None
    non_persona_memory: list[dict] = []
    for entry in memory_block:
        if entry.get("memory_type") == "persona":
            persona_entry = entry
        else:
            non_persona_memory.append(entry)

    # Separate current input from history
    if messages:
        history = messages[:-1]
        current_input = messages[-1]
    else:
        history = []
        current_input = {}

    # ── History assembly based on history_mode ────────────────────────────
    history_dropped_turns = 0
    included_history: list[dict] = []

    if history_mode == "raw":
        # Standard: send truncated raw history turns
        included_history, history_dropped_turns = _truncate_history_last_n(
            history, history_max_turns
        )
    elif history_mode == "summary":
        # Summary-only: no raw history turns sent at all
        included_history = []
        history_dropped_turns = len(history)
    elif history_mode == "hybrid":
        # Hybrid: summary prepended + recent raw turns
        included_history, history_dropped_turns = _truncate_history_last_n(
            history, history_max_turns
        )

    # 1. Persona block
    if persona_entry is not None:
        raw_persona = persona_entry.get("content", "").strip()
        persona_content = (
            "[PERSONA — this defines your identity and behavior. "
            "Adopt it completely for this conversation.]\n"
            + raw_persona
        )
        annotated.append({
            "role":    "system",
            "content": persona_content,
            "source":  "persona",
        })

    # 2. System prompt
    if system_prompt and system_prompt.strip():
        annotated.append({
            "role":    "system",
            "content": system_prompt,
            "source":  "system-prompt",
        })

    # 3. Memory block (non-persona)
    if non_persona_memory:
        by_type: dict[str, list[str]] = {}
        for e in non_persona_memory:
            t = e.get("memory_type", "memory")
            by_type.setdefault(t, []).append(e.get("content", "").strip())
        mem_lines = []
        for mem_type, contents in by_type.items():
            mem_lines.append(f"[{mem_type.upper()} MEMORY]")
            for c in contents:
                if c:
                    mem_lines.append(f"- {c}")
        annotated.append({
            "role":    "system",
            "content": "\n".join(mem_lines),
            "source":  "memory",
        })

    # 4. History summary block (for summary and hybrid modes)
    if history_mode in ("summary", "hybrid") and history_summary and history_summary.strip():
        annotated.append({
            "role":    "system",
            "content": (
                "[CONVERSATION SUMMARY — condensed record of the conversation so far. "
                "Use it as context for the current turn.]\n"
                + history_summary.strip()
            ),
            "source":  "history-summary",
        })

    # 5. Conversation history (raw turns — only in raw and hybrid modes)
    for msg in included_history:
        annotated.append({
            "role":    msg.get("role", "user"),
            "content": msg.get("content", ""),
            "source":  "history",
        })

    # 6. Current user input
    if current_input:
        annotated.append({
            "role":    current_input.get("role", "user"),
            "content": current_input.get("content", ""),
            "source":  "user-input",
        })

    # Raw payload (drop source key) — this is what gets sent to the model
    assembled_payload = [
        {"role": e["role"], "content": e["content"]}
        for e in annotated
    ]

    context_text = " ".join(e.get("content", "") for e in assembled_payload)
    context_token_estimate = _estimate_tokens(context_text)

    persona_block = next((e for e in annotated if e.get("source") == "persona"), None)
    system_prompt_val = system_prompt if (system_prompt and system_prompt.strip()) else None

    context_snapshot: dict = {
        "system_prompt":           system_prompt_val,
        "prompt_state":            "present" if system_prompt_val else "empty",
        "persona_block":           persona_block,
        "memory_entries":          [e for e in annotated if e.get("source") == "memory"],
        "history_included":        [e for e in annotated if e.get("source") == "history"],
        "history_dropped_turns":   history_dropped_turns,
        "truncation_strategy":     truncation_strategy,
        "history_mode":            history_mode,
        "history_summary":         history_summary if history_mode in ("summary", "hybrid") else None,
        "current_input":           next((e for e in annotated if e.get("source") == "user-input"), {}),
        "assembled_payload":       assembled_payload,
        "annotated_payload":       annotated,
        "context_token_estimate":  context_token_estimate,
        "retrieval_timeout":       retrieval_timeout,
        "retrieval_result":        None,
        "message_count":           len(assembled_payload),
    }

    return annotated, context_snapshot


# ---------------------------------------------------------------------------
# build_context_view — legacy context view for backward compatibility
# ---------------------------------------------------------------------------

def build_context_view(
    system_prompt: str,
    messages: list[dict],
    retrieved_memory: list[dict] | None = None,
    documents: list[str] | None = None,
) -> dict:
    """Legacy context view — uses simple build_payload (no persona/memory block)."""
    current_input = messages[-1] if messages else {}
    history = messages[:-1] if len(messages) > 1 else []
    assembled_payload = build_payload(system_prompt, messages)
    return {
        "system_prompt":       system_prompt if system_prompt and system_prompt.strip() else None,
        "prompt_state":        "present" if (system_prompt and system_prompt.strip()) else "empty",
        "current_input":       current_input,
        "conversation_history": history,
        "retrieved_memory":    retrieved_memory or [],
        "documents":           documents or [],
        "assembled_payload":   assembled_payload,
        "message_count":       len(assembled_payload),
    }


# ---------------------------------------------------------------------------
# _StreamResult — streaming OpenRouter call
# ---------------------------------------------------------------------------

class _StreamResult:
    """Wraps a streaming OpenRouter call.

    Accepts a pre-built payload directly — persona, system prompt, memory,
    and history are already assembled by build_annotated_payload before
    this is called. Nothing is rebuilt here.

    Usage:
        gen = stream_inference(model_name, payload, api_key, ...)
        text = st.write_stream(gen)
        usage = gen.usage
        raw_output = gen.raw_output
    """

    def __init__(
        self,
        model_name: str,
        payload: list[dict],
        api_key: str,
        temperature: float = 0.7,
        top_p: float = 1.0,
        repetition_penalty: float = 1.0,
        stop_tokens: list[str] | None = None,
    ):
        self.usage: dict = {}
        self.raw_output: str = ""
        self._model_name         = model_name
        self._payload            = payload
        self._api_key            = api_key
        self._temperature        = temperature
        self._top_p              = top_p
        self._repetition_penalty = repetition_penalty
        self._stop_tokens        = stop_tokens or []
        # Lazily initialised on first __next__ call so the HTTP connection
        # is not opened until Streamlit actually starts iterating.
        self._gen = None

    def _run(self):
        client = _get_client(self._api_key)

        call_kwargs: dict = {
            "model":          self._model_name,
            "messages":       self._payload,
            "temperature":    self._temperature,
            "top_p":          self._top_p,
            "stream":         True,
            "stream_options": {"include_usage": True},
        }
        if self._stop_tokens:
            call_kwargs["stop"] = self._stop_tokens
        if self._repetition_penalty != 1.0:
            call_kwargs["extra_body"] = {"repetition_penalty": self._repetition_penalty}

        stream = client.chat.completions.create(**call_kwargs)

        in_think  = False
        buf       = ""
        full_text = ""

        for chunk in stream:
            if chunk.usage:
                self.usage = {
                    "prompt_tokens":     chunk.usage.prompt_tokens,
                    "completion_tokens": chunk.usage.completion_tokens,
                    "total_tokens":      chunk.usage.total_tokens,
                }

            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            token = delta.content if (delta and delta.content) else ""
            if not token:
                continue

            full_text += token

            # Fast path: no think-tag processing needed for the common case.
            if not in_think and "<think>" not in token:
                yield token
                continue

            # Slow path: buffer and strip <think>…</think> blocks.
            buf += token
            out = ""
            while buf:
                if in_think:
                    end = buf.find("</think>")
                    if end == -1:
                        break           # wait for more chunks
                    buf      = buf[end + len("</think>"):]
                    in_think = False
                else:
                    start = buf.find("<think>")
                    if start == -1:
                        out += buf
                        buf  = ""
                    else:
                        out     += buf[:start]
                        buf      = buf[start + len("<think>"):]
                        in_think = True
            if out:
                yield out

        self.raw_output = full_text

        # Flush any remaining non-think buffer content
        if buf and not in_think:
            self.raw_output = full_text
            yield buf

        if not full_text.strip():
            self.raw_output = _EMPTY_OUTPUT_MARKER
            yield _EMPTY_OUTPUT_MARKER

    def __iter__(self):
        if self._gen is None:
            self._gen = self._run()
        return self._gen

    def __next__(self):
        if self._gen is None:
            self._gen = self._run()
        return next(self._gen)


def stream_inference(
    model_name: str,
    api_key: str,
    payload: list[dict] | None = None,
    # Legacy params kept for backward compatibility — ignored when payload provided
    system_prompt: str = "",
    messages: list[dict] | None = None,
    temperature: float = 0.7,
    top_p: float = 1.0,
    repetition_penalty: float = 1.0,
    stop_tokens: list[str] | None = None,
) -> "_StreamResult":
    """Stream tokens from OpenRouter using a pre-built payload.

    Always pass `payload` — the fully assembled list from build_annotated_payload.
    The legacy `system_prompt` + `messages` params are kept for backward compat
    but should not be used in new code (they don't include persona or memory).
    """
    if payload is None:
        payload = build_payload(system_prompt, messages or [])

    return _StreamResult(
        model_name=model_name,
        payload=payload,
        api_key=api_key,
        temperature=temperature,
        top_p=top_p,
        repetition_penalty=repetition_penalty,
        stop_tokens=stop_tokens,
    )
