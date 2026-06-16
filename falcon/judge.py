"""
judge.py — Response Judge for Falcon.

Evaluates a completed model response and decides whether it should be
shown or suppressed. The judge NEVER rewrites or improves the response.
It only returns a binary verdict: pass | suppress.

Suppression criteria — output is suppressed when it reads as generic
AI-assistant chatter rather than meaningful communication:
  - Filler acknowledgements ("Sure!", "Of course!", "Certainly!")
  - AI self-identification ("I am an AI", "As a language model…")
  - Refusals dressed in assistant-speak ("I'm sorry, I can't help…")
  - Empty politeness with no informational content
  - Offers to help / asks what the user needs when nothing was asked
  - Any response whose only content is assistant-persona performance

The judge is called synchronously after the generator finishes but
before the response is committed to history or shown permanently.

Public API:
    evaluate(response_text, user_input, model, api_key) -> JudgeResult
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import openai

logger = logging.getLogger(__name__)

_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

_JUDGE_SYSTEM_PROMPT_FALLBACK = """\
You are a signal/noise classifier for a minimalist inference channel.

Your only job: decide if a response contains meaningful communication or is generic AI-assistant chatter.

SUPPRESS responses that are ONLY:
- Filler acknowledgements with no information ("Sure!", "Of course!", "Certainly!", "Great question!")
- AI self-identification ("I am an AI", "As a language model", "As an AI assistant")
- Pure offers to help without doing anything ("How can I assist you?", "What would you like to know?")
- Apologies and refusals wrapped in assistant-speak ("I'm sorry, I can't help with that", "I apologize, but…")
- Responses whose entire substance is performing an assistant persona with zero informational content

PASS responses that contain:
- Any factual answer, explanation, or direct reply to the user's question
- Code, formulas, lists, structured data
- A refusal stated plainly and briefly without assistant-persona wrapping
- Analysis, opinion, or reasoning — even if short
- A direct continuation of the conversation

RULES:
- You must output ONLY a single JSON object, nothing else.
- Format: {"verdict": "pass", "reason": "<one short sentence>"}
  or      {"verdict": "suppress", "reason": "<one short sentence>"}
- verdict must be exactly "pass" or "suppress" — no other values.
- reason must be a single sentence, maximum 120 characters.
- Do NOT rewrite, improve, or quote the response.
- If the response contains ANY meaningful content beyond chatter, verdict is "pass".\
"""

_JUDGE_USER_TEMPLATE = """\
USER INPUT:
{user_input}

RESPONSE TO EVALUATE:
{response_text}

Classify this response as pass or suppress. Output only JSON.\
"""


@dataclass
class JudgeResult:
    """Result of a judge evaluation."""
    verdict:    str    # "pass" | "suppress"
    reason:     str    # one-sentence explanation
    latency_ms: int    # wall-clock time for the judge call
    model:      str    # judge model used
    raw:        str    # raw judge output (for trace log)
    error:      str    # non-empty if the judge call failed


def evaluate(
    response_text: str,
    user_input: str,
    model: str,
    api_key: str,
    system_prompt: str = "",
) -> JudgeResult:
    """Call the judge model and return a pass/suppress verdict.

    The response is never modified. If the judge call fails for any reason,
    the verdict defaults to "pass" so generation is never silently dropped
    due to judge infrastructure failure.

    Args:
        response_text: The raw generator output to evaluate.
        user_input:    The user message that triggered the generation.
        model:         Judge model name (OpenRouter identifier).
        api_key:       OpenRouter API key.
        system_prompt: System prompt for the judge. If empty, falls back to
                       the hardcoded default (_JUDGE_SYSTEM_PROMPT_FALLBACK).

    Returns:
        JudgeResult with verdict, reason, latency, model, raw output, and
        any error string (empty on success).
    """
    t0 = time.monotonic()

    # Use config-supplied prompt if provided, else built-in fallback.
    effective_system_prompt = system_prompt.strip() if system_prompt and system_prompt.strip() \
        else _JUDGE_SYSTEM_PROMPT_FALLBACK

    if not response_text or not response_text.strip():
        return JudgeResult(
            verdict="suppress",
            reason="Empty response — nothing to show.",
            latency_ms=0,
            model=model,
            raw="",
            error="",
        )

    user_message = _JUDGE_USER_TEMPLATE.format(
        user_input=user_input[:1000],
        response_text=response_text[:3000],
    )

    try:
        client = openai.OpenAI(
            api_key=api_key,
            base_url=_OPENROUTER_BASE_URL,
            default_headers={
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer":  "https://github.com/falcon",
                "X-Title":       "Falcon-Judge",
            },
        )
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": effective_system_prompt},
                {"role": "user",   "content": user_message},
            ],
            temperature=0.0,
            max_tokens=128,
        )
        raw = (response.choices[0].message.content or "").strip()
    except Exception as exc:
        latency_ms = round((time.monotonic() - t0) * 1000)
        logger.error("judge: API call failed for model=%r: %s", model, exc)
        return JudgeResult(
            verdict="pass",
            reason="Judge call failed — defaulting to pass.",
            latency_ms=latency_ms,
            model=model,
            raw="",
            error=str(exc),
        )

    latency_ms = round((time.monotonic() - t0) * 1000)

    # Parse JSON
    try:
        import json
        # Strip markdown fences if present
        text = raw
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
        parsed = json.loads(text)
        verdict = str(parsed.get("verdict", "pass")).strip().lower()
        reason  = str(parsed.get("reason", "")).strip()[:200]
        if verdict not in ("pass", "suppress"):
            verdict = "pass"
            reason  = f"Unexpected verdict value — defaulting to pass. Raw: {raw[:80]}"
    except Exception as exc:
        logger.error("judge: JSON parse failed: %s — raw=%r", exc, raw[:200])
        return JudgeResult(
            verdict="pass",
            reason=f"Judge output unparseable — defaulting to pass.",
            latency_ms=latency_ms,
            model=model,
            raw=raw,
            error=str(exc),
        )

    return JudgeResult(
        verdict=verdict,
        reason=reason,
        latency_ms=latency_ms,
        model=model,
        raw=raw,
        error="",
    )
