"""
config.py — Single point of entry for all runtime configuration.

Loads .env via python-dotenv and config.yaml via PyYAML, validates required
values, and exposes a flat namespace to all other modules.

Raises ValueError on import if any required configuration is absent or invalid.

New fields (Req 16.1–16.5, 21.1–21.3, 9.3, 20.6, 19.9):
  - top_k_per_type: int, default 3, range [1, 20]
  - recency_weight: float, default 0.4
  - relevance_weight: float, default 0.6
  - history_truncation_strategy: str, one of last-n-turns / token-budget / summarize-and-compress
  - history_max_turns: int, default 20, range [1, 100]
  - history_token_budget: int, default 4000, range [100, 200000]
  - memory_extraction_enabled: bool, default True
  - assistant_language_patterns: list[str], default per Req 16.5
"""

import os

import yaml
from dotenv import load_dotenv

# Load .env from the project root (parent of the falcon/ package directory)
_env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
load_dotenv(dotenv_path=_env_path)

# ---------------------------------------------------------------------------
# Read and validate OPENROUTER_API_KEY
# ---------------------------------------------------------------------------
_raw_api_key = os.environ.get("OPENROUTER_API_KEY", "")

if not _raw_api_key or not _raw_api_key.strip():
    raise ValueError(
        "OPENROUTER_API_KEY is not set. "
        "Add your OpenRouter API key to the .env file: OPENROUTER_API_KEY=sk-or-..."
    )

OPENROUTER_API_KEY: str = _raw_api_key

# ---------------------------------------------------------------------------
# Read and validate MONGODB_URI
# ---------------------------------------------------------------------------
_raw_mongo_uri = os.environ.get("MONGODB_URI", "")

if not _raw_mongo_uri or not _raw_mongo_uri.strip():
    raise ValueError(
        "MONGODB_URI is not set. "
        "Add your MongoDB Atlas connection string to the .env file:\n"
        "  MONGODB_URI=mongodb+srv://<user>:<password>@<cluster>.mongodb.net/?retryWrites=true&w=majority"
    )

MONGODB_URI: str = _raw_mongo_uri

# ---------------------------------------------------------------------------
# Load and validate config.yaml
# ---------------------------------------------------------------------------
_config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.yaml")

try:
    with open(_config_path, "r", encoding="utf-8") as _f:
        _cfg = yaml.safe_load(_f)
except FileNotFoundError:
    raise ValueError(
        f"config.yaml not found at '{_config_path}'. "
        "Create config.yaml in the project root with the required fields."
    )

if not isinstance(_cfg, dict):
    raise ValueError(
        "config.yaml must contain a YAML mapping at the top level."
    )

# Validate default_model
_default_model = _cfg.get("default_model")
if not isinstance(_default_model, str) or not _default_model.strip():
    raise ValueError(
        "'default_model' is missing, empty, or not a string in config.yaml."
    )

# Validate log_dir
_log_dir = _cfg.get("log_dir")
if not isinstance(_log_dir, str) or not _log_dir.strip():
    raise ValueError(
        "'log_dir' is missing, empty, or not a string in config.yaml."
    )

# ---------------------------------------------------------------------------
# Expose flat namespace
# ---------------------------------------------------------------------------
default_model: str = _default_model
log_dir: str = _log_dir

available_models: list = _cfg.get("available_models") or []

# Default system prompt — empty by design.
# Falcon's spec: absence of a system prompt must NOT activate assistant mode.
# The empty string is the correct default; no persona is injected.
default_system_prompt: str = _cfg.get("default_system_prompt") or ""

# ---------------------------------------------------------------------------
# Generation Controls
# ---------------------------------------------------------------------------
_gen = _cfg.get("generation") or {}

generation_temperature: float    = float(_gen.get("temperature", 0.7))
generation_top_p: float          = float(_gen.get("top_p", 1.0))
generation_repetition_penalty: float = float(_gen.get("repetition_penalty", 1.0))
generation_stop_tokens: list     = list(_gen.get("stop_tokens") or [])

# ---------------------------------------------------------------------------
# Audit Trail
# ---------------------------------------------------------------------------
_audit = _cfg.get("audit") or {}
audit_enabled: bool   = bool(_audit.get("enabled", True))
audit_collection: str = str(_audit.get("collection", "audit_log"))

# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------
_mem = _cfg.get("memory") or {}
memory_episodic_limit: int  = int(_mem.get("episodic_limit", 50))
memory_semantic_limit: int  = int(_mem.get("semantic_limit", 20))
memory_working_limit: int   = int(_mem.get("working_limit", 10))

# ---------------------------------------------------------------------------
# Memory Retrieval — weighted scoring (Req 9.3, 20.6)
# ---------------------------------------------------------------------------
top_k_per_type: int = int(_cfg.get("top_k_per_type", 3))
if not (1 <= top_k_per_type <= 20):
    raise ValueError(
        f"'top_k_per_type' must be an integer in [1, 20], got {top_k_per_type!r}."
    )

recency_weight: float = float(_cfg.get("recency_weight", 0.4))
relevance_weight: float = float(_cfg.get("relevance_weight", 0.6))

# ---------------------------------------------------------------------------
# Conversation History Truncation (Req 21.1)
# ---------------------------------------------------------------------------

history_max_turns: int = int(_cfg.get("history_max_turns", 20))
if not (1 <= history_max_turns <= 100):
    raise ValueError(
        f"'history_max_turns' must be an integer in [1, 100], got {history_max_turns!r}."
    )

# ---------------------------------------------------------------------------
# Default Persona (startup seeding + new-identity seeding)
# Reads the `default_persona` block from config.yaml.
# `default_persona_identity`        — which identity to seed on first run (default: "default")
# `default_persona_startup_content` — assembled "Name: ...\nTone: ..." string
# `default_persona_content`         — alias kept for backwards compat; same value
# ---------------------------------------------------------------------------
_dp = _cfg.get("default_persona") or {}
default_persona_identity: str = str(_dp.get("identity") or "default").strip()

# Build the structured content string from the four flat YAML fields.
# This is the exact format _parse_persona() in app.py expects.
_dp_name   = str(_dp.get("name")               or "").strip()
_dp_tone   = str(_dp.get("tone")               or "").strip()
_dp_style  = str(_dp.get("communication_style") or "").strip()
_dp_traits = str(_dp.get("core_traits")         or "").strip()

default_persona_startup_content: str = (
    f"Name: {_dp_name}\n"
    f"Tone: {_dp_tone}\n"
    f"Communication style: {_dp_style}\n"
    f"Core traits: {_dp_traits}"
) if any([_dp_name, _dp_tone, _dp_style, _dp_traits]) else ""

# Backwards-compat alias — always equals default_persona_startup_content now.
default_persona_content: str = default_persona_startup_content


# ---------------------------------------------------------------------------
memory_extraction_enabled: bool = bool(_cfg.get("memory_extraction_enabled", True))

# Model used for background memory extraction — separate from the inference
# model so a cheaper/faster model can be used for JSON classification.
# Falls back to default_model if not specified in config.yaml.
extraction_model: str = str(_cfg.get("extraction_model") or _default_model)

# ---------------------------------------------------------------------------
# Assistant Language Detection (Req 16.5)
# Patterns that detect when the model is responding as an AI assistant
# when the system prompt is OFF.
# ---------------------------------------------------------------------------
_DEFAULT_ASSISTANT_LANGUAGE_PATTERNS: list[str] = [
    "I am an AI",
    "As an AI",
    "I'm an AI language model",
    "I cannot",
    "I'm just an AI",
    "as a language model",
]

_raw_patterns = _cfg.get("assistant_language_patterns")
if _raw_patterns is not None and isinstance(_raw_patterns, list):
    assistant_language_patterns: list[str] = [str(p) for p in _raw_patterns]
else:
    assistant_language_patterns: list[str] = _DEFAULT_ASSISTANT_LANGUAGE_PATTERNS
