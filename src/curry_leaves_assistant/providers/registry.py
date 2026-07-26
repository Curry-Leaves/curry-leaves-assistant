"""Universal AI-provider registry — the single source of truth for provider metadata.

Every place that used to hardcode per-provider knowledge (how to build the client, the
default model, which model-id prefixes to show, the curated fallback list, the live
model-list URL) now reads it from one ``ProviderSpec`` here. Adding a mainstream provider
is a single entry in ``_SPECS`` — no scattered code edits.

Two escape hatches keep this genuinely universal:

  - ``wire`` says how to talk to the provider. Almost every modern provider speaks the
    OpenAI chat-completions wire, so ``wire="openai"`` + a ``base_url`` covers the long
    tail (Groq, Together, OpenRouter, DeepSeek, Mistral, xAI, Perplexity, Gemini's compat
    endpoint, …). Only Anthropic needs its native wire. ``copilot``/``codex``/``ollama``
    keep their own special connect handlers and are marked ``wire="special"``.

  - User-defined **custom** providers (any OpenAI-compatible endpoint the user pastes a
    base URL for) are resolved via ``spec_for`` too — ``custom_spec`` fabricates a
    ``ProviderSpec`` on the fly from the saved cfg, so the rest of the app treats a custom
    endpoint exactly like a built-in one.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderSpec:
    id: str                       # stable key, e.g. "google", "groq", or a custom id
    name: str                     # display name for the UI
    wire: str = "openai"          # "openai" | "anthropic" | "special"
    base_url: str = ""            # OpenAI-compatible API root (…/v1). "" = provider's own default
    key_envs: tuple = ()          # env vars that also count as a configured key
    key_placeholder: str = ""     # UI hint for the key field
    default_model: str = ""       # used when the user hasn't picked one
    curated: tuple = ()           # fallback model ids when a live pull fails / isn't supported
    model_prefixes: tuple = ()    # keep only model ids starting with one of these ("" = keep all)
    hint: str = ""                # one-line UI description
    keyed: bool = True            # False for local/OAuth providers (copilot/codex/ollama)
    custom: bool = False          # True for user-defined endpoints
    tiers: bool = True            # whether the effort-tier pickers apply


# Google/OpenAI-compatible base. Gemini serves an OpenAI wire at this root.
_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"

_SPECS: dict[str, ProviderSpec] = {
    "anthropic": ProviderSpec(
        id="anthropic", name="Anthropic", wire="anthropic", key_envs=("ANTHROPIC_API_KEY",),
        key_placeholder="sk-ant-…", default_model="claude-sonnet-4-6",
        curated=("claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5", "claude-sonnet-4-5"),
        model_prefixes=("claude-",),
        hint="Claude models. Get a key at console.anthropic.com."),
    "openai": ProviderSpec(
        id="openai", name="OpenAI", wire="openai", key_envs=("OPENAI_API_KEY",),
        key_placeholder="sk-…", default_model="gpt-4o",
        curated=("gpt-4o", "gpt-4o-mini", "gpt-4.1", "o3", "o4-mini"),
        model_prefixes=("gpt-", "o1", "o3", "o4", "chatgpt-"),
        hint="GPT / o-series models. Get a key at platform.openai.com."),
    "google": ProviderSpec(
        id="google", name="Google Gemini", wire="openai", base_url=_GEMINI_BASE,
        key_envs=("GEMINI_API_KEY", "GOOGLE_API_KEY"), key_placeholder="AIza…",
        default_model="gemini-2.5-flash",
        curated=("gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash"),
        model_prefixes=("gemini-", "gemma-"),
        hint="Gemini models via Google's OpenAI-compatible endpoint. Key at ai.google.dev."),
    "groq": ProviderSpec(
        id="groq", name="Groq", wire="openai", base_url="https://api.groq.com/openai/v1",
        key_envs=("GROQ_API_KEY",), key_placeholder="gsk_…", default_model="llama-3.3-70b-versatile",
        curated=("llama-3.3-70b-versatile", "llama-3.1-8b-instant", "mixtral-8x7b-32768"),
        hint="Fast open models on Groq's LPU. Key at console.groq.com."),
    "together": ProviderSpec(
        id="together", name="Together AI", wire="openai", base_url="https://api.together.xyz/v1",
        key_envs=("TOGETHER_API_KEY",), key_placeholder="…",
        default_model="meta-llama/Llama-3.3-70B-Instruct-Turbo",
        curated=("meta-llama/Llama-3.3-70B-Instruct-Turbo", "Qwen/Qwen2.5-72B-Instruct-Turbo"),
        hint="Open models on Together. Key at api.together.xyz."),
    "openrouter": ProviderSpec(
        id="openrouter", name="OpenRouter", wire="openai", base_url="https://openrouter.ai/api/v1",
        key_envs=("OPENROUTER_API_KEY",), key_placeholder="sk-or-…",
        default_model="openai/gpt-4o",
        curated=("openai/gpt-4o", "anthropic/claude-sonnet-4-6", "google/gemini-2.5-flash"),
        hint="One key, hundreds of models across providers. Key at openrouter.ai."),
    "deepseek": ProviderSpec(
        id="deepseek", name="DeepSeek", wire="openai", base_url="https://api.deepseek.com/v1",
        key_envs=("DEEPSEEK_API_KEY",), key_placeholder="sk-…", default_model="deepseek-chat",
        curated=("deepseek-chat", "deepseek-reasoner"),
        hint="DeepSeek V3 / R1. Key at platform.deepseek.com."),
    "mistral": ProviderSpec(
        id="mistral", name="Mistral", wire="openai", base_url="https://api.mistral.ai/v1",
        key_envs=("MISTRAL_API_KEY",), key_placeholder="…", default_model="mistral-large-latest",
        curated=("mistral-large-latest", "mistral-small-latest", "codestral-latest"),
        hint="Mistral models. Key at console.mistral.ai."),
    "xai": ProviderSpec(
        id="xai", name="xAI (Grok)", wire="openai", base_url="https://api.x.ai/v1",
        key_envs=("XAI_API_KEY",), key_placeholder="xai-…", default_model="grok-4",
        curated=("grok-4", "grok-3", "grok-3-mini"),
        hint="Grok models. Key at console.x.ai."),
    "perplexity": ProviderSpec(
        id="perplexity", name="Perplexity", wire="openai", base_url="https://api.perplexity.ai",
        key_envs=("PERPLEXITY_API_KEY",), key_placeholder="pplx-…", default_model="sonar",
        curated=("sonar", "sonar-pro", "sonar-reasoning"),
        hint="Web-grounded Sonar models. Key at perplexity.ai."),
    # Special providers keep their own connect handlers (OAuth / local probe). The registry
    # only carries their display metadata + default model; wire="special" routes _make_provider
    # to the dedicated module.
    "copilot": ProviderSpec(
        id="copilot", name="GitHub Copilot", wire="special", keyed=False, default_model="gpt-5-mini",
        hint="Uses your GitHub Copilot subscription — no API key."),
    "codex": ProviderSpec(
        id="codex", name="OpenAI Codex", wire="special", keyed=False, default_model="gpt-5-codex",
        hint="Uses your ChatGPT (Codex) subscription — sign in, no API key."),
    "ollama": ProviderSpec(
        id="ollama", name="Ollama (local)", wire="special", keyed=False, default_model="llama3.1",
        hint="Runs models locally via Ollama — no API key, nothing leaves your machine."),
}

# Display order for the built-in providers in the UI catalog.
BUILTIN_ORDER = ("anthropic", "openai", "google", "groq", "together", "openrouter",
                 "deepseek", "mistral", "xai", "perplexity", "copilot", "codex", "ollama")


def builtin_specs() -> list[ProviderSpec]:
    """All built-in provider specs in display order."""
    return [_SPECS[i] for i in BUILTIN_ORDER if i in _SPECS]


def is_builtin(provider_id: str) -> bool:
    return provider_id in _SPECS


def custom_spec(provider_id: str, cfg: dict) -> ProviderSpec:
    """Fabricate a spec for a user-defined OpenAI-compatible provider from its saved cfg.
    The cfg carries the display name + base URL the user entered."""
    return ProviderSpec(
        id=provider_id,
        name=(cfg.get("name") or provider_id),
        wire="openai",
        base_url=(cfg.get("baseUrl") or "").rstrip("/"),
        default_model=(cfg.get("model") or ""),
        keyed=True,
        custom=True,
        hint="Custom OpenAI-compatible endpoint.",
    )


def spec_for(provider_id: str, cfg: dict | None = None) -> ProviderSpec:
    """The spec for any provider id — a built-in from the registry, or a fabricated custom
    one. Falls back to a bare OpenAI-compatible spec for an unknown id so nothing hard-crashes."""
    if provider_id in _SPECS:
        return _SPECS[provider_id]
    cfg = cfg or {}
    if cfg.get("custom") or cfg.get("baseUrl"):
        return custom_spec(provider_id, cfg)
    # Unknown, no cfg: assume an OpenAI-wire provider with no preset base URL.
    return ProviderSpec(id=provider_id, name=provider_id, wire="openai")
