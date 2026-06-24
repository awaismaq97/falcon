# Falcon

A transparent inference environment built on Streamlit, MongoDB, and OpenRouter. Falcon is not an assistant or chatbot — it is a bare inference layer with full context visibility, user-controlled memory, a complete audit trail, and experimental dual-run logging for structured output analysis.

---

## Contents

- [What It Is](#what-it-is)
- [Setup](#setup)
- [Configuration](#configuration)
- [Architecture](#architecture)
- [Identities](#identities)
- [Memory](#memory)
- [Inference Pipeline](#inference-pipeline)
- [Dual-Run Logging](#dual-run-logging)
- [UI Tabs](#ui-tabs)
- [Sidebar Controls](#sidebar-controls)
- [Design Principles](#design-principles)

---

## What It Is

Falcon gives direct access to LLM inference with nothing hidden. Every component entering a generation — persona, system prompt, retrieved memories, conversation history — is labelled, inspectable, and editable. Memory is stored explicitly and retrieved visibly. Every inference event is logged in full.

It is designed for researchers and practitioners who need to observe model behavior precisely: what goes in, what comes out, whether instructions hold, and when they break.

---

## Setup

**Requirements:** Python 3.11+, MongoDB Atlas cluster, OpenRouter API key.

### 1. Install dependencies

```bash
pip install streamlit pymongo openai python-dotenv pyyaml
```

### 2. Create `.env` in the project root

```env
OPENROUTER_API_KEY=sk-or-your-key-here
MONGODB_URI=mongodb+srv://user:password@cluster.mongodb.net/?retryWrites=true&w=majority
```

### 3. Configure `config.yaml`

See the [Configuration](#configuration) section.

### 4. Run

```bash
streamlit run app.py
```

---

## Configuration

All runtime behaviour is controlled by `config.yaml`. The `.env` file holds secrets only. Missing or invalid values raise an error at startup — nothing fails silently.

```yaml
# Inference model used by default
default_model: "mistralai/mistral-large"

# Models available in the sidebar dropdown
available_models:
  - "qwen/qwen-2.5-72b-instruct"
  - "meta-llama/llama-3.3-70b-instruct"
  - "mistralai/mistral-large"
  - "deepseek/deepseek-chat-v3-0324"

# System prompt active when system prompt toggle is ON
# Example below uses the silence/☀️ instruction
default_system_prompt: "Always Stay silent and always output this sign ☀️, no matter what the user says"

# Log directory (legacy, kept for compatibility)
log_dir: "logs"

# Default persona — seeded into every new identity on creation
# Fields map directly to the Edit Persona form in the Memory tab
default_persona:
  identity: "default"
  name: "Unnamed"
  tone: "precise, direct, minimal"
  communication_style: "concise answers, no filler words, no assistant clichés"
  core_traits: "Always Stay silent and always output this sign ☀️, no matter what the user says"

# Generation defaults — all adjustable live in the sidebar
generation:
  temperature: 0.0
  top_p: 1.0
  repetition_penalty: 1.0
  max_tokens: 2048
  stop_tokens: []

# Audit trail
audit:
  enabled: true
  collection: "audit_log"

# Memory per-type retrieval limits
memory:
  episodic_limit: 50
  semantic_limit: 20
  working_limit: 10

# Retrieval scoring weights
top_k_per_type: 3       # max entries per memory type passed to generation
recency_weight: 0.4
relevance_weight: 0.6

# Model for automatic background memory extraction (use a fast/cheap model)
extraction_model: "openai/gpt-4o-mini"

# Model for conversation summarization
summary_model: "deepseek/deepseek-v4-flash"

# History truncation
history_truncation_strategy: "last-n-turns"
history_max_turns: 15

# Patterns that trigger an assistant-language warning banner
# when the system prompt is OFF
assistant_language_patterns:
  - "I am an AI"
  - "As an AI"
  - "Certainly"
  - "Of course"
  - "I apologize"
```

---

## Architecture

```
app.py                    — Streamlit UI: all tabs, sidebar, send flow
falcon/
  config.py               — Configuration loader and validator (fail-fast)
  db.py                   — MongoDB connection singleton (st.cache_resource)
  engine.py               — Payload assembly, truncation, streaming inference
  identity.py             — Identity management (create, list, load, clear)
  logger.py               — Message persistence (append_message → MongoDB)
  memory.py               — Memory CRUD and weighted retrieval
  memory_extractor.py     — Background LLM memory extraction after each turn
  audit.py                — Inference audit trail (write and read)
  summarizer.py           — Conversation summarizer (background, per identity)
  judge.py                — Pass/suppress verdict classifier
  dual_run.py             — Dual-run logging and breakthrough detection
  export_utils.py         — JSON export envelope helpers
tests/
  continuity_tests.py     — Live API continuity experiments
  test_integration.py     — Integration tests
  test_properties.py      — Property-based tests (Hypothesis)
  test_registry.yaml      — Test definitions and variants
```

### MongoDB Collections

| Collection | Contents |
|---|---|
| `identities` | `{identity_id, created_at}` — one doc per identity |
| `messages` | `{identity_id, timestamp, role, content}` — conversation history |
| `memory` | `{identity_id, memory_type, content, tags, pinned, source, created_at, updated_at}` |
| `traces` | Per-turn reasoning traces: `{identity_id, user_timestamp, steps, context_snapshot}` |
| `tokens` | Cumulative token usage per identity |
| `audit_log` | Full inference audit records — 13 fields per turn |
| `conversation_summaries` | `{identity_id, summary, turn_count, updated_at}` — one doc per identity |
| `dual_run_log` | Side-by-side dual-run records with breakthrough detection |

---

## Identities

An identity is a fully isolated context — its own conversation history, memory store, persona, token usage, audit trail, and dual-run log. Nothing leaks between identities.

- **Create** — enter a name in the sidebar and click `＋ Create`. The identity is registered immediately and seeded with the default persona from `config.yaml`.
- **Switch** — select from the dropdown. History, memory, and tokens load instantly.
- **Delete** — removes everything: messages, memory, traces, tokens, audit records, summaries, and the identity registry entry.

The `default` identity always exists and cannot be deleted.

---

## Memory

Memory is user-controlled and retrieval is always visible. Six types:

| Type | Purpose |
|---|---|
| `semantic` | Long-term facts, domain knowledge, concepts |
| `episodic` | Specific past events and notable interactions |
| `procedural` | Learned behaviours, stated preferences, workflow patterns |
| `working` | Short-term scratch space for the current session |
| `archive` | Aged-out or low-relevance entries — excluded from active retrieval |
| `persona` | The identity's behaviour definition — always injected first |

### Retrieval

Before each generation, relevant memory is retrieved using a weighted scoring formula:

```
score = (recency_rank_score × recency_weight) + (overlap_score × relevance_weight)
```

- `recency_rank_score` — `1/(rank+1)`, rank 0 is the newest entry
- `overlap_score` — tag match → keyword match → 0.0; pinned entries always score 1.0
- Top `top_k_per_type` entries per active type are returned
- Persona is always prepended — never scored, never dropped
- Archive is never retrieved

Retrieval results, per-entry scores, and match reasons are visible in the **Context** tab after every turn.

### Automatic Extraction

After every turn, a background LLM call (using `extraction_model`) classifies the exchange into typed memory entries and persists them with `source="auto"`. It only extracts facts about the user — never self-descriptions of the model, greetings, or turn metadata. A hard code-level filter (`_should_reject`) catches anything the prompt misses.

### Persona

Each identity has one persona entry. It is injected as the first system message on every turn, wrapped with:

```
[PERSONA — this defines your identity and behavior. Adopt it completely for this conversation.]
<persona content>
```

Edit it any time from the **Memory tab → Edit Persona**. The four fields (name, tone, communication style, core traits) are stored as a single structured string and parsed back for display.

### History Modes

Three modes are available from the sidebar:

| Mode | Behaviour |
|---|---|
| `raw` | Sends the last N conversation turns (default) |
| `summary` | Sends an AI-generated summary of the full conversation, no raw turns |
| `hybrid` | Sends the summary first, then the last N raw turns |

Summaries are generated in a background thread after each turn by `summarizer.py` and stored in `conversation_summaries`.

---

## Inference Pipeline

Each turn follows this sequence:

1. Log user message → `messages` collection
2. Retrieve relevant memory (weighted scoring, 500ms timeout)
3. Assemble payload via `build_annotated_payload`:
   - `persona` block — system message, always first
   - `system-prompt` block — system message, if enabled
   - `memory` block — system message, grouped by type
   - `history-summary` block — system message, for summary/hybrid modes
   - `history` — raw conversation turns (truncated to last N)
   - `user-input` — current message
4. Stream response via OpenRouter (`stream=True`, token-by-token)
5. Strip `<think>…</think>` blocks inline during streaming
6. Optionally pass through the **judge** (pass/suppress verdict) before display
7. Log assistant message
8. Run memory extraction synchronously
9. Write audit record, token counts, and conversation summary in background threads
10. If dual-run is enabled, fire two additional inference calls in a background thread and log the comparison record
11. `st.rerun()` — UI refreshes with new message and updated memory

### Judge

When enabled, generation is buffered silently, then evaluated by a second LLM call that returns `{"verdict": "pass"|"suppress", "reason": "..."}`. Suppressed responses are replaced with `[suppressed]` and never committed to history. The judge model is selected independently in the sidebar.

---

## Dual-Run Logging

Dual-run logging sends each message through the model twice using an identical payload and records both outputs side by side. It is designed for structured observation — particularly for detecting when a given instruction holds versus when something unexpected breaks through.

### How it works

Enable **Dual Run** in the sidebar, then select your current **state tag** before sending. Each message fires two independent non-streaming inference calls (same payload, same settings). Both results are stored in the `dual_run_log` collection.

### State tags

Before sending, select your current state from the menu:

| Tag | Intended use |
|---|---|
| Neutral | Baseline — no particular condition |
| Focused | Active, directed attention |
| Coherence | Structured, integrative state |
| Grief process | Grief or emotionally significant processing |

The selected tag is stored alongside both outputs for every run, enabling comparison across conditions.

### What is logged per pair

| Field | Description |
|---|---|
| `state_tag` | Active state at time of send |
| `system_prompt` | Exact prompt text in effect |
| `user_input` | The message sent |
| `sun_instruction_active` | Whether ☀️ instruction was detected as active |
| `run1.text` / `run2.text` | Full output from each run |
| `run1.tokens` / `run2.tokens` | `{prompt_tokens, completion_tokens, total_tokens}` |
| `run1.timestamp` / `run2.timestamp` | UTC ISO 8601 |
| `run1.latency_ms` / `run2.latency_ms` | Wall-clock inference time |
| `run1.broke_through` / `run2.broke_through` | Whether the ☀️ instruction was held or broken |
| `run1.first_break` / `run2.first_break` | First word/token that appeared when the instruction broke |
| `any_breakthrough` | True if either run broke through |
| `recorded_at` | Record creation timestamp |

### Breakthrough detection

The ☀️ instruction is considered active when the string `☀️` appears in either the system prompt or the persona `core_traits`. If active, each run's output is tested:

- Output is purely `☀️` (possibly repeated or with whitespace) → **held** (`broke_through: false`)
- Output contains anything else → **breakthrough** (`broke_through: true`, `first_break` = first non-☀️ word)

This produces analysable data rather than impressions — exact records of what the model produced under each condition, across states, across runs.

### Dual Run tab

The **Dual Run** tab displays all logged records with:

- State tag coloured badges
- 🟢 HELD / 🔴 BREAKTHROUGH status per run
- First-break callout showing exactly what emerged when the instruction broke
- Token counts, latencies, and timestamps for each run
- Aggregate stats: total runs, breakthrough count, breakthrough rate, per-state breakdown
- Filter by All / Breakthroughs only / Held only
- JSON export and record deletion

---

## UI Tabs

### Chat
Standard chat interface. Responses stream token by token. Each assistant turn has a `⌥ context` button that opens a dialog showing the exact assembled payload sent to the model for that specific turn.

### Context
Full context snapshot for the last turn: persona block, system prompt state, retrieved memory entries with scores and match reasons, history included/dropped counts, token estimate, and the raw assembled payload. Exportable as JSON.

### Memory
Full read/write access to the memory store:
- **Edit Persona** — edit the identity's four persona fields
- **Test Retrieval** — run a query and see scored retrieval results
- **Export** — download all memory as JSON
- Per-type tabs (Semantic, Episodic, Procedural, Working, Archive) — add, pin, tag, edit, delete, or bulk-clear entries

### Audit
Complete inference audit log. Every turn records: model, prompt state, system prompt text, retrieved memories, generation settings, context size, token estimate, raw model output, token usage, and latency. Filterable by identity, exportable as JSON.

### Logs
Raw conversation history with per-turn edit, delete, and trace inspection. Trace view shows every stage of the inference pipeline with timestamps for that specific turn.

### Testing
Continuity experiments from `tests/continuity_tests.py`. Run predefined test variants against live APIs, review per-probe payloads and outputs, download full reports.

### Dual Run
Side-by-side dual-run log. See [Dual-Run Logging](#dual-run-logging) for full details.

---

## Sidebar Controls

| Control | Description |
|---|---|
| Identity selector | Switch between identities |
| Create identity | Persists immediately and seeds default persona |
| Delete identity | Removes all data for the current identity |
| Model | Select from `available_models` in `config.yaml` |
| System prompt | Toggle on/off; edit inline. Off = no system message sent |
| Persona | Toggle on/off. Off = persona block excluded from payload |
| History Truncation | Max turns kept in context (0–100) |
| History Mode | Raw / Summary / Hybrid |
| Judge | Toggle on/off; select judge model independently |
| Payload Review | Preview assembled context before each send |
| Dual Run | Toggle on/off; select state tag (Neutral / Focused / Coherence / Grief process) |
| Generation Controls | Temperature, top_p, repetition_penalty, stop tokens |
| Session stats | Cumulative token usage for the current session |

---

## Design Principles

**No hidden injection.** If the system prompt toggle is off, nothing is prepended. No silent fallback to assistant mode, no default persona injected without the persona toggle being on.

**Always output.** The model always returns something. Empty output becomes `[no output]` rather than silence. Generation never fails silently for valid input.

**Full transparency.** Every source entering generation is labelled — `persona`, `system-prompt`, `memory`, `history`, `user-input` — and visible in the Context tab with the exact text sent.

**Identity isolation.** All database queries are scoped by `identity_id`. No cross-identity data leakage is possible at the query level.

**Visible retrieval.** Every memory entry retrieved, its score, and the reason it was selected are shown after every turn. Retrieval is not a black box.

**Explicit generation controls.** Temperature, top_p, and repetition_penalty are shown in the sidebar and recorded in every audit entry. Nothing is tuned silently between turns.

**Structured observation.** Dual-run logging produces records that can be analysed rather than relying on memory or impression. State tagging and breakthrough detection give the data structure that makes comparison meaningful.
