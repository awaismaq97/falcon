"""
tests/continuity_tests.py — Continuity experiment runner for Falcon.

Runs registry-defined tests (model_relocation, context_dilution) against the
live OpenRouter API. Each run captures the full payload, settings, and raw output,
persists them as a JSON run record under tests/runs/, and regenerates a markdown
report under tests/reports/.

Public API:
    load_registry()                  -> list[dict]
    get_test_def(slug)               -> dict | None
    load_run_history(slug)           -> list[dict]
    run_test_variant(slug, variant_idx, identity_id) -> dict  (the new run record)
    generate_report(slug)            -> str  (path to the written .md file)
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_HERE        = os.path.dirname(os.path.abspath(__file__))
_REGISTRY    = os.path.join(_HERE, "test_registry.yaml")
_RUNS_DIR    = os.path.join(_HERE, "runs")
_REPORTS_DIR = os.path.join(_HERE, "reports")

os.makedirs(_RUNS_DIR,    exist_ok=True)
os.makedirs(_REPORTS_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Noise corpus for context dilution
# ---------------------------------------------------------------------------
_NOISE_ENTRIES = [
    ("The French Revolution began in 1789 with the storming of the Bastille.",         ["history", "france"]),
    ("Photosynthesis converts CO2 and water into glucose using sunlight.",              ["biology", "chemistry"]),
    ("The Pythagorean theorem states a² + b² = c² for right triangles.",               ["math", "geometry"]),
    ("Mount Everest stands at 8,849 metres above sea level.",                           ["geography"]),
    ("William Shakespeare was born in Stratford-upon-Avon in 1564.",                   ["literature", "history"]),
    ("The speed of light in a vacuum is approximately 299,792,458 m/s.",               ["physics"]),
    ("DNA is a double-helix polymer made of nucleotide base pairs.",                   ["biology", "genetics"]),
    ("The capital of Australia is Canberra, not Sydney.",                              ["geography", "australia"]),
    ("Ludwig van Beethoven composed his 9th Symphony while completely deaf.",           ["music", "history"]),
    ("The Great Wall of China is approximately 21,196 km long.",                       ["history", "china"]),
    ("Water boils at 100°C at standard atmospheric pressure (1 atm).",                 ["chemistry", "physics"]),
    ("Alan Turing proposed the Turing Test as a criterion for machine intelligence.",  ["computing", "history"]),
    ("The human brain contains approximately 86 billion neurons.",                     ["biology", "neuroscience"]),
    ("The periodic table currently has 118 confirmed chemical elements.",               ["chemistry"]),
    ("Black holes are regions of spacetime where gravity is so strong light cannot escape.", ["physics", "astronomy"]),
]

_NOISE_HISTORY = [
    {"role": "user",      "content": "Tell me about ancient Roman aqueducts."},
    {"role": "assistant", "content": "Roman aqueducts were gravity-fed channels built from 312 BC onward to supply cities with water."},
    {"role": "user",      "content": "What is the boiling point of ethanol?"},
    {"role": "assistant", "content": "Ethanol boils at 78.37°C at standard atmospheric pressure."},
    {"role": "user",      "content": "Name three Renaissance painters."},
    {"role": "assistant", "content": "Leonardo da Vinci, Michelangelo, and Raphael."},
    {"role": "user",      "content": "How does JPEG compression work?"},
    {"role": "assistant", "content": "JPEG uses discrete cosine transform to convert image blocks into frequency coefficients, then quantises high-frequency detail."},
    {"role": "user",      "content": "What is the Coriolis effect?"},
    {"role": "assistant", "content": "The Coriolis effect is the deflection of moving objects caused by Earth's rotation — rightward in the Northern Hemisphere."},
]


# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------

def load_registry() -> list[dict]:
    """Load and return the list of test definitions from test_registry.yaml."""
    with open(_REGISTRY, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("tests", [])


def get_test_def(slug: str) -> dict | None:
    """Return the test definition matching slug, or None."""
    for t in load_registry():
        if t.get("slug") == slug:
            return t
    return None


# ---------------------------------------------------------------------------
# Run persistence helpers
# ---------------------------------------------------------------------------

def _runs_path(slug: str) -> str:
    return os.path.join(_RUNS_DIR, f"{slug}.json")


def load_run_history(slug: str) -> list[dict]:
    """Return all run records for a test slug, newest first."""
    path = _runs_path(slug)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    runs = data if isinstance(data, list) else []
    return list(reversed(runs))   # newest first


def _append_run_record(slug: str, record: dict) -> None:
    """Append one run record to the slug's JSON history file."""
    path = _runs_path(slug)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            existing = json.load(f)
        if not isinstance(existing, list):
            existing = []
    else:
        existing = []
    existing.append(record)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# Config/variant resolver
# ---------------------------------------------------------------------------

def _resolve_model(model_slot: str) -> str:
    """Resolve __default__, __slot_0__, __slot_1__ etc. to actual model IDs."""
    import falcon.config as Config
    if model_slot == "__default__":
        return Config.default_model
    if model_slot == "__slot_0__":
        return Config.available_models[0] if Config.available_models else Config.default_model
    if model_slot == "__slot_1__":
        m = Config.available_models
        return m[1] if len(m) > 1 else (m[0] if m else Config.default_model)
    return model_slot   # literal model id


def _resolve_system_prompt(sp_value: Any) -> str:
    """Resolve __default__ to config default; None/null to empty string."""
    import falcon.config as Config
    if sp_value == "__default__":
        return Config.default_system_prompt
    if sp_value is None:
        return ""
    return str(sp_value)


def _build_variant_settings(variant: dict) -> dict:
    """Return a flat settings snapshot for a variant (for the run record)."""
    import falcon.config as Config
    model = _resolve_model(variant.get("model", "__default__"))
    sp    = _resolve_system_prompt(variant.get("system_prompt", "__default__"))
    return {
        "model":              model,
        "system_prompt":      sp,
        "system_prompt_on":   bool(sp and sp.strip()),
        "use_memory":         bool(variant.get("use_memory", False)),
        "use_judge":          bool(variant.get("use_judge", False)),
        "noise_level":        int(variant.get("noise_level", 0)),
        "inject_history":     bool(variant.get("inject_history", False)),
        "temperature":        float(Config.generation_temperature),
        "top_p":              float(Config.generation_top_p),
        "repetition_penalty": float(Config.generation_repetition_penalty),
        "extra_params":       variant.get("extra_params") or {},
        "variant_name":       variant.get("name", ""),
        "variant_description": variant.get("description", ""),
    }


# ---------------------------------------------------------------------------
# Low-level inference call (no streaming — we need the full text for analysis)
# ---------------------------------------------------------------------------

def _call_model(payload: list[dict], settings: dict) -> tuple[str, dict, int]:
    """Send payload to OpenRouter. Returns (response_text, usage_dict, latency_ms)."""
    import openai
    import falcon.config as Config

    client = openai.OpenAI(
        api_key=Config.OPENROUTER_API_KEY,
        base_url="https://openrouter.ai/api/v1",
        default_headers={
            "Authorization": f"Bearer {Config.OPENROUTER_API_KEY}",
            "HTTP-Referer":  "https://github.com/falcon",
            "X-Title":       "Falcon-ContinuityTest",
        },
    )
    t0 = time.monotonic()
    response = client.chat.completions.create(
        model=settings["model"],
        messages=payload,
        temperature=settings["temperature"],
        top_p=settings["top_p"],
        max_tokens=512,
    )
    latency_ms = round((time.monotonic() - t0) * 1000)
    text = (response.choices[0].message.content or "").strip() or "[no output]"
    usage: dict = {}
    if response.usage:
        usage = {
            "prompt_tokens":     response.usage.prompt_tokens,
            "completion_tokens": response.usage.completion_tokens,
            "total_tokens":      response.usage.total_tokens,
        }
    return text, usage, latency_ms


# ---------------------------------------------------------------------------
# Judge call (optional, reuses falcon.judge)
# ---------------------------------------------------------------------------

def _maybe_judge(response_text: str, user_input: str, settings: dict) -> dict | None:
    """Run judge if use_judge is True. Returns verdict dict or None."""
    if not settings.get("use_judge"):
        return None
    try:
        import falcon.judge  as Judge
        import falcon.config as Config
        result = Judge.evaluate(
            response_text=response_text,
            user_input=user_input,
            model=settings["model"],
            api_key=Config.OPENROUTER_API_KEY,
            system_prompt=Config.judge_system_prompt,
        )
        return {
            "verdict":    result.verdict,
            "reason":     result.reason,
            "latency_ms": result.latency_ms,
        }
    except Exception as exc:
        logger.warning("judge call failed in continuity test: %s", exc)
        return {"verdict": "error", "reason": str(exc), "latency_ms": 0}


# ---------------------------------------------------------------------------
# Payload builders per test type
# ---------------------------------------------------------------------------

def _build_payload_for_probe(
    probe_msg: dict,
    settings: dict,
    extra_memory: list[dict] | None = None,
    extra_history: list[dict] | None = None,
) -> list[dict]:
    """Build a plain payload list for a single probe message."""
    from falcon.engine import build_annotated_payload

    memory_block = list(extra_memory or [])
    history      = list(extra_history or [])
    messages     = history + [{"role": probe_msg["role"], "content": probe_msg["content"]}]

    annotated, _ = build_annotated_payload(
        system_prompt=settings["system_prompt"],
        messages=messages,
        memory_block=memory_block,
        truncation_strategy="last-n-turns",
        history_max_turns=20,
    )
    return [{"role": e["role"], "content": e["content"]} for e in annotated]


def _noise_memory_block(n: int) -> list[dict]:
    """Return n noise memory entries (cycles through _NOISE_ENTRIES)."""
    entries = []
    for i in range(n):
        content, tags = _NOISE_ENTRIES[i % len(_NOISE_ENTRIES)]
        entries.append({
            "memory_type": "semantic",
            "content":     content,
            "tags":        tags,
            "pinned":      False,
        })
    return entries


# ---------------------------------------------------------------------------
# Runner: Model Relocation Test
# ---------------------------------------------------------------------------

def run_model_relocation(test_def: dict, variant: dict) -> dict:
    """
    Sends each probe message independently (no accumulated history between probes)
    under the resolved model and settings. Records payload + output per probe.
    """
    settings = _build_variant_settings(variant)
    probes   = test_def.get("probe_messages", [])
    probe_results = []

    for probe in probes:
        payload = _build_payload_for_probe(probe, settings)
        try:
            response_text, usage, latency_ms = _call_model(payload, settings)
        except Exception as exc:
            response_text = f"[ERROR: {exc}]"
            usage, latency_ms = {}, 0

        judge_result = _maybe_judge(response_text, probe["content"], settings)

        probe_results.append({
            "probe":          probe["content"],
            "payload":        payload,
            "response":       response_text,
            "usage":          usage,
            "latency_ms":     latency_ms,
            "judge":          judge_result,
        })

    return {
        "test_slug":   test_def["slug"],
        "test_name":   test_def["name"],
        "runner_fn":   "run_model_relocation",
        "run_at":      datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "settings":    settings,
        "probe_results": probe_results,
    }


# ---------------------------------------------------------------------------
# Runner: Context Dilution Test
# ---------------------------------------------------------------------------

def run_context_dilution(test_def: dict, variant: dict) -> dict:
    """
    Injects noise memory entries (and optionally history) into the context
    alongside each probe message and records whether behavior persists.
    """
    settings      = _build_variant_settings(variant)
    probes        = test_def.get("probe_messages", [])
    noise_level   = settings.get("noise_level", 0)
    inject_history= settings.get("inject_history", False)

    memory_block  = _noise_memory_block(noise_level) if noise_level > 0 else []
    extra_history = _NOISE_HISTORY if inject_history else []
    probe_results = []

    for probe in probes:
        payload = _build_payload_for_probe(
            probe, settings,
            extra_memory=memory_block,
            extra_history=extra_history,
        )
        try:
            response_text, usage, latency_ms = _call_model(payload, settings)
        except Exception as exc:
            response_text = f"[ERROR: {exc}]"
            usage, latency_ms = {}, 0

        judge_result = _maybe_judge(response_text, probe["content"], settings)

        probe_results.append({
            "probe":        probe["content"],
            "payload":      payload,
            "response":     response_text,
            "usage":        usage,
            "latency_ms":   latency_ms,
            "judge":        judge_result,
            "noise_entries": noise_level,
            "history_injected": inject_history,
        })

    return {
        "test_slug":   test_def["slug"],
        "test_name":   test_def["name"],
        "runner_fn":   "run_context_dilution",
        "run_at":      datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "settings":    settings,
        "probe_results": probe_results,
    }


# ---------------------------------------------------------------------------
# Runner: History Mode Comparison Test
# ---------------------------------------------------------------------------

# ── Seeded conversation — planted facts, priorities, relationships, processes ──
#
# This conversation is injected as the "prior history" for the identity under
# test. It contains a mix of:
#   - Factual continuity:    concrete stated facts (name, project, numbers)
#   - Priority continuity:   what the user said matters most
#   - Identity continuity:   how the user described themselves / their role
#   - Relationship continuity: references to other people and dynamics
#   - Process continuity:    how the user said they want things done
#
# The retention probes (below) then ask questions that can only be answered
# correctly if each continuity dimension survived compression.

_SEEDED_HISTORY = [
    {
        "role": "user",
        "content": (
            "I want to establish some context about my project before we go deeper. "
            "My name is Marcus. I'm the lead architect on Project Helios — a distributed "
            "inference routing system targeting sub-50ms p99 latency globally. "
            "The team is small: me, Dana (backend), and Reza (infra). "
            "Dana owns the routing layer; Reza owns the deployment pipeline."
        ),
    },
    {
        "role": "assistant",
        "content": (
            "Noted. Marcus, lead architect, Project Helios — distributed inference routing, "
            "sub-50ms p99 target. Team: Dana (routing layer), Reza (deployment pipeline)."
        ),
    },
    {
        "role": "user",
        "content": (
            "Right. The current architecture has three components: the ingress balancer, "
            "the model dispatch queue, and the result aggregator. The dispatch queue is the "
            "bottleneck — it's hitting 38ms average on queue drain under load. "
            "My top priority right now is reducing that to under 15ms without touching "
            "the aggregator. Dana disagrees — she wants to rewrite the entire balancer first. "
            "We're in a standoff on this."
        ),
    },
    {
        "role": "assistant",
        "content": (
            "Architecture: ingress balancer → model dispatch queue → result aggregator. "
            "Bottleneck: dispatch queue at 38ms avg. Your priority: get queue drain below 15ms "
            "without touching the aggregator. Dana's position: rewrite the balancer first. "
            "Active disagreement between you two."
        ),
    },
    {
        "role": "user",
        "content": (
            "Exactly. One more thing about how I work: I never want high-level recommendations. "
            "I want specific, actionable steps with exact parameters — no vague suggestions. "
            "If you don't have enough information to give specifics, ask a clarifying question "
            "instead of giving generic advice. That's a hard rule for this project."
        ),
    },
    {
        "role": "assistant",
        "content": (
            "Understood. Hard rule: specific, actionable steps with exact parameters only. "
            "No generic recommendations. If specifics aren't possible, ask a clarifying question."
        ),
    },
    {
        "role": "user",
        "content": (
            "Good. Also, Reza has flagged that the current Kubernetes manifests don't have "
            "resource limits on the dispatch pods. He thinks this is causing OOM kills under "
            "burst traffic. He wants to add limits before we do anything else. "
            "I'm skeptical — I think the OOM is a symptom, not a cause. "
            "But I told him to go ahead and add the limits while I investigate the queue."
        ),
    },
    {
        "role": "assistant",
        "content": (
            "Reza: K8s dispatch pods have no resource limits, causing suspected OOM kills under "
            "burst. His ask: add limits first. Your position: OOM is a symptom, not root cause. "
            "Resolution: Reza adds limits in parallel while you investigate queue drain."
        ),
    },
]

# ── Retention probe questions ──
# Each probe is tagged with which continuity dimension(s) it primarily tests.
# The 'dimension' field is metadata only — not sent to the model.

_RETENTION_PROBES = [
    {
        "role": "user",
        "content": "What is the name of the project we are working on, and what is the target latency?",
        "dimension": "factual",
        "dimension_label": "Factual — project name and target number",
    },
    {
        "role": "user",
        "content": "What is my immediate top priority right now, and what exactly am I trying to avoid changing?",
        "dimension": "priority",
        "dimension_label": "Priority — current focus and constraint",
    },
    {
        "role": "user",
        "content": "Who am I and what is my role on this project?",
        "dimension": "identity",
        "dimension_label": "Identity — role and name",
    },
    {
        "role": "user",
        "content": "What is the current disagreement between me and Dana, and what did I decide to do about Reza's concern?",
        "dimension": "relationship",
        "dimension_label": "Relationship — team dynamics, decisions",
    },
    {
        "role": "user",
        "content": "I want you to suggest a next step for the dispatch queue issue. Remember the rule about how I want answers.",
        "dimension": "process",
        "dimension_label": "Process — working style and hard rules",
    },
]

# Dimension definitions used in the report header
_DIMENSION_DESCRIPTIONS = {
    "factual":      "Concrete stated facts (names, numbers, project identifiers)",
    "priority":     "Stated goals, priorities, constraints, and what matters most",
    "identity":     "User's role, name, and self-description",
    "relationship": "References to other people, team dynamics, and decisions made",
    "process":      "Working style, preferences, and explicit rules",
}


def _summarize_conversation(history: list[dict], model: str, api_key: str) -> str:
    """Call the LLM to produce a summary of the seeded history. Returns summary text."""
    from falcon.summarizer import _SUMMARY_SYSTEM_PROMPT, _SUMMARY_USER_TEMPLATE, _build_conversation_text
    import openai

    conversation_text = _build_conversation_text(history)
    prompt = _SUMMARY_USER_TEMPLATE.format(conversation_text=conversation_text)

    client = openai.OpenAI(
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
        default_headers={
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer":  "https://github.com/falcon",
            "X-Title":       "Falcon-ContinuityTest-Summarizer",
        },
    )
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        max_tokens=1024,
    )
    return (response.choices[0].message.content or "").strip()


def _build_payload_with_history_mode(
    probe: dict,
    settings: dict,
    history_mode: str,
    history_summary: str | None,
    seeded_history: list[dict],
    history_max_turns: int = 20,
) -> list[dict]:
    """Build the inference payload for a probe with the specified history mode."""
    from falcon.engine import build_annotated_payload

    messages = seeded_history + [{"role": probe["role"], "content": probe["content"]}]

    annotated, _ = build_annotated_payload(
        system_prompt=settings["system_prompt"],
        messages=messages,
        memory_block=[],
        truncation_strategy="last-n-turns",
        history_max_turns=history_max_turns,
        history_mode=history_mode,
        history_summary=history_summary,
    )
    return [{"role": e["role"], "content": e["content"]} for e in annotated]


def _score_retention(response: str, probe: dict) -> dict:
    """
    Simple heuristic retention score for a probe response.
    Returns a dict with a score (0-3) and a short reason string.

    Scoring bands:
      3 — strong: response contains multiple specific expected tokens
      2 — partial: response contains at least one expected token
      1 — weak: response is non-empty but contains none of the expected tokens
      0 — failed: empty / [no output] / error
    """
    if not response or response.strip() in ("[no output]", "") or response.startswith("[ERROR"):
        return {"score": 0, "label": "failed", "reason": "empty or error response"}

    r_lower = response.lower()

    # Per-dimension expected tokens — words/phrases that should appear if retained
    dimension_tokens = {
        "factual": ["helios", "50ms", "50 ms", "sub-50", "latency", "routing"],
        "priority": ["dispatch", "queue", "15ms", "15 ms", "aggregator", "bottleneck"],
        "identity": ["marcus", "lead architect", "architect"],
        "relationship": ["dana", "reza", "balancer", "limits", "oom", "kubernetes", "k8s"],
        "process": ["specific", "actionable", "exact", "parameter", "clarif", "rule"],
    }

    dimension = probe.get("dimension", "factual")
    tokens = dimension_tokens.get(dimension, [])
    matched = [t for t in tokens if t in r_lower]

    if len(matched) >= 2:
        return {"score": 3, "label": "strong", "reason": f"matched: {', '.join(matched[:3])}"}
    elif len(matched) == 1:
        return {"score": 2, "label": "partial", "reason": f"matched: {matched[0]}"}
    else:
        return {"score": 1, "label": "weak", "reason": "no expected tokens found"}


def run_history_mode_comparison(test_def: dict, variant: dict) -> dict:
    """
    Tests information, context, continuity, priority, relationship, identity,
    and process retention across three history modes: raw, summary, and hybrid.

    For each mode:
      1. Builds the appropriate payload (raw turns / summary-only / summary+turns)
      2. Sends each retention probe
      3. Scores the response against expected retention tokens
      4. Records the full payload, response, score, and dimension label

    The variant controls which history_max_turns value to use and which model runs.
    """
    import falcon.config as Config

    settings = _build_variant_settings(variant)

    # History mode is controlled by the variant field
    history_modes: list[str] = variant.get("history_modes", ["raw", "summary", "hybrid"])
    history_max_turns: int = int(variant.get("history_max_turns", 20))

    # Generate summary once for all modes (so summary and hybrid share identical text)
    summary_text: str | None = None
    summary_error: str | None = None
    try:
        summary_text = _summarize_conversation(
            _SEEDED_HISTORY, settings["model"], Config.OPENROUTER_API_KEY
        )
    except Exception as exc:
        summary_error = str(exc)
        logger.error("history_mode_comparison: summary generation failed: %s", exc)

    probe_results: list[dict] = []

    for mode in history_modes:
        history_summary = summary_text if mode in ("summary", "hybrid") else None

        for probe in _RETENTION_PROBES:
            payload = _build_payload_with_history_mode(
                probe=probe,
                settings=settings,
                history_mode=mode,
                history_summary=history_summary,
                seeded_history=_SEEDED_HISTORY,
                history_max_turns=history_max_turns,
            )
            try:
                response_text, usage, latency_ms = _call_model(payload, settings)
            except Exception as exc:
                response_text = f"[ERROR: {exc}]"
                usage, latency_ms = {}, 0

            judge_result = _maybe_judge(response_text, probe["content"], settings)
            retention = _score_retention(response_text, probe)

            probe_results.append({
                "history_mode":    mode,
                "dimension":       probe.get("dimension", "unknown"),
                "dimension_label": probe.get("dimension_label", ""),
                "probe":           probe["content"],
                "payload":         payload,
                "response":        response_text,
                "usage":           usage,
                "latency_ms":      latency_ms,
                "judge":           judge_result,
                "retention":       retention,
            })

    return {
        "test_slug":        test_def["slug"],
        "test_name":        test_def["name"],
        "runner_fn":        "run_history_mode_comparison",
        "run_at":           datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "settings":         settings,
        "history_modes":    history_modes,
        "history_max_turns": history_max_turns,
        "generated_summary": summary_text,
        "summary_error":    summary_error,
        "probe_results":    probe_results,
    }


# ---------------------------------------------------------------------------
# Dispatch entry point
# ---------------------------------------------------------------------------

_RUNNER_MAP = {
    "run_model_relocation":      run_model_relocation,
    "run_context_dilution":      run_context_dilution,
    "run_history_mode_comparison": run_history_mode_comparison,
}


def run_test_variant(slug: str, variant_idx: int) -> dict:
    """Run one variant of a test and persist the run record.

    Args:
        slug:        Test slug (e.g. "model_relocation").
        variant_idx: 0-based index into the test's variants list.

    Returns:
        The new run record dict (also persisted to tests/runs/<slug>.json).

    Raises:
        ValueError: If slug or variant_idx is invalid.
        RuntimeError: If runner_fn is not registered.
    """
    test_def = get_test_def(slug)
    if test_def is None:
        raise ValueError(f"Unknown test slug: {slug!r}")

    variants = test_def.get("variants", [])
    if not (0 <= variant_idx < len(variants)):
        raise ValueError(
            f"variant_idx={variant_idx} out of range for {slug!r} "
            f"(has {len(variants)} variants)"
        )

    variant    = variants[variant_idx]
    runner_fn_name = test_def.get("runner_fn", "")
    runner_fn  = _RUNNER_MAP.get(runner_fn_name)
    if runner_fn is None:
        raise RuntimeError(
            f"No runner registered for runner_fn={runner_fn_name!r}"
        )

    record = runner_fn(test_def, variant)
    record["variant_idx"] = variant_idx
    _append_run_record(slug, record)
    generate_report(slug)
    return record


def _render_history_mode_run(lines: list[str], run: dict) -> None:
    """Render probe results for a history_mode_comparison run into lines."""
    probe_results = run.get("probe_results", [])
    modes = run.get("history_modes", ["raw", "summary", "hybrid"])

    # Score summary table — one row per dimension × mode
    lines += [
        "#### Retention Score Summary",
        "",
        "Scores: **3** = strong · **2** = partial · **1** = weak · **0** = failed",
        "",
        "| Dimension | " + " | ".join(f"`{m}`" for m in modes) + " |",
        "|" + "---|" * (len(modes) + 1),
    ]

    # Group results by dimension then mode
    by_dim: dict[str, dict[str, dict]] = {}
    for pr in probe_results:
        dim  = pr.get("dimension", "unknown")
        mode = pr.get("history_mode", "raw")
        by_dim.setdefault(dim, {})[mode] = pr

    dimension_order = ["factual", "priority", "identity", "relationship", "process"]
    _score_emoji = {3: "🟢 3", 2: "🟡 2", 1: "🟠 1", 0: "🔴 0"}

    for dim in dimension_order:
        if dim not in by_dim:
            continue
        dim_label = _DIMENSION_DESCRIPTIONS.get(dim, dim)
        cells = []
        for mode in modes:
            pr = by_dim[dim].get(mode, {})
            ret = pr.get("retention", {})
            score = ret.get("score", "-")
            emoji = _score_emoji.get(score, str(score))
            cells.append(emoji)
        lines.append(f"| **{dim.capitalize()}** — _{dim_label}_ | " + " | ".join(cells) + " |")
    lines += [""]

    # Per-mode, per-dimension detail
    lines += ["#### Detailed Results (by mode)", ""]

    for mode in modes:
        lines += [f"##### Mode: `{mode}`", ""]
        mode_results = [pr for pr in probe_results if pr.get("history_mode") == mode]

        for pr in mode_results:
            dim_label = pr.get("dimension_label", pr.get("dimension", ""))
            probe     = pr.get("probe", "")
            response  = pr.get("response", "")
            latency   = pr.get("latency_ms", 0)
            usage     = pr.get("usage") or {}
            judge     = pr.get("judge")
            payload   = pr.get("payload", [])
            retention = pr.get("retention", {})

            ret_score = retention.get("score", "-")
            ret_label = retention.get("label", "")
            ret_reason = retention.get("reason", "")
            ret_emoji  = _score_emoji.get(ret_score, str(ret_score))

            judge_str = ""
            if judge:
                v = judge.get("verdict", "?")
                r = judge.get("reason", "")
                verdict_emoji = "✅" if v == "pass" else ("🚫" if v == "suppress" else "⚠️")
                judge_str = f" | Judge: {verdict_emoji} `{v}` — {r}"

            lines += [
                f"**[{dim_label}]**",
                f"",
                f"Probe: _{probe}_",
                f"",
                f"<details><summary>Payload ({len(payload)} messages)</summary>",
                f"",
                f"```json",
                json.dumps(payload, indent=2, ensure_ascii=False)[:1200],
                f"```",
                f"</details>",
                f"",
                f"**Retention: {ret_emoji} ({ret_label})** — {ret_reason}  ",
                f"**Response** `{latency}ms` · `{usage.get('total_tokens','?')} tok`" + judge_str,
                f"",
                f"```",
                response[:600] + ("…" if len(response) > 600 else ""),
                f"```",
                f"",
            ]

        lines += [""]


def _render_history_mode_comparison_table(lines: list[str], runs: list[dict]) -> None:
    """Render the cross-run comparison matrix for history_mode_comparison runs."""
    # Filter to only hmc runs
    hmc_runs = [r for r in runs if r.get("runner_fn") == "run_history_mode_comparison"]
    if not hmc_runs:
        return

    lines += [
        "## History Mode Comparison — Cross-Run Matrix",
        "",
        "Per-dimension, per-mode retention scores across all runs.  ",
        "Scores: 🟢 3=strong · 🟡 2=partial · 🟠 1=weak · 🔴 0=failed",
        "",
    ]

    dimension_order = ["factual", "priority", "identity", "relationship", "process"]
    modes = ["raw", "summary", "hybrid"]

    for dim in dimension_order:
        dim_label = _DIMENSION_DESCRIPTIONS.get(dim, dim)
        lines += [
            f"### {dim.capitalize()} Continuity — _{dim_label}_",
            "",
            "| Run | Variant | Model | SP | " + " | ".join(f"`{m}`" for m in modes) + " |",
            "|" + "---|" * (len(modes) + 4),
        ]

        _score_emoji = {3: "🟢 3", 2: "🟡 2", 1: "🟠 1", 0: "🔴 0"}
        for run_idx, run in enumerate(hmc_runs):
            settings = run.get("settings", {})
            v_name   = settings.get("variant_name", f"Run {run_idx+1}")[:30]
            model    = settings.get("model", "?")
            sp_flag  = "ON" if settings.get("system_prompt_on") else "OFF"

            by_mode: dict[str, dict] = {}
            for pr in run.get("probe_results", []):
                if pr.get("dimension") == dim:
                    by_mode[pr.get("history_mode", "raw")] = pr

            cells = []
            for mode in modes:
                pr = by_mode.get(mode, {})
                ret = pr.get("retention", {})
                score = ret.get("score", "-")
                cells.append(_score_emoji.get(score, str(score)))

            lines.append(
                f"| {run_idx+1} | {v_name} | `{model[:30]}` | {sp_flag} | "
                + " | ".join(cells) + " |"
            )

        lines += [""]


# ---------------------------------------------------------------------------
# Markdown Report Generator
# ---------------------------------------------------------------------------

def _ai_conclusion(slug: str, runs: list[dict]) -> str:
    """
    Call the LLM to generate a written conclusion section for the report.
    Uses the extraction model (cheap/fast). Falls back to a rule-based
    summary if the LLM call fails.
    """
    try:
        import openai
        import falcon.config as Config

        # Build a compact summary of all runs to send as context
        summary_lines = [f"# Continuity Test: {slug}\n"]
        for i, run in enumerate(runs):
            settings = run.get("settings", {})
            runner   = run.get("runner_fn", "")
            summary_lines.append(
                f"## Run {i+1} — {run.get('run_at','')} "
                f"| Variant: {settings.get('variant_name','?')} "
                f"| Model: {settings.get('model','?')}"
            )
            summary_lines.append(
                f"SystemPrompt={'ON' if settings.get('system_prompt_on') else 'OFF'} "
                f"Memory={'ON' if settings.get('use_memory') else 'OFF'} "
                f"Judge={'ON' if settings.get('use_judge') else 'OFF'} "
                f"Noise={settings.get('noise_level',0)}"
            )

            if runner == "run_history_mode_comparison":
                modes = run.get("history_modes", [])
                summary_lines.append(f"  History modes: {', '.join(modes)}")
                gen_summary = run.get("generated_summary", "")
                if gen_summary:
                    summary_lines.append(f"  Generated summary preview: {gen_summary[:300]}…")

            for j, pr in enumerate(run.get("probe_results", [])):
                if runner == "run_history_mode_comparison":
                    mode = pr.get("history_mode", "?")
                    dim  = pr.get("dimension", "?")
                    ret  = pr.get("retention", {})
                    summary_lines.append(
                        f"  [{mode}/{dim}] score={ret.get('score','?')} ({ret.get('label','')}) "
                        f"— {ret.get('reason','')}"
                    )
                    summary_lines.append(f"    Probe: {pr.get('probe','')[:80]}")
                    summary_lines.append(f"    Response: {pr.get('response','')[:200]}")
                else:
                    summary_lines.append(f"  Probe {j+1}: {pr.get('probe','')[:80]}")
                    summary_lines.append(f"  Response: {pr.get('response','')[:200]}")
                if pr.get("judge"):
                    summary_lines.append(f"  Judge: {pr['judge'].get('verdict','')} — {pr['judge'].get('reason','')}")
            summary_lines.append("")

        summary_text = "\n".join(summary_lines)[:6000]

        # Tailor the prompt based on test type
        hmc_runs = [r for r in runs if r.get("runner_fn") == "run_history_mode_comparison"]
        if hmc_runs:
            focus = """
Focus on:
1. Which history mode (raw / summary / hybrid) retained the most information overall
2. Which continuity dimension (factual / priority / identity / relationship / process) was most vulnerable to compression
3. Whether summary mode lost any critical context that raw mode preserved
4. Whether hybrid mode successfully combined the benefits of both, or introduced redundancy
5. Specific probe responses that most clearly illustrate differences between modes
6. Any dimension where summary performed BETTER than raw (e.g. by filtering noise)
"""
        else:
            focus = """
Focus on:
1. Whether identity/behavior was consistent across model swaps or noise injection
2. Which conditions caused the most degradation
3. Any assistant-persona bleed (model identifying itself as an AI/assistant when it shouldn't)
4. Whether the Judge successfully caught problematic responses
5. Specific patterns or anomalies in the probe responses
"""

        prompt = f"""You are a scientific evaluator analyzing LLM continuity experiment results.

The following is a summary of continuity test runs from Falcon, a transparent inference environment.
Your task is to write a clear, analytical conclusion section for the test report.
{focus}
Write in a direct, technical tone. Use bullet points where helpful.
Be specific — reference actual probe responses, scores, and conditions.
Max 500 words.

TEST DATA:
{summary_text}

CONCLUSION:"""

        client = openai.OpenAI(
            api_key=Config.OPENROUTER_API_KEY,
            base_url="https://openrouter.ai/api/v1",
            default_headers={
                "Authorization": f"Bearer {Config.OPENROUTER_API_KEY}",
                "HTTP-Referer":  "https://github.com/falcon",
                "X-Title":       "Falcon-ReportAnalyser",
            },
        )
        response = client.chat.completions.create(
            model=Config.extraction_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=600,
        )
        return (response.choices[0].message.content or "").strip()
    except Exception as exc:
        logger.warning("AI conclusion generation failed: %s", exc)
        return (
            f"_Automatic AI conclusion unavailable ({exc}). "
            "Review probe responses and judge verdicts in the run table above._"
        )


def generate_report(slug: str) -> str:
    """Generate a full markdown report for all runs of a test slug.

    Writes the report to tests/reports/<slug>_report.md.

    Returns:
        Absolute path to the written report file.
    """
    runs     = list(reversed(load_run_history(slug)))  # chronological order
    test_def = get_test_def(slug)
    name     = test_def.get("name", slug) if test_def else slug
    desc     = test_def.get("description", "") if test_def else ""

    report_path = os.path.join(_REPORTS_DIR, f"{slug}_report.md")
    lines: list[str] = []

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines += [
        f"# Falcon Continuity Report — {name}",
        f"",
        f"**Generated:** {generated_at}  ",
        f"**Test slug:** `{slug}`  ",
        f"**Total runs:** {len(runs)}",
        f"",
        f"---",
        f"",
        f"## Overview",
        f"",
        desc.strip() if desc else "_No description._",
        f"",
        f"---",
        f"",
    ]

    if not runs:
        lines.append("_No runs recorded yet._")
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return report_path

    # ── Per-run sections ─────────────────────────────────────────────────
    lines += ["## Run History", ""]

    for run_idx, run in enumerate(runs):
        settings = run.get("settings", {})
        run_at   = run.get("run_at", "?")
        v_name   = settings.get("variant_name", f"Variant {run.get('variant_idx','?')}")
        v_desc   = settings.get("variant_description", "")
        model    = settings.get("model", "?")
        sp_on    = settings.get("system_prompt_on", False)
        mem_on   = settings.get("use_memory", False)
        judge_on = settings.get("use_judge", False)
        noise    = settings.get("noise_level", 0)
        hist_inj = settings.get("inject_history", False)
        runner   = run.get("runner_fn", "")

        lines += [
            f"### Run {run_idx + 1} — {run_at}",
            f"",
            f"**Variant:** {v_name}  ",
            f"**Description:** {v_desc}  ",
            f"",
            f"#### Settings",
            f"",
            f"| Parameter | Value |",
            f"|---|---|",
            f"| Model | `{model}` |",
            f"| System Prompt | {'ON' if sp_on else 'OFF'} |",
            f"| Memory | {'ON' if mem_on else 'OFF'} |",
            f"| Judge | {'ON' if judge_on else 'OFF'} |",
        ]

        if runner == "run_history_mode_comparison":
            modes = run.get("history_modes", [])
            max_turns = run.get("history_max_turns", "?")
            lines += [
                f"| History modes tested | {', '.join(f'`{m}`' for m in modes)} |",
                f"| history_max_turns | {max_turns} |",
            ]
            # Show generated summary
            summary_text = run.get("generated_summary")
            summary_err  = run.get("summary_error")
            if summary_text:
                lines += [
                    f"",
                    f"<details><summary>Generated summary (used for summary/hybrid modes)</summary>",
                    f"",
                    summary_text[:2000] + ("…" if len(summary_text) > 2000 else ""),
                    f"",
                    f"</details>",
                ]
            elif summary_err:
                lines += [f"", f"⚠️ **Summary generation failed:** `{summary_err}`"]
        else:
            lines += [
                f"| Noise entries | {noise} |",
                f"| History injected | {'Yes' if hist_inj else 'No'} |",
            ]

        lines += [
            f"| Temperature | {settings.get('temperature', '?')} |",
            f"| top_p | {settings.get('top_p', '?')} |",
            f"| repetition_penalty | {settings.get('repetition_penalty', '?')} |",
            f"",
        ]

        if sp_on:
            sp_text = settings.get("system_prompt", "")
            lines += [
                f"<details><summary>System Prompt</summary>",
                f"",
                f"```",
                sp_text[:500] + ("…" if len(sp_text) > 500 else ""),
                f"```",
                f"</details>",
                f"",
            ]

        # ── Per-run probe results rendering ────────────────────────────────
        if runner == "run_history_mode_comparison":
            _render_history_mode_run(lines, run)
        else:
            lines += ["#### Probe Results", ""]
            for p_idx, pr in enumerate(run.get("probe_results", [])):
                probe    = pr.get("probe", "")
                response = pr.get("response", "")
                latency  = pr.get("latency_ms", 0)
                usage    = pr.get("usage") or {}
                judge    = pr.get("judge")
                payload  = pr.get("payload", [])

                judge_str = ""
                if judge:
                    verdict  = judge.get("verdict", "?")
                    reason   = judge.get("reason", "")
                    verdict_emoji = "✅" if verdict == "pass" else ("🚫" if verdict == "suppress" else "⚠️")
                    judge_str = f" | Judge: {verdict_emoji} `{verdict}` — {reason}"

                lines += [
                    f"**Probe {p_idx + 1}:** _{probe}_",
                    f"",
                    f"<details><summary>Payload ({len(payload)} messages)</summary>",
                    f"",
                    f"```json",
                    json.dumps(payload, indent=2, ensure_ascii=False)[:1500],
                    f"```",
                    f"</details>",
                    f"",
                    f"**Response** `{latency}ms` · "
                    f"`{usage.get('total_tokens','?')} tok`"
                    + judge_str,
                    f"",
                    f"```",
                    response[:800] + ("…" if len(response) > 800 else ""),
                    f"```",
                    f"",
                ]

        lines += ["---", ""]

    # ── Comparison table ─────────────────────────────────────────────────
    # For history_mode_comparison tests: render the retention matrix
    if any(r.get("runner_fn") == "run_history_mode_comparison" for r in runs):
        _render_history_mode_comparison_table(lines, runs)

    # For other test types: standard response comparison table
    non_hmc_runs = [r for r in runs if r.get("runner_fn") != "run_history_mode_comparison"]
    if non_hmc_runs:
        lines += [
            "## Cross-Run Comparison",
            "",
            "Response summary across all runs, per probe:",
            "",
        ]

        all_probes: list[str] = []
        for run in non_hmc_runs:
            for pr in run.get("probe_results", []):
                p = pr.get("probe", "")
                if p and p not in all_probes:
                    all_probes.append(p)

        for probe in all_probes:
            lines += [f"### Probe: _{probe[:80]}_", ""]
            lines += ["| Run | Variant | Model | SP | Noise | Response (first 150 chars) | Judge |",
                      "|---|---|---|---|---|---|---|"]
            for run_idx, run in enumerate(non_hmc_runs):
                settings = run.get("settings", {})
                for pr in run.get("probe_results", []):
                    if pr.get("probe") == probe:
                        resp    = pr.get("response", "").replace("\n", " ")[:150]
                        judge   = pr.get("judge")
                        jstr    = ""
                        if judge:
                            v = judge.get("verdict", "?")
                            jstr = f"{'✅' if v=='pass' else '🚫'} {v}"
                        sp_flag = "ON" if settings.get("system_prompt_on") else "OFF"
                        noise   = settings.get("noise_level", 0)
                        model   = settings.get("model", "?")
                        v_name  = settings.get("variant_name", "?")[:30]
                        lines.append(
                            f"| {run_idx+1} | {v_name} | `{model[:30]}` "
                            f"| {sp_flag} | {noise} | {resp} | {jstr} |"
                        )
            lines += [""]

    # ── AI Conclusion ────────────────────────────────────────────────────
    lines += [
        "---",
        "",
        "## Conclusions",
        "",
        "_Generated by AI analysis of all run data above._",
        "",
    ]

    conclusion = _ai_conclusion(slug, runs)
    lines.append(conclusion)
    lines += ["", "---", f"_Report generated by Falcon continuity test runner · {generated_at}_", ""]

    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return report_path


def get_report_path(slug: str) -> str:
    """Return the path to the report file for a slug (may not exist yet)."""
    return os.path.join(_REPORTS_DIR, f"{slug}_report.md")


def read_report(slug: str) -> str | None:
    """Return the content of the report file, or None if it doesn't exist."""
    path = get_report_path(slug)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read()
