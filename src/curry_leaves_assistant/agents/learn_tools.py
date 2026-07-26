"""Tools for the Skill Learner to CLOSE the learning loop.

The generic `skills` tool (skill_tools.py) can create/edit skill files, but a LEARNED skill
needs governance metadata the learner shouldn't hand-author as raw YAML: who it applies to
(scoping), that it's on trial, and the traces it came from (provenance). `learn_skill` writes
those correctly. `mark_reviewed` flags the episode so the same signal isn't re-processed.

Together with update_profile / remember (semantic routing) and the `skills` tool (editing an
existing skill's body), these let the Skill Learner turn one learning signal into the right kind of durable
memory — procedural, semantic, or lifecycle change — with the bookkeeping done right.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field
from curry_leaves.core.tools import ToolResult

from curry_leaves_assistant.stores import episode_store, skill_meta, skills_store


def _err(msg: str) -> ToolResult:
    return ToolResult(content=msg, is_error=True)


class LearnSkillTool:
    """Author or update a LEARNED skill with its governance metadata, or mark an episode
    reviewed. Action-dispatched: create | update | mark_reviewed."""
    name = "learn_skill"
    description = (
        "Turn a lesson into a governed LEARNED skill — action: create | update | mark_reviewed.\n"
        "• create: a NEW skill from a lesson. Give name (kebab-case), description (when to use "
        "it), body (short, imperative: what to do / avoid, in what situation), appliesTo (list "
        "of agent ids it should reach, e.g. ['kb-filer'] — NOT everyone; scope it to who it "
        "helps), and learnedFrom (the trace id(s) that taught it). It starts on `trial` and is "
        "injected ONLY into those agents' prompts until it proves out.\n"
        "• update: revise an EXISTING learned skill's body (pass name + body) — e.g. sharpen it "
        "after a second, similar signal. Its metrics/status are preserved.\n"
        "• mark_reviewed: record that you've reflected on an episode (agentId + jobId) so this "
        "signal isn't processed again. ALWAYS call this once you've decided — even if the "
        "verdict was 'nothing worth learning'.\n"
        "For a durable fact about the USER use update_profile; about how an agent does its own "
        "job use remember; only reach here for a repeatable PROCEDURE."
    )
    risk = "write"

    class Args(BaseModel):
        action: Literal["create", "update", "mark_reviewed"] = Field(description="create | update | mark_reviewed")
        # create / update
        name: str | None = Field(default=None, description="create/update: skill name, kebab-case.")
        description: str | None = Field(default=None, description="create: one sentence — what it's for and when to apply it.")
        body: str | None = Field(default=None, description="create/update: the procedure as short imperative markdown.")
        appliesTo: list[str] | None = Field(default=None, description="create: agent ids this skill should reach (scope it — don't default to everyone).")
        learnedFrom: list[str] | None = Field(default=None, description="create: trace id(s) this lesson was distilled from (provenance).")
        # mark_reviewed
        agentId: str | None = Field(default=None, description="mark_reviewed: the agent whose episode you reviewed.")
        jobId: str | None = Field(default=None, description="mark_reviewed: the job id of the reviewed episode.")

    schema = Args
    timeout = None

    async def run(self, args: "LearnSkillTool.Args", ctx, signal) -> ToolResult:
        if args.action == "mark_reviewed":
            if not args.agentId or not args.jobId:
                return _err("mark_reviewed requires `agentId` and `jobId`.")
            episode_store.mark_reviewed(args.agentId, args.jobId)
            return ToolResult(content=f"Marked episode {args.agentId}/{args.jobId} reviewed.")

        if args.action == "create":
            if not args.name or not args.description or not args.body:
                return _err("create requires `name`, `description`, and `body`.")
            from curry_leaves_assistant.core.store import now_iso
            try:
                skills_store.create_skill(args.name, args.description, args.body)
            except FileExistsError:
                return _err(f"Skill '{args.name}' already exists — use action='update'.")
            skill_meta.write_meta(args.name, {
                "status": "trial",
                "appliesTo": args.appliesTo or "all",
                "learnedFrom": args.learnedFrom or [],
                "learnedAt": now_iso(),
                "metrics": {"loads": 0, "successes": 0, "failures": 0},
            })
            scope = ", ".join(args.appliesTo) if args.appliesTo else "all agents"
            return ToolResult(content=f"Created learned skill '{args.name}' (trial, scoped to {scope}).")

        # update — replace the body, keep all governance frontmatter (status/metrics/provenance).
        if not args.name or not args.body:
            return _err("update requires `name` and `body`.")
        try:
            text = skills_store.read_file(args.name, "SKILL.md")
        except (FileNotFoundError, ValueError):
            return _err(f"No such skill '{args.name}' — use action='create'.")
        from curry_leaves_assistant.stores.agent_store import parse_frontmatter
        from curry_leaves_assistant.stores.skill_meta import _render
        fm, _old_body = parse_frontmatter(text)
        skills_store.write_file(args.name, "SKILL.md", _render(fm, args.body))
        return ToolResult(content=f"Updated learned skill '{args.name}'.")


LEARN_TOOLS = [LearnSkillTool()]
