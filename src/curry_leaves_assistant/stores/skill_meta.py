"""Lifecycle + provenance for LEARNED skills, stored in the SKILL.md frontmatter itself.

A seeded skill needs no lifecycle — it's always live. A skill the Skill Learner writes gets
extra frontmatter so the system can govern it: WHO it applies to, whether it's still on trial,
where it came from, and how it's performed. Keeping this in the SKILL.md (not a sidecar) means
a learned skill is still one portable file, and the provenance travels with it.

Frontmatter keys this module owns (all optional; absent ⇒ a hand-made/seeded skill):
  status      trial | proven | retired   — trial + retired are gated differently (see below)
  appliesTo   [agentId, ...] | "all"     — which agents get this skill's teaser (scoping)
  learnedFrom [traceId, ...]             — the evidence it was distilled from
  learnedAt   ISO timestamp
  metrics     {loads, successes, failures}  — updated post-run by skill correlation

Distribution rule (consumed by agent_engine._scoped_skills):
  • status == retired            → never injected
  • appliesTo present            → injected only for agents it lists (or "all")
  • no appliesTo (seeded/manual) → behaves as before (auto-discovered for everyone)
"""
from __future__ import annotations

# NOTE: use agent_store.parse_frontmatter / render_frontmatter (real yaml.safe_load/dump), NOT
# the kernel's curry_leaves.util.frontmatter — the kernel parser is a naive line reader that
# flattens nested YAML (lists like appliesTo, the metrics dict) into empty strings / hoisted
# keys. Learned-skill frontmatter is genuinely nested, so it needs a real YAML parser to read
# and write.
from curry_leaves_assistant.stores.agent_store import parse_frontmatter, render_frontmatter
from curry_leaves_assistant.stores import skills_store

_LIFECYCLE_KEYS = ("status", "appliesTo", "learnedFrom", "learnedAt", "metrics")


def read_meta(skill_name: str) -> dict:
    """The lifecycle frontmatter of a skill (empty dict for a plain seeded/manual skill)."""
    try:
        text = skills_store.read_file(skill_name, "SKILL.md")
    except (FileNotFoundError, ValueError):
        return {}
    meta, _ = parse_frontmatter(text)
    return {k: meta[k] for k in _LIFECYCLE_KEYS if k in meta} | {
        "name": meta.get("name") or skill_name,
        "description": meta.get("description") or "",
    }


def write_meta(skill_name: str, patch: dict) -> None:
    """Merge `patch` into a skill's frontmatter, preserving its body. Only called for learned
    skills (seeded ones are never mutated by the learner)."""
    text = skills_store.read_file(skill_name, "SKILL.md")
    meta, body = parse_frontmatter(text)
    meta.update(patch)
    skills_store.write_file(skill_name, "SKILL.md", _render(meta, body))


def record_run(skill_names: list[str], *, success: bool) -> None:
    """After a run, bump loads/successes/failures on each skill that was loaded during it.
    Only touches skills that already carry a metrics block (i.e. learned ones) — a seeded
    skill has no lifecycle to measure."""
    for name in skill_names:
        meta = read_meta(name)
        if "status" not in meta and "metrics" not in meta:
            continue  # not a learned skill — nothing to measure
        m = dict(meta.get("metrics") or {})
        m["loads"] = int(m.get("loads", 0)) + 1
        key = "successes" if success else "failures"
        m[key] = int(m.get(key, 0)) + 1
        try:
            write_meta(name, {"metrics": m})
        except (FileNotFoundError, ValueError):
            pass


def lifecycle_sweep() -> dict:
    """Promote/retire learned skills by their measured record. Mechanical (no LLM):
      • trial + ≥3 loads + 0 failures + successes>0  → proven
      • any learned skill whose failures ≥ successes and loads ≥ 4 → retired (it's hurting)
    Returns a small report. Called from the nightly maintenance pass."""
    promoted, retired = [], []
    for entry in skills_store.list_skills():
        name = entry["name"]
        meta = read_meta(name)
        status = meta.get("status")
        if status not in ("trial", "proven"):
            continue
        m = meta.get("metrics") or {}
        loads = int(m.get("loads", 0))
        succ = int(m.get("successes", 0))
        fail = int(m.get("failures", 0))
        if status == "trial" and loads >= 3 and fail == 0 and succ > 0:
            write_meta(name, {"status": "proven"})
            promoted.append(name)
        elif loads >= 4 and fail >= succ:
            write_meta(name, {"status": "retired"})
            retired.append(name)
    return {"promoted": promoted, "retired": retired}


def learned_skills() -> list[dict]:
    """Every skill carrying lifecycle frontmatter (for the Learned-skills UI)."""
    out = []
    for entry in skills_store.list_skills():
        meta = read_meta(entry["name"])
        if meta.get("status") or meta.get("learnedFrom"):
            out.append(meta)
    return out


def _render(meta: dict, body: str) -> str:
    # name + description FIRST and on their own lines: the kernel's SkillRegistry reads teasers
    # with a naive line-parser, so those two must stay simple top-level scalars it can find
    # before any nested block (appliesTo/metrics) appears.
    ordered = {}
    for k in ("name", "description"):
        if k in meta:
            ordered[k] = meta[k]
    for k, v in meta.items():
        if k not in ordered:
            ordered[k] = v
    return render_frontmatter(ordered, body)
