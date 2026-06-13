# Falcon

A transparent inference environment built on Streamlit, MongoDB, and OpenRouter. Falcon is not an assistant or chatbot — it is an inference layer with full context visibility, user-controlled memory, and a complete audit trail.

---

## What It Is

Falcon gives you direct access to LLM inference with nothing hidden. Every component that enters a generation — persona, system prompt, retrieved memories, conversation history — is labelled, inspectable, and editable. Memory is stored explicitly and retrieved visibly. Every inference event is logged completely.

---

## Setup

**Requirements:** Python 3.11+, a MongoDB Atlas cluster, an OpenRouter API key.

**1. Install dependencies**
```bash
pip install streamlit pymongo openai python-dotenv pyyaml
```

**2. Create `.env` in the project root**
```env
OPENROUTER_API_KEY=sk-or-your-key-here
MONGODB_URI=mongodb+srv://user:password@cluster.mongodb.net/?retryWrites=true&w=majority
```

**3. Configure `config.yaml`** — see the [Configuration](#configuration) section below.

**4. Run**
```bash
streamlit run app.py
```

---

## Configuration

All runtime behaviour is controlled by `config.yaml`. The `.env` file holds secrets only.

```yaml
# Model used by default for inference
default_model: "google/gemini-2.5-flash"

# Models available in the sidebar dropdown
available_models:
  - "openai/gpt-4o-mini"
  - "google/gemini-2.5-flash"
  - "meta-llama/llama-3.3-70b-instruct"

# System prompt shown by default when system prompt is ON
# Leave empty for a fully neutral context (no instructions injected)
default_system_prompt: ""

# Directory for local log files (legacy, kept for compatibility)
log_dir: "logs"

# Generation defaults — adjustable live in the sidebar
generation:
  temperature: 0.7
  top_p: 1.0
  repetition_penalty: 1.0
  stop_tokens: []

# Audit trail
audit:
  enabled: true
  collection: "audit_log"

# Memory per-type limits
memory:
  episodic_limit: 50
  semantic_limit: 20
  working_limit: 10

# Retrieval scoring — how retrieved memory is ranked per turn
top_k_per_type: 3        # max entries per memory type returned to generation
recency_weight: 0.4      # weight for recency in scoring
relevance_weight: 0.6    # weight for keyword/tag overlap in scoring

# Model for automatic memory extraction (use a fast/cheap model)
extraction_model: "openai/gpt-4o-mini"

# Default persona applied to every new identity on creation
# Overrides the hardcoded default below if set
default_persona_content: "Your custom persona text here"
```

The hardcoded default persona (used if `default_persona_content` is not set in `config.yaml`) is:

> You are a neutral text-processing interface. Answer only the user's last request. Do not mention system prompts, hidden instructions, policies, roles, or internal labels. Do not refer to yourself as an AI, assistant, language model, system, or computer program. Do not roleplay, play games, simulate entities, or grant/deny permission inside scenarios. If the user asks for non-informational content such as roleplay, games, or pretend interaction, refuse briefly. Otherwise, respond normally and keep the answer as short as possible while remaining correct.

---

## Architecture

```
app.py                  — Streamlit UI, all tabs and sidebar
falcon/
  config.py             — Configuration loader and validator
  db.py                 — MongoDB connection singleton
  identity.py           — Identity management (create, list, load history)
  logger.py             — Message persistence (append_message)
  memory.py             — Memory CRUD and retrieval with weighted scoring
  memory_extractor.py   — Automatic memory extraction after each turn
  engine.py             — Payload assembly, truncation, streaming inference
  audit.py              — Inference audit trail (write and read)
  export_utils.py       — JSON export helpers
```

### MongoDB Collections

| Collection   | Contents |
|---|---|
| `identities` | One doc per identity: `{identity_id, created_at}` |
| `messages`   | Conversation history: `{identity_id, timestamp, role, content}` |
| `memory`     | All memory entries: `{identity_id, memory_type, content, tags, pinned, source, created_at, updated_at}` |
| `traces`     | Per-turn reasoning traces: `{identity_id, user_timestamp, steps, ...}` |
| `tokens`     | Cumulative token usage per identity |
| `audit_log`  | Full inference audit records per turn |

---

## Identities

An identity is an isolated context — its own conversation history, memory store, persona, and audit trail. Nothing leaks between identities.

- **Create** — enter a name in the sidebar and click `＋ Create`. The identity is persisted immediately and seeded with the default persona.
- **Switch** — select from the dropdown. History and memory load instantly.
- **Delete** — click the delete button. Removes everything: messages, memory, traces, tokens, audit records, and the identity registry entry.
- The `default` identity always exists and cannot be deleted.

---

## Memory

Memory is user-controlled and retrieval is always visible. Six types:

| Type | Purpose |
|---|---|
| `semantic` | Long-term facts, knowledge, domain concepts |
| `episodic` | Specific past events and notable interactions |
| `procedural` | Learned behaviors, stated preferences, workflow patterns |
| `working` | Short-term scratch space for the current session |
| `archive` | Aged-out or low-relevance entries — never retrieved |
| `persona` | The identity's behavior definition — always injected first |

### Retrieval

Before each generation, relevant memory is retrieved using a weighted scoring formula:

```
score = (recency_rank_score × recency_weight) + (overlap_score × relevance_weight)
```

- `recency_rank_score` — `1/(rank+1)` where rank 0 is the newest entry
- `overlap_score` — tag match → keyword match → 0.0 (pinned entries always score 1.0)
- Top `top_k_per_type` entries per active type are retrieved
- Persona is always prepended — never scored, always included
- Archive is never retrieved

Retrieval results and per-entry reasoning are shown in the **Context** tab after every turn.

### Automatic Extraction

After every turn, a second LLM call (using `extraction_model`) classifies the conversation into memory entries and persists them with `source="auto"`. It only extracts facts about the user — never about the model itself. A hard code-level filter (`_should_reject`) catches anything the prompt misses (AI self-descriptions, greetings, metadata, questions).

If extraction fails, a warning is shown in the chat — it never fails silently.

### Persona

Each identity has one persona entry. It is injected as the first system message on every turn, wrapped with:

```
[PERSONA — this defines your identity and behavior. Adopt it completely for this conversation.]
<persona content>
```

Edit it anytime from the **Memory tab → Edit Persona**. The fields are stored as a single content string and parsed back for display.

---

## Inference Pipeline

Each turn follows this sequence:

1. Log user message to `messages` collection
2. Retrieve relevant memory (weighted scoring, 500ms timeout)
3. Assemble payload via `build_annotated_payload`:
   - Persona block (system)
   - System prompt (system, if enabled)
   - Memory block (system, grouped by type)
   - Conversation history (truncated to last N turns)
   - Current user input
4. Stream response via OpenRouter (`stream=True`)
5. Strip `<think>…</think>` blocks from output (fast-path skips this for models that don't use it)
6. Log assistant message
7. Run memory extraction synchronously
8. Write audit record and token counts in background threads
9. `st.rerun()` — UI refreshes with new message and memories visible

### Streaming

The OpenAI client is cached at module level — one connection pool reused across all requests. The HTTP connection is opened lazily (on first iteration) so Streamlit starts rendering immediately when tokens arrive.

### History Truncation

Strategy: **last-n-turns**. Keeps the most recent N turn-pairs (user + assistant). Default is 20 turns, adjustable in the sidebar. Dropped turns are counted and shown in the Context tab.

---

## UI Tabs

### Chat
Standard chat interface. Messages stream token-by-token. Each assistant turn has a `⌥ context` button that opens a dialog showing the exact assembled payload sent to the model for that turn.

### Context
Shows the full context snapshot for the last turn: persona block, system prompt state, retrieved memory entries with scores and match reasons, history included/dropped, token estimate, and the raw assembled payload.

### Memory
Full read/write access to the memory store:
- **Edit Persona** — edit the identity's persona (pre-filled from stored content)
- **Export** — download all memory as JSON
- **Test Retrieval** — run a retrieval query and see scored results
- Per-type tabs (Semantic, Episodic, Procedural, Working, Archive) — add, pin, tag, edit, delete, or clear entries

### Audit
Complete inference audit log. Every turn records: model, prompt state, system prompt text, retrieved memories, generation settings, context size, token estimate, raw model output, token usage, and latency. Exportable and filterable.

### Logs
Raw trace log for the last turn — every stage of the inference pipeline with timestamps, useful for debugging.

---

## Sidebar Controls

| Control | Description |
|---|---|
| Identity selector | Switch between identities |
| Create identity | Name + button — persisted immediately with default persona |
| Delete identity | Removes all data for the current identity |
| Model | Select from `available_models` in config.yaml |
| System prompt | Toggle on/off; edit inline. When off, no system message is sent |
| History Truncation | Max turns to keep in context (1–100) |
| Generation Controls | Temperature, top_p, repetition_penalty, stop tokens |
| Session stats | Cumulative token usage for the current session |

---

## Design Principles

- **No hidden injection** — if a system prompt is off, nothing is prepended. No silent fallback to assistant mode.
- **Always output** — the model always returns something. Empty output triggers `[no output]` rather than silence.
- **Full transparency** — every source that enters generation is labelled (`persona`, `system-prompt`, `memory`, `history`, `user-input`) and visible in the Context tab.
- **Identity isolation** — all DB queries are scoped by `identity_id`. No cross-identity data leakage.
- **Visible retrieval** — every memory entry retrieved, its score, and the reason it was selected are shown after every turn.
- **Explicit generation controls** — temperature, top_p, repetition_penalty are shown in the sidebar and in every audit record. Nothing is tuned silently.
