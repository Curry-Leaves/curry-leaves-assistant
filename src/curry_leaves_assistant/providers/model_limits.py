"""Context-window limits per model, keyed by substring of the model id.

The context meter needs the real window for whatever model ran — a hardcoded 128k is
wrong for most models (Claude is 200k, Gemini 1M+, GPT-4.1 1M). Provider APIs don't
report this uniformly, so we keep a small curated table matched by substring (model ids
carry version/date suffixes, e.g. ``claude-sonnet-4-6-20250219``). Order matters: the
FIRST matching prefix wins, so list more-specific keys before their prefixes."""
from __future__ import annotations

# (id-substring, context tokens). Matched in order; first hit wins.
_LIMITS: list[tuple[str, int]] = [
    # ── Anthropic ──
    ("claude-opus-4", 200_000),
    ("claude-sonnet-4", 200_000),
    ("claude-haiku-4", 200_000),
    ("claude-3-5", 200_000),
    ("claude-3", 200_000),
    ("claude", 200_000),
    # ── OpenAI ──
    ("gpt-5", 400_000),
    ("gpt-4.1", 1_047_576),
    ("gpt-4o", 128_000),
    ("o4", 200_000),
    ("o3", 200_000),
    ("o1", 200_000),
    ("gpt-4", 128_000),
    ("gpt-3.5", 16_385),
    # ── Google ──
    ("gemini-2.5-pro", 2_097_152),
    ("gemini-2.5", 1_048_576),
    ("gemini-2.0", 1_048_576),
    ("gemini-1.5-pro", 2_097_152),
    ("gemini", 1_048_576),
]

DEFAULT_LIMIT = 128_000


def context_limit(model_id: str | None) -> int:
    """Best-guess context window for a model id. Falls back to DEFAULT_LIMIT (128k) for
    unknown models — conservative, so the meter never claims more headroom than exists."""
    if not model_id:
        return DEFAULT_LIMIT
    mid = model_id.lower()
    for key, limit in _LIMITS:
        if key in mid:
            return limit
    return DEFAULT_LIMIT
