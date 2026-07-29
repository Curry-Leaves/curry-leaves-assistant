"""App settings persisted to ~/.curry-leaves/settings.json.

Holds AI provider configuration: each provider is configured independently (its own
key/model); ``active`` selects which one Curry Leaves uses. No single hard-coded default —
if ``active`` is unset we fall back to whatever is configured/available.
"""
from __future__ import annotations

from curry_leaves_assistant.core.paths import SETTINGS_PATH
from curry_leaves_assistant.core.store import read_json_safe, write_json

EMPTY_TIERS = {"less": "", "medium": "", "heavy": "", "smart": ""}

DEFAULTS = {
    "identity": {
        "name": "",
        "work": "",       # role / company / what they do
        "behavior": "",   # free-form instructions for how the app/agents should behave
        "workingHours": "",  # e.g. "9am-6pm IST, Mon-Fri"
        # What the user said they'd use Curry Leaves for, picked in the first-run wizard
        # (subset of "meetings" | "voice" | "knowledge" | "agents"). Tunes setup defaults
        # and is injected as a one-line hint into every agent's prompt.
        "usage": [],
    },
    "ai": {
        "active": "",  # "" = auto-detect from configured providers / env
        "providers": {
            "anthropic": {"apiKey": "", "model": "claude-sonnet-4-6", "tiers": dict(EMPTY_TIERS)},
            "openai": {"apiKey": "", "model": "gpt-4o", "tiers": dict(EMPTY_TIERS)},
            "google": {"apiKey": "", "model": "gemini-2.5-flash", "tiers": dict(EMPTY_TIERS)},
            # clientId / headers: optional user overrides for the Copilot connection. "" / {} =
            # our own registered app with the base request identity (GA models). A user can set a
            # different clientId and/or supply custom request headers (e.g.
            # {"Copilot-Integration-Id": "vscode-chat", "Editor-Version": "vscode/…"}) to reach a
            # different catalog — their choice, on their account. Providing either switches on the
            # token-exchange path. See providers/copilot_provider.is_overridden().
            "copilot": {"model": "", "clientId": "", "headers": {}, "tiers": dict(EMPTY_TIERS)},
            "codex": {"model": "gpt-5-codex", "tiers": dict(EMPTY_TIERS)},
            "ollama": {"baseUrl": "", "model": "llama3.1", "tiers": dict(EMPTY_TIERS)},  # local; "" baseUrl = http://localhost:11434
        },
    },
    "appearance": {
        "theme": "system",  # system | paper | sepia | dark | midnight | mono | noir
    },
    "recording": {
        "backend": "mlx-whisper",  # "mlx-whisper" | "faster-whisper" (see transcribe.REGISTRIES)
        "model": "small",          # model size within the active backend's registry
        "language": "en",          # ISO code, or "auto" to let Whisper detect
        "vocabulary": "",          # names/jargon fed to Whisper as initial_prompt/hotwords bias
    },
    # Wake word ("hey curry" → opens voice chat). Detection runs client-side; this is
    # only the config. Default off on purpose: an always-on listener holds a permanent
    # mic ref, which keeps the OS mic indicator lit for as long as the app is open.
    "wakeword": {
        "enabled": False,
        "active": "curry_leaves",  # model id — see domain/wakeword.BUILTIN + dir scan
        "threshold": 0.5,         # score ≥ this fires; raise if it false-triggers
        # ── how the spoken answer behaves ──────────────────────────────────────
        "speak": True,            # read the answer aloud (off = show it silently)
        # Keep the mic open for a follow-up after each answer, so a back-and-forth doesn't need
        # the wake word every turn. The loop ends on silence (or Escape/closing the panel), then
        # returns to listening for the wake word. On by default — it's the more natural feel.
        "continuous": True,
        "autoDismiss": True,      # hide the answer panel once it finishes
        "dismissAfterMs": 6000,   # ...how long to leave it up first
        "voice": "",              # Kokoro voice id; "" = tts.DEFAULT_VOICE
        "silenceMs": 1200,        # quiet gap that ends your question
        # Whether handing work to a background agent needs a spoken "yes" first.
        # "confirm" (default) speaks the handoff back and waits; "auto" queues it
        # silently. This gates the handoff only — the background job itself always
        # runs headlessly (autonomy=auto), since a spoken turn has no surface to
        # render a per-tool approval card on.
        "workApproval": "confirm",  # "confirm" | "auto"
    },
    # tool name -> True once the user clicks "Always allow" for it (PermissionEngine's
    # global_approvals). Applies across every agent/session — a tool is either globally
    # trusted or it isn't. Session-scoped ("just this chat") grants stay in-memory only,
    # per PermissionEngine._session_approvals, and are NOT persisted here.
    "permissions": {},
}


def _merge_provider_cfg(base: dict, patch: dict) -> dict:
    """Shallow-merge a provider cfg, but merge `tiers` one level deeper so patching a
    single tier (e.g. {"tiers": {"smart": "..."}}) doesn't drop the other saved tiers."""
    merged = {**base, **patch}
    if "tiers" in patch:
        merged["tiers"] = {**(base.get("tiers") or {}), **(patch.get("tiers") or {})}
    return merged


def read_settings() -> dict:
    s = read_json_safe(SETTINGS_PATH, {})
    # shallow-merge defaults so new keys appear without clobbering saved values
    out = {**DEFAULTS, **s}
    ai = {**DEFAULTS["ai"], **(s.get("ai") or {})}
    providers = {**DEFAULTS["ai"]["providers"]}
    for k, v in (s.get("ai", {}).get("providers") or {}).items():
        providers[k] = _merge_provider_cfg(providers.get(k, {}), v)
    ai["providers"] = providers
    out["ai"] = ai
    out["appearance"] = {**DEFAULTS["appearance"], **(s.get("appearance") or {})}
    out["recording"] = {**DEFAULTS["recording"], **(s.get("recording") or {})}
    out["wakeword"] = {**DEFAULTS["wakeword"], **(s.get("wakeword") or {})}
    out["identity"] = {**DEFAULTS["identity"], **(s.get("identity") or {})}
    out["permissions"] = {**DEFAULTS["permissions"], **(s.get("permissions") or {})}
    return out


def patch_ai(patch: dict) -> dict:
    """Merge an AI-config patch: {active?, providers?: {<id>: {...}}}."""
    s = read_settings()
    ai = s["ai"]
    if "active" in patch:
        ai["active"] = patch["active"]
    for pid, cfg in (patch.get("providers") or {}).items():
        ai["providers"][pid] = _merge_provider_cfg(ai["providers"].get(pid, {}), cfg)
    write_json(SETTINGS_PATH, s)
    try:
        SETTINGS_PATH.chmod(0o600)  # holds API keys + the Copilot token
    except Exception:
        pass
    return s


def patch_recording(patch: dict) -> dict:
    """Merge a recording patch: {backend?, model?, language?, vocabulary?}."""
    s = read_settings()
    for k in ("backend", "model", "language", "vocabulary"):
        if k in patch:
            s["recording"][k] = patch[k]
    write_json(SETTINGS_PATH, s)
    return s


def recording_cfg() -> dict:
    return read_settings()["recording"]


def patch_wakeword(patch: dict) -> dict:
    """Merge a wake-word patch: {enabled?, active?, threshold?}.

    Its own block rather than a `recording` key: patch_recording whitelists exactly four
    keys and silently drops anything else, so wake-word config would vanish on save.
    """
    s = read_settings()
    for k in ("enabled", "active", "threshold", "speak", "continuous", "autoDismiss",
              "dismissAfterMs", "voice", "silenceMs", "workApproval"):
        if k in patch:
            s["wakeword"][k] = patch[k]
    write_json(SETTINGS_PATH, s)
    return s


def wakeword_cfg() -> dict:
    return read_settings()["wakeword"]


def patch_identity(patch: dict) -> dict:
    """Merge an identity patch: {name?, work?, behavior?, workingHours?, usage?}."""
    s = read_settings()
    for k in ("name", "work", "behavior", "workingHours", "usage"):
        if k in patch:
            s["identity"][k] = patch[k]
    write_json(SETTINGS_PATH, s)
    return s


def identity_cfg() -> dict:
    return read_settings()["identity"]


def global_approvals() -> list[str]:
    """Tool names the user has clicked 'Always allow' for — feeds PermissionEngine's
    global_approvals so the grant actually persists across turns/sessions instead of
    being forgotten the moment the HTTP request that captured it ends."""
    return [name for name, allowed in read_settings()["permissions"].items() if allowed]


def add_global_approval(tool: str) -> None:
    """Persist a fresh 'Always allow' grant — the on_global_approve callback PermissionEngine
    calls the instant the user picks it, so it survives past this run."""
    s = read_settings()
    s["permissions"][tool] = True
    write_json(SETTINGS_PATH, s)


def patch_appearance(patch: dict) -> dict:
    """Merge an appearance patch: {theme?}."""
    s = read_settings()
    if "theme" in patch:
        s["appearance"]["theme"] = patch["theme"]
    write_json(SETTINGS_PATH, s)
    try:
        SETTINGS_PATH.chmod(0o600)  # the file also holds API keys + the Copilot token
    except Exception:
        pass
    return s


def active_ai() -> tuple[str | None, str, str]:
    """Resolve (provider_name, api_key, model) for the active provider.

    Honors ai.active when its provider is configured; otherwise returns
    (None, "", "") so the engine falls back to env-based auto-detection.
    """
    ai = read_settings()["ai"]
    name = ai.get("active") or ""
    providers = ai.get("providers", {})
    if not name:
        return None, "", ""
    cfg = providers.get(name, {})
    return name, cfg.get("apiKey", ""), cfg.get("model", "")


def provider_cfg(name: str) -> tuple[str, str]:
    """(api_key, model) configured for a specific provider (blanks if unset). Used when an
    agent pins its own provider, so we pull that provider's key/model rather than the active one."""
    providers = read_settings()["ai"].get("providers", {})
    cfg = providers.get(name, {})
    return cfg.get("apiKey", ""), cfg.get("model", "")
