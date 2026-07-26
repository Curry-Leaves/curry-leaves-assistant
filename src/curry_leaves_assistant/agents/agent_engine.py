"""Thin wrapper over curry-leaves: turn a Curry Leaves agent record into a runnable Runner.

The agent record (md + meta merged) supplies the model, instructions, and tool
list; curry-leaves supplies the loop. Pool jobs use ``run_agent`` (headless, allow
all curated tools); chat uses ``stream_agent`` to surface events over SSE.
"""
from __future__ import annotations

import asyncio
import dataclasses
import os
import re

from curry_leaves.core.agent import Agent
from curry_leaves.runner import Runner, RunConfig
from curry_leaves.catalog import resolve_model
from curry_leaves.providers.base import ModelSettings
from curry_leaves.thinking import ThinkingConfig, Effort
from curry_leaves.elision import ElisionConfig

from curry_leaves_assistant.agents import agent_tools

from curry_leaves_assistant.core import settings as app_settings

from curry_leaves_assistant.core.textfmt import trunc as _trunc


@dataclasses.dataclass
class _RunMeta:
    """Synthetic first event yielded by stream_agent carrying resolved run metadata."""
    type: str = "run_meta"
    model: str = ""

# Provider defaults + all per-provider metadata now live in providers/registry.py — the
# single source of truth. Resolve a provider's default via default_model_id() / spec_for().


# Anthropic-style claude ids use dashes (claude-sonnet-4-6); Copilot serves the same models
# with a dotted minor version (claude-sonnet-4.6). Agents declare the dashed (Anthropic) form.
_CLAUDE_VER = re.compile(r"^(claude-[a-z]+)-(\d+)-(\d+)$")

# Copilot's available model ids, fetched once. None until warmed; {} if the fetch failed.
_COPILOT_MODELS: set[str] | None = None


def _to_copilot_id(mid: str | None) -> str | None:
    """Map an agent's claude id to the id Copilot accepts, or None if it isn't a claude id."""
    if not mid:
        return None
    m = _CLAUDE_VER.match(mid)
    if m:
        return f"{m.group(1)}-{m.group(2)}.{m.group(3)}"
    return mid if mid.startswith("claude-") else None


async def warm_copilot_models() -> set[str]:
    """Cache Copilot's model list so model selection can stay sync (and never 400 on an id
    Copilot doesn't offer). Safe to call repeatedly — the result is memoized."""
    global _COPILOT_MODELS
    if _COPILOT_MODELS is None:
        try:
            from curry_leaves_assistant.providers import copilot

            models = await copilot.list_models()
            _COPILOT_MODELS = {mid for m in models if isinstance(m, dict) and (mid := m.get("id"))}
        except Exception:
            _COPILOT_MODELS = set()
    return _COPILOT_MODELS or set()


def _select_model_id(name: str, agent_model: str | None, model_override: str | None,
                     cfg_model: str | None, *, provider_pinned: bool = False) -> str:
    """Resolve the model id to run. An explicit per-request model wins; then the agent's own
    declared model — always honored when the agent also pinned its provider (the provider+model
    pair is intentional), otherwise only for the family the active provider serves (an off-family
    id 400s the whole turn). Copilot ids are translated and membership-checked either way; else
    the configured / provider default."""
    if model_override:
        return model_override
    if agent_model:
        if name == "copilot":
            cid = _to_copilot_id(agent_model)
            if cid and _COPILOT_MODELS and cid in _COPILOT_MODELS:
                return cid
        elif provider_pinned or name == "anthropic":
            return agent_model
    picked = cfg_model or os.environ.get("CURRY_LEAVES_MODEL")
    if picked:
        return picked
    # Copilot: never fall back to an arbitrary id — the offered list varies by
    # subscription, so require an explicit choice instead of silently picking one.
    if name == "copilot":
        raise RuntimeError(
            "No default model selected for GitHub Copilot. Pick one in Settings → AI providers"
            + (f" (this agent's model {agent_model!r} isn't offered by your Copilot subscription)."
               if agent_model else "."))
    from curry_leaves_assistant.providers import registry
    cfg = app_settings.read_settings()["ai"]["providers"].get(name, {})
    return registry.spec_for(name, cfg).default_model or "gpt-4o"


def _thinking_supported(provider_name: str, model_id: str) -> bool:
    """Whether this provider/model combo supports extended thinking at all. Kept
    conservative: unsupported params 400 the whole turn.
      • Anthropic direct  -> extended thinking on claude-* models.
      • Copilot           -> Claude Sonnet streams thinking as reasoning_text.
      • Codex             -> reasoning models, effort maps to reasoning_effort."""
    mid = (model_id or "").lower()
    if provider_name == "anthropic" and mid.startswith("claude"):
        return True
    if provider_name == "copilot" and "sonnet" in mid:
        return True
    if provider_name == "codex":
        return True
    return False


def _thinking_effort(provider_name: str, model_id: str, requested: Effort | None) -> Effort | None:
    """Resolve the reasoning Effort to actually use: the caller's requested level, gated
    to providers/models that support thinking at all. `requested=None` means "off"."""
    if requested is None:
        return None
    if not _thinking_supported(provider_name, model_id):
        return None
    return requested


def _detect_provider_name() -> str:
    name = os.environ.get("CURRY_LEAVES_PROVIDER")
    if name:
        return name
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        return "google"
    if os.environ.get("OLLAMA_HOST"):
        return "ollama"
    return "anthropic"


def _make_provider(name: str, api_key: str):
    """Build a kernel Provider for any registered or custom provider. Anthropic uses its
    native wire; every OpenAI-compatible provider (openai, google, groq, together, openrouter,
    deepseek, mistral, xai, perplexity, and any user-defined custom endpoint) is built with
    OpenAIProvider pointed at the spec's base URL. copilot/codex/ollama keep their dedicated
    handlers. Registry-driven, so a new provider is a data entry, not a code branch."""
    from curry_leaves_assistant.providers import registry
    key = api_key or None
    # A custom provider's base URL lives in its saved cfg; look it up so spec_for can build it.
    cfg = app_settings.read_settings()["ai"]["providers"].get(name, {}) if not registry.is_builtin(name) else {}
    spec = registry.spec_for(name, cfg)

    if spec.wire == "special":
        if name == "copilot":
            from curry_leaves_assistant.providers import copilot
            return copilot.build_provider()  # stored GitHub token
        if name == "codex":
            from curry_leaves_assistant.providers import codex
            return codex.build_provider()  # stored ChatGPT (Codex) tokens
        if name == "ollama":
            from curry_leaves_assistant.providers import ollama
            return ollama.build_provider()  # local server, base URL from settings/env
        raise RuntimeError(f"Unknown special provider: {name!r}")

    if spec.wire == "anthropic":
        from curry_leaves.providers.anthropic import AnthropicProvider
        return AnthropicProvider(api_key=key)

    # OpenAI-compatible (the universal path). base_url "" → kernel's own OpenAI default.
    from curry_leaves.providers.openai import OpenAIProvider, OpenAIProviderOptions
    return OpenAIProvider(OpenAIProviderOptions(api_key=key, base_url=(spec.base_url or None)))


def active_provider_name() -> str:
    name, _key, _model = app_settings.active_ai()
    return name or _detect_provider_name()


def default_model_id(name: str) -> str:
    """The model id that will actually be used when a run doesn't pick one explicitly:
    the provider's configured model, else its registry default (mirrors _select_model_id)."""
    from curry_leaves_assistant.providers import registry
    cfg_model = app_settings.provider_cfg(name)[1]
    if cfg_model:
        return cfg_model
    env_model = os.environ.get("CURRY_LEAVES_MODEL")
    if env_model:
        return env_model
    cfg = app_settings.read_settings()["ai"]["providers"].get(name, {})
    return registry.spec_for(name, cfg).default_model


def _effective_provider(agent_record: dict) -> str:
    """The provider this agent will actually run on: its pinned provider, else the app default."""
    return (agent_record.get("provider") or "").strip() or active_provider_name()


async def _build_agent(rec: dict, provider, model, settings, _depth: int = 0, _mcp_servers: list | None = None,
                       extra_tools: list | None = None, output_type: type | None = None,
                       query: str | None = None, promote: set[str] | None = None):
    """Build a smart-loop Agent, recursively attaching its declared `subagents` (a list of
    agent ids) as call-and-return delegation tools (agent-as-tool). Recursion is bounded so
    a nested roster (curry-leaves → keeper → researcher/executor/reviewer) is built in full, while a
    stray cycle can't loop forever. The delegation tool name is the id's last segment.

    Any declared MCP tools (`mcp__<server>__<tool>`) are connected on demand and appended;
    the live server connections are collected into `_mcp_servers` (shared across the whole
    recursive build) so the top-level caller can close them once the run ends.

    `extra_tools`: pre-built tool instances appended for just this one build call (not
    recursed into subagents, not looked up by name) — for run-scoped capabilities a caller
    injects ad hoc, e.g. dashboard_runner's per-tile notify_user tool. Never mutates the
    shared ALL_TOOLS registry, so concurrent runs can't race on each other's instances.

    `output_type`: a pydantic model the run's FINAL reply must match (curry-leaves injects
    the JSON schema into the system prompt and validates/retries in Runner.run). Applies to
    this top-level agent only — subagents keep free-text replies.

    `deferredTools` (from the agent record, opt-in per agent): tool names registered but
    NOT advertised in the system prompt until the model calls curry-leaves's built-in
    search_tools and finds them by keyword — keeps a big roster's schema tokens off every
    turn's prompt for tools that aren't needed most turns. An agent that declares none
    behaves exactly as before (everything always-on); extra_tools and MCP tools are
    always-on regardless — they're run-scoped/on-demand already, not part of a static
    roster worth deferring.

    `promote`: tool names to advertise for THIS run even though the agent defers them. A turn
    carrying an @-reference to a recording knows it needs recordings_read; leaving it deferred
    means the model answers from the reference's title without ever opening the transcript —
    plausible-sounding and wrong. Applies to this agent only, never to subagents.

    A promoted name is granted even if the agent's roster doesn't list it at all. Agent files
    are seeded once and never migrated (agent_store.seed_default_agents), so an assistant
    created before a tool existed would otherwise be permanently unable to open the thing the
    user just pointed at. Promotion is driven by an explicit @-reference in THIS turn, so this
    grants only what the user directly asked for — not a general widening of the roster."""
    tool_names = rec.get("tools", [])
    deferred_names = [n for n in (rec.get("deferredTools") or []) if n not in tool_names
                      and n not in (promote or ())]
    if promote:  # a promoted tool must be IN the roster to be resolvable — add it if absent
        tool_names = [*tool_names, *(n for n in promote if n not in tool_names)]
    tools = agent_tools.resolve_tools(tool_names)
    deferred_tools = agent_tools.resolve_tools(deferred_names)
    if extra_tools:
        tools = [*tools, *extra_tools]
    mcp_names = [n for n in tool_names if n.startswith("mcp__")]
    if mcp_names:
        from curry_leaves_assistant.providers import mcp_client

        mcp_tools, connected = await mcp_client.connect_servers_for_tools(mcp_names)
        tools.extend(mcp_tools)
        if _mcp_servers is not None:
            _mcp_servers.extend(connected)
    if _depth < 3:
        from curry_leaves_assistant.stores import agent_store

        for sub_id in (rec.get("subagents") or []):
            sub_rec = agent_store.read_agent(sub_id)
            if not sub_rec:
                continue
            sub = await _build_agent(sub_rec, provider, model, settings, _depth + 1, _mcp_servers)
            tools.append(sub.as_tool(name=sub_id.split("-")[-1], description=sub_rec.get("description", "")))
    _VALID_VERDICTS = {"allow", "ask", "deny"}
    permissions = {k: v for k, v in (rec.get("permissions") or {}).items() if v in _VALID_VERDICTS}
    return Agent(
        model,
        provider=provider,
        name=rec["id"],
        description=rec.get("description", ""),
        instructions=_with_user_profile(rec.get("instructions", ""), agent_id=rec.get("id"),
                                         tool_names=[t for t in (rec.get("tools") or []) + (rec.get("deferredTools") or [])],
                                         query=query),
        tools=tools,
        deferred_tools=deferred_tools or None,
        model_settings=settings,
        permissions=permissions or None,
        max_turns=rec.get("maxSteps") or None,   # honor the agent's declared step cap
        output_type=output_type,
    )


_TASK_TOOL_NAMES = {"task_create", "task_update", "task_list", "task_get"}


def _task_tools_for(agent_record: dict, session_id: str | None) -> list:
    """Task-list tools (task_create/update/list/get) are run-scoped, not app-wide singletons
    like the rest of ALL_TOOLS — each run needs its OWN store so concurrent runs (or two
    chats) can't scribble on the same list. Only built when the agent actually declares one
    of these names in its `tools:` list, and filtered back down to just the declared subset.

    A chat run (session_id set) gets a store backed by <session_dir>/tasks.json, so the
    plan persists and resumes across turns in that session; deleting the session deletes
    that file along with the rest of the session dir, so the list is cleared out for free.
    A triggered/headless run (no session_id, e.g. the knowledge filer) gets a throwaway
    in-memory store that lives only for that one run.

    Note: not recursed into subagents (extra_tools isn't propagated by _build_agent), and
    these tools bypass the Tools settings page / tools_store.is_enabled gate that
    agent_tools.resolve_tools applies to ALL_TOOLS — acceptable for now, not engineered around.
    """
    declared = set(agent_record.get("tools") or [])
    wanted = declared & _TASK_TOOL_NAMES
    if not wanted:
        return []
    from curry_leaves.tools.tasks import TaskStore, task_tools

    task_path = None
    if session_id is not None:
        from curry_leaves_assistant.stores import chat_sessions
        task_path = chat_sessions.store.root / session_id / "tasks.json"
    return [t for t in task_tools(TaskStore(path=task_path)) if t.name in wanted]


async def build_runner(agent_record: dict, model_override: str | None = None, session_id: str | None = None,
                 host=None, permission=None, thinking_effort: Effort | None = None, autonomous: bool = False,
                 elide: bool = False, extra_tools: list | None = None, output_type: type | None = None,
                 query: str | None = None, promote: set[str] | None = None):
    """`query`: the run's user input, when the caller has it. Used only to pull the facts
    RELEVANT to this request into the prompt (see _with_user_profile's situational block) —
    omitting it simply means the agent gets the always-on profile and nothing situational.

    `promote`: deferred tool names to advertise for this run only — see _build_agent."""
    # An agent may pin its own provider (so different agents can run on different providers).
    # Otherwise use the provider the user explicitly set as the app default in Settings.
    agent_provider = (agent_record.get("provider") or "").strip()
    if agent_provider:
        name = agent_provider
        api_key, cfg_model = app_settings.provider_cfg(name)
    else:
        name, api_key, cfg_model = app_settings.active_ai()
        # No silent fallback: if the user hasn't connected a provider AND set it as the default,
        # refuse rather than quietly auto-detecting one (which used to run Claude/Anthropic even
        # when the user had connected, say, Gemini but not made it the default). Mirrors
        # readiness.ai_status()'s no_provider gate so the UI banner and actual runs agree.
        if not name:
            raise RuntimeError(
                "No AI provider is set as the default. Connect a provider in Settings → "
                "AI providers and click 'Set as default' before running agents or chat.")
    # Key-based providers with no key anywhere would otherwise "run" and return an
    # empty result that gets recorded as success — fail here with an actionable
    # message so the run surfaces as failed in the UI instead.
    _key_envs = {"anthropic": ("ANTHROPIC_API_KEY",), "openai": ("OPENAI_API_KEY",),
                 "google": ("GEMINI_API_KEY", "GOOGLE_API_KEY")}.get(name)
    if _key_envs and not api_key and not any(os.environ.get(e) for e in _key_envs):
        raise RuntimeError(
            f"No API key configured for AI provider '{name}'. Add one in Settings → "
            "AI providers, or connect GitHub Copilot / Codex / Ollama and set it as the default.")
    provider = _make_provider(name, api_key)
    model_id = _select_model_id(name, agent_record.get("model"), model_override, cfg_model,
                                provider_pinned=bool(agent_provider))
    model = resolve_model(model_id, provider=name)
    extra_tools = [*(extra_tools or []), *_task_tools_for(agent_record, session_id)]
    # Build the agent, recursively attaching any declared subagents as delegation tools.
    # Any MCP servers referenced by the agent's tools get connected here; the caller must
    # close them (via mcp_client.close_servers) once the run ends.
    mcp_servers: list = []
    cl_agent = await _build_agent(agent_record, provider, model, ModelSettings(), _mcp_servers=mcp_servers,
                                  extra_tools=extra_tools, output_type=output_type, query=query,
                                  promote=promote)
    # Enable thinking via ThinkingConfig: pass a fixed-effort classifier so thinking fires on
    # every turn (AutoThinking would only think on prompts it rates hard). Only enable for
    # provider+model combos that support it — unsupported reasoning params 400 the whole turn.
    thinking_config: ThinkingConfig | None = None
    effort = _thinking_effort(name, getattr(model, "id", model_id), thinking_effort)
    if effort is not None:
        async def _fixed_effort(_prompt: str, _e=effort) -> Effort:
            return _e
        thinking_config = ThinkingConfig(classify=_fixed_effort)
    # Build RunConfig with host, permission, skills, and optional session store.
    # tool_timeout is a per-call BACKSTOP (only applies when a tool doesn't set its own
    # `timeout`) — without it a misbehaving tool (e.g. a headless-browser wait condition
    # that never settles) hangs the whole run with no way out except the user hitting
    # Stop. 120s comfortably covers a slow page render/extraction chain without making
    # genuinely-stuck calls linger for minutes.
    config = RunConfig(
        host=host,
        permission=permission,
        skills=_scoped_skills(agent_record.get("skills") or [], agent_record.get("id")),
        thinking=thinking_config,
        autonomous=autonomous or None,
        elision=ElisionConfig(enabled=True) if elide else None,
        tool_timeout=120.0,
    )
    # A session_id makes the run stateful: wire up a curry-leaves FileSessionStore so
    # every runner event (assistant turns, tool calls, errors) is persisted to transcript.jsonl.
    # The runner attaches it; callers (stream_agent / run_agent) must await session_store.close().
    session_store = None
    prior_messages = None
    if session_id is not None:
        from curry_leaves_assistant.stores import chat_sessions

        from curry_leaves_assistant.stores.session_store import open_runner_store
        session_dir = chat_sessions.store.root / session_id
        session_store = open_runner_store(
            session_dir,
            session_id,
            model=getattr(model, "id", str(model)),
            provider=name,
        )
        config.store = session_store
        # Rehydrate the conversation the model sees: the store only wires up future writes, so
        # without this a brand-new Runner starts with an empty history on every turn and the
        # model loses all prior context on follow-up questions.
        prior_messages = chat_sessions.store.load(session_id)
    runner = Runner(cl_agent, config=config)
    if prior_messages:
        runner.messages = prior_messages
    return runner, session_store, mcp_servers


# Wizard usage picks → prose an agent can read. Keys match the ids the first-run
# wizard writes to `identity.usage` (see renderer/screens/setup/steps/UsageStep).
USAGE_LABELS = {
    "meetings": "recording and summarising meetings",
    "voice": "talking to it hands-free as a voice assistant",
    "knowledge": "keeping notes and a personal knowledge base",
    "agents": "running a team of AI assistants on their behalf",
}


def _with_user_profile(instructions: str, *, agent_id: str | None = None,
                       tool_names: list[str] | None = None, query: str | None = None) -> str:
    """Prepend the agent's MEMORY to its instructions: the user's IDENTITY (who they are, from
    Settings → Identity), the shared USER PROFILE (facts learned about them, read by every
    agent) and — if this agent holds any — its OWN private notes (how it does its job). Both
    end with the standing instinct to keep memory updated.

    Assistant-side seam: build_system_prompt lives in the curry_leaves kernel and can't take a
    new layer from here, so we fold memory into the `instructions` string before constructing
    the Agent. Applies to both headless pool runs and chat (both go through _build_agent).
    Fails soft — a memory-store hiccup must never break a run.

    The profile is small + universally relevant → always injected. Per-agent notes are kept low
    by the high save bar, so we inject all of them too (capped).

    Both blocks are UNFILTERED by design: an agent is built before (and, in chat, independently
    of) the run's input, so there's no query to scope them by at this point. Scoping happens
    mid-run instead — `profile_read recall` / `remember recall` search memory by meaning on any
    turn, which is what reaches a fact the capped block left out."""
    held = set(tool_names or [])
    blocks: list[str] = []
    facts: list[dict] = []
    try:
        from curry_leaves_assistant.stores import profile_store

        # Tier 1 — PREFERENCES only. A preference is behavioural ("short answers", "bullets over
        # prose"): it shapes how any reply should be written, whatever the topic, so it earns a
        # place in every prompt. Facts don't — they're informational and only matter when the
        # question is about them, so they come through the situational block below.
        facts = profile_store.preferences_for_prompt()
        if facts:
            lines = [f"- {f['body']}" for f in facts]
            more = (" Something else about the user may be recorded — `profile_read` with action "
                    "`recall` searches it by meaning." if "profile_read" in held else "")
            blocks.append(
                "## How the user likes things done\n"
                "_Standing preferences, learned over time. Honor them in every reply. If one is "
                f"now wrong, call `update_profile` to correct it.{more}_\n" + "\n".join(lines))
    except Exception:
        pass
    # Tier 2 — SITUATIONAL facts. The profile above is what's true of the user always; this is
    # what's true of *what they just asked about*, pulled by meaning from every fact in the
    # bundle (including the ones filed with their subject, e.g. apps/cbm/facts/…). Nothing is
    # injected when nothing clears the relevance floor, so an unrelated question costs no tokens.
    try:
        if query:
            from curry_leaves_assistant.stores import profile_store

            hits = profile_store.relevant_for_prompt(query)
            # Don't repeat what the always-on block already carries.
            shown = {f.get("body") for f in (facts or [])}
            hits = [h for h in hits if h.get("body") not in shown]
            if hits:
                lines = [f"- {h['body']}" + (f"  _(about {h['path'].rsplit('/facts/', 1)[0]})_"
                                             if "/facts/" in (h.get("path") or "") else "")
                         for h in hits]
                blocks.append(
                    "## Relevant to this request\n"
                    "_Facts already recorded that bear on what was just asked. Treat them as "
                    "known; don't re-ask for them._\n" + "\n".join(lines))
    except Exception:
        pass
    try:
        if agent_id:
            from curry_leaves_assistant.stores import agent_memory_store

            notes = agent_memory_store.list_all(agent_id)[:12]
            if notes:
                lines = [f"- ({n['type']}) {n['body']}" for n in notes]
                more = (" Capped — `remember` with action `recall` searches all your notes by "
                        "meaning." if "remember" in held else "")
                blocks.append(
                    "## What you've learned (your own notes)\n"
                    "_Private to you — how you do your job. Honor them; call `remember` to "
                    f"correct one.{more}_\n" + "\n".join(lines))
    except Exception:
        pass
    # WHO the user is, straight from Settings → Identity (and the first-run wizard). This is
    # the one thing every agent should know before any memory exists — without it an
    # assistant opens by asking the user's name, which they already typed during setup.
    try:
        from curry_leaves_assistant.core import settings as app_settings

        ident = app_settings.identity_cfg()
        who: list[str] = []
        if (name := (ident.get("name") or "").strip()):
            who.append(f"- You are talking to **{name}**. Address them by name; never ask "
                       "what to call them.")
        if (work := (ident.get("work") or "").strip()):
            who.append(f"- What they do: {work}")
        if (hours := (ident.get("workingHours") or "").strip()):
            who.append(f"- Working hours: {hours}")
        picked = [USAGE_LABELS[u] for u in (ident.get("usage") or []) if u in USAGE_LABELS]
        if picked:
            who.append("- They set Curry Leaves up for: " + ", ".join(picked) + ".")
        if who:
            blocks.append("## Who you're helping\n" + "\n".join(who))
        # Free-form standing instructions the user wrote for how the app should behave.
        if (behavior := (ident.get("behavior") or "").strip()):
            blocks.append("## How the user wants you to work\n" + behavior)
    except Exception:
        pass
    instinct = (
        "\n\n_When you learn something durable, record it (only if you hold the tool): a fact or "
        "preference about the USER any assistant should know → `update_profile`; a convention "
        "specific to how YOU do your job → `remember`. A high bar keeps memory clean — don't "
        "record one-offs or guesses._"
    )
    ask_rule = ""
    if tool_names and "ask" in tool_names:
        # The `ask` tool renders a structured question card the user answers in place, and it
        # BLOCKS until they do. Models tend to also restate the question as prose in the same
        # turn — which double-asks and, since the tool already paused the run, adds noise. Make
        # the tool the single channel: ask via the tool, say nothing else, wait for the answer.
        ask_rule = (
            "\n\n_To ask the user something, use the `ask` tool and NOTHING ELSE that turn — do "
            "not also restate the question in your text reply. The tool shows the question and "
            "waits for the answer, which comes back to you as the tool result; continue from "
            "there. One question channel, no prose duplicates._"
        )
    prefix = ("\n\n".join(blocks) + "\n\n") if blocks else ""
    return f"{prefix}{instructions}{instinct}{ask_rule}"


def _scoped_skills(skill_names: list[str], agent_id: str | None = None):
    """Build the SkillRegistry an agent sees as system-prompt teasers (and can reach via
    skill://). The rule set, in order:

      • RETIRED learned skills are never included (they measured as unhelpful).
      • A LEARNED skill (has `appliesTo`) is included only if this agent is in its appliesTo
        (or appliesTo == "all") — this is what gives a skill learned from one agent's runs a
        BLAST RADIUS instead of leaking into every agent's prompt.
      • The agent's own explicitly-declared `skills` are always included.
      • If the agent declares nothing AND no learned skill targets it, return None so the
        Runner auto-discovers the plain (non-learned) skills exactly as before — preserving
        today's behavior for every seeded agent.

    Before this, `skills: []` meant "auto-discover EVERYTHING", so every learned skill polluted
    every agent. Now learned skills are scoped and seeded skills keep their old broad reach."""
    from curry_leaves.skills import SkillRegistry
    from curry_leaves_assistant.stores import skill_meta

    full = SkillRegistry(discover=True)
    declared = set(skill_names or [])
    targeted: set[str] = set()
    retired: set[str] = set()
    for sk in full.all():
        meta = skill_meta.read_meta(sk.name)
        status = meta.get("status")
        if status == "retired":
            retired.add(sk.name)
            continue
        applies = meta.get("appliesTo")
        if applies and agent_id:
            if applies == "all" or (isinstance(applies, list) and agent_id in applies):
                targeted.add(sk.name)

    wanted = (declared | targeted) - retired
    # Nothing scoped and no retirements to enforce → fall back to auto-discovery (old behavior).
    if not wanted and not retired:
        return None

    reg = SkillRegistry()
    if not declared and not targeted:
        # Agent declared nothing but there ARE retirements: auto-discover everything, minus the
        # retired ones (so a bad learned skill stops reaching even auto-discovery agents).
        for sk in full.all():
            if sk.name not in retired:
                reg._skills[sk.name] = sk
        return reg
    for nm in wanted:
        sk = full.get(nm)
        if sk is not None:
            reg._skills[nm] = sk
    return reg


def _model_id(runner) -> str | None:
    return getattr(getattr(getattr(runner, "agent", None), "model", None), "id", None)


def _serialize_messages(runner) -> list[dict]:
    """The full conversation the model saw — every user / assistant / tool_result message
    (text, thinking, tool calls, tool outputs). This is 'the whole content sent to the LLM'."""
    import json as _json
    out = []
    for m in (getattr(runner, "messages", None) or [])[-40:]:
        role = getattr(m, "role", "?")
        blocks = []
        for b in (getattr(m, "content", None) or []):
            bt = getattr(b, "type", None)
            if bt == "text":
                blocks.append(getattr(b, "text", "") or "")
            elif bt == "thinking":
                blocks.append("🧠 " + (getattr(b, "thinking", "") or ""))
            elif bt == "tool_call":
                args = _trunc(_json.dumps(getattr(b, "arguments", {}), ensure_ascii=False, default=str), 2000)
                blocks.append(f"→ call {getattr(b, 'name', 'tool')}({args})")
            else:
                blocks.append(str(b))
        item = {"role": role, "text": _trunc("\n".join(x for x in blocks if x), 4000)}
        if role == "tool_result":
            item["tool"] = getattr(m, "tool_name", "")
        out.append(item)
    return out


def _serialize_roster(runner) -> dict:
    """The tool + skill catalog actually advertised to the model this run — what the LLM
    could see in its system prompt. `tools` are always-on (schemas sent every turn);
    `deferred` are registered but hidden behind search_tools (name+teaser only until called);
    `skills` are the agent's skill teasers. This is the 'what we sent' companion to the
    system prompt, so the trace can show the full request surface, not just the instructions."""
    try:
        agent = getattr(runner, "agent", None)
        reg = getattr(agent, "tools", None)
        tools = sorted(getattr(t, "name", "") for t in reg.tools()) if reg else []
        advertised = [t for t in tools if reg and not reg.is_deferred(t)]
        deferred = [t for t in tools if reg and reg.is_deferred(t)]
        skills = getattr(runner, "_skills", None)
        skill_names = sorted(name for name, _ in skills.teasers()) if skills else []
        return {"tools": advertised, "deferred": deferred, "skills": skill_names}
    except Exception:
        return {"tools": [], "deferred": [], "skills": []}


async def _run_traced(agent_record: dict, user_input: str, *, surface: str,
                      extra_tools: list | None, output_type: type | None,
                      host=None, permission=None):
    """Shared traced run-to-completion body behind run_agent / run_agent_structured:
    opens an agent_run (or subagent_run, when already inside an agent) span, captures
    the run via a TracingHost, and returns the kernel's RunResult.

    `host`/`permission`: a headless run passes neither (auto behavior). A background run that
    should be able to ask a human passes a SuspendHost + PermissionEngine; the TracingHost
    composes it as `inner` (same pattern as stream_agent) so tracing AND approvals both work,
    and `permission` is forwarded to the runner (without it, the approval gate is bypassed)."""
    if _effective_provider(agent_record) == "copilot":
        await warm_copilot_models()
    from curry_leaves_assistant.core import trace_ctx

    from curry_leaves_assistant.agents.trace_host import TracingHost
    kind = "subagent_run" if trace_ctx.is_agent_context() else "agent_run"
    with trace_ctx.span(kind, agent_record.get("name") or agent_record["id"], attributes={
        "agentId": agent_record["id"], "surface": surface,
        "instructions": _trunc(agent_record.get("instructions", "")),
        "userInput": _trunc(user_input),
    }) as rs, trace_ctx.agent_scope(agent_record["id"]):
        thost = TracingHost(rs.trace_id, rs.span_id, inner=host, agent_id=agent_record["id"], surface=surface)
        runner, _, mcp_servers = await build_runner(agent_record, host=thost, permission=permission,
                                                    extra_tools=extra_tools, output_type=output_type,
                                                    query=_input_text(user_input))
        thost.model = _model_id(runner)
        rs.attr(model=thost.model)
        try:
            result = await runner.run(user_input)
        finally:
            thost.flush()
            rs.attr(tokensIn=thost.tokens_in, tokensOut=thost.tokens_out,
                    messages=_serialize_messages(runner), roster=_serialize_roster(runner))
            if mcp_servers:
                from curry_leaves_assistant.providers import mcp_client

                await mcp_client.close_servers(mcp_servers)
        rs.attr(output=_trunc(result.output_text))
        return result


async def run_agent(agent_record: dict, user_input: str, *, surface: str = "pool",
                    extra_tools: list | None = None, host=None, permission=None) -> str:
    """Run to completion and return the final assistant text. Traced: opens an agent_run (or
    subagent_run, when already inside an agent) span and captures the run via a TracingHost.

    `extra_tools`: pre-built tool instances available for just this run only.
    `host`/`permission`: pass a SuspendHost + PermissionEngine to let a background run ask a
    human (suspend/notify/resume); omit for a headless auto run."""
    result = await _run_traced(agent_record, user_input, surface=surface,
                               extra_tools=extra_tools, output_type=None,
                               host=host, permission=permission)
    return result.output_text


async def run_agent_structured(agent_record: dict, user_input: str, output_type: type, *,
                               surface: str = "pool", extra_tools: list | None = None,
                               host=None, permission=None):
    """Like run_agent, but the run must answer with JSON matching `output_type` (a pydantic
    model). Returns `(parsed, raw_text)` — `parsed` is the validated instance, or None when
    the reply still didn't validate after the kernel's built-in retries, so callers can fall
    back to shaping `raw_text` themselves (a tile should degrade, not blank out)."""
    result = await _run_traced(agent_record, user_input, surface=surface,
                               extra_tools=extra_tools, output_type=output_type,
                               host=host, permission=permission)
    return result.output, result.output_text


def _input_text(user_input) -> str:
    """Visible text of a run's user input — a plain string, or the text blocks of a
    multimodal UserMessage (images/files carry no text). Used for tracing only."""
    if isinstance(user_input, str):
        return user_input
    from curry_leaves.core.messages import text_of
    return text_of(getattr(user_input, "content", []) or [])


async def stream_agent(agent_record: dict, user_input, model_override: str | None = None,
                       session_id: str | None = None, host=None, permission=None,
                       thinking_effort: Effort | None = Effort.MEDIUM, autonomous: bool = False,
                       elide: bool = False,
                       surface: str = "chat", traced: bool = True, on_runner=None,
                       extra_tools: list | None = None, output_type: type | None = None,
                       promote: set[str] | None = None):
    """Yield smart-loop events for SSE. With a session_id the run is stateful (persisted).
    A host + permission engine enable interactive approvals / ask prompts mid-run. When traced,
    a TracingHost wrapping the (chat) host captures the conversation + approvals into a span.

    `on_runner`, if given, is called with the live curry-leaves Runner as soon as it's built —
    before the first event is yielded — so a caller (the chat API) can stash it somewhere
    reachable (keyed by run id) to later call `.steer()` / cancel the driving task for
    stop/steering support. Not stored on this generator itself since it can be torn down
    from a different asyncio Task than the one that started it.

    `extra_tools` / `output_type`: pass-throughs to build_runner so a STREAMING run can also
    have run-scoped tools (e.g. a tile's notify_user) and a structured `output_type` (the final
    reply is then JSON — the caller parses it; the run-level re-ask-on-invalid retry that
    run_agent_structured has is NOT applied on the stream path, so a structured streaming caller
    should validate + fall back on its own)."""
    if _effective_provider(agent_record) == "copilot":
        await warm_copilot_models()
    from curry_leaves_assistant.core import trace_ctx

    # Set/reset manually (not `with agent_scope()`) — this is an async generator that can be
    # closed from a different asyncio Task context, where a context-manager's reset() would
    # raise (see the open_span/close_span note below for the same issue on the trace span).
    agent_token = trace_ctx._current_agent_id.set(agent_record["id"])
    try:
        if not traced:  # e.g. ephemeral System-Prompt edits — no spans, but still ledger the tokens
            from curry_leaves_assistant.agents.trace_host import UsageHost
            uhost = UsageHost(agent_id=agent_record["id"], surface="ephemeral", inner=host)
            runner, session_store, mcp_servers = await build_runner(agent_record, model_override, session_id, host=uhost, permission=permission, thinking_effort=thinking_effort, autonomous=autonomous, elide=elide, extra_tools=extra_tools, output_type=output_type, query=_input_text(user_input), promote=promote)
            uhost.model = _model_id(runner)
            if on_runner is not None:
                on_runner(runner)
            yield _RunMeta(model=uhost.model or "")
            try:
                async for ev in runner.stream(user_input):
                    yield ev
            finally:
                if session_store is not None:
                    try:
                        await asyncio.shield(session_store.close())
                    except (asyncio.CancelledError, Exception):
                        pass
                if mcp_servers:
                    from curry_leaves_assistant.providers import mcp_client

                    try:
                        await asyncio.shield(mcp_client.close_servers(mcp_servers))
                    except (asyncio.CancelledError, Exception):
                        pass
            return
        from curry_leaves_assistant.agents.trace_host import TracingHost
        # Use open_span/close_span instead of `with trace_ctx.span()` because the context
        # manager calls _current.reset(token) in its finally, which crashes when the async
        # generator is closed from a different asyncio Task context (ValueError: wrong Context).
        rs, t0, started, parent_span_id = trace_ctx.open_span(
            "agent_run", agent_record.get("name") or agent_record["id"],
            attributes={"agentId": agent_record["id"], "surface": surface,
                        "instructions": _trunc(agent_record.get("instructions", "")),
                        "userInput": _trunc(_input_text(user_input))},
        )
        thost = TracingHost(rs.trace_id, rs.span_id, inner=host,
                             agent_id=agent_record["id"], surface=surface)
        runner, session_store, mcp_servers = await build_runner(agent_record, model_override, session_id, host=thost, permission=permission, thinking_effort=thinking_effort, autonomous=autonomous, elide=elide, extra_tools=extra_tools, output_type=output_type, query=_input_text(user_input), promote=promote)
        thost.model = _model_id(runner)
        rs.attr(model=thost.model)
        if on_runner is not None:
            on_runner(runner)
        yield _RunMeta(model=thost.model or "")
        try:
            async for ev in runner.stream(user_input):
                yield ev
        except BaseException as exc:
            rs.set_error(exc)
            raise
        finally:
            thost.flush()
            if session_store is not None:
                try:
                    await asyncio.shield(session_store.close())
                except (asyncio.CancelledError, Exception):
                    pass
            if mcp_servers:
                from curry_leaves_assistant.providers import mcp_client

                try:
                    await asyncio.shield(mcp_client.close_servers(mcp_servers))
                except (asyncio.CancelledError, Exception):
                    pass
            rs.attr(tokensIn=thost.tokens_in, tokensOut=thost.tokens_out,
                    messages=_serialize_messages(runner), roster=_serialize_roster(runner))
            trace_ctx.close_span(rs, t0, started, parent_span_id)
    finally:
        try:
            trace_ctx._current_agent_id.reset(agent_token)
        except ValueError:
            pass
