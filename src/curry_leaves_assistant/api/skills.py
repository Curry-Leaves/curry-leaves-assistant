"""Skills (procedural memory — "how we do things", vs. the factual Knowledge Base).

Full directory management: browse the tree, read/write/delete any file,
create/delete whole skills — not just the flattened SKILL.md body."""
from __future__ import annotations

from fastapi import APIRouter, Response
from pydantic import BaseModel

from curry_leaves_assistant.stores import skills_store

router = APIRouter(tags=["skills"])


class SkillFileBody(BaseModel):
    path: str
    content: str


class SkillDirBody(BaseModel):
    path: str


class SkillCreateBody(BaseModel):
    name: str
    description: str = ""
    body: str = ""


@router.get("/skills")
def skills_list():
    return {"skills": skills_store.list_skills()}


@router.get("/skills/{name}/tree")
def skills_tree(name: str):
    try:
        return {"tree": skills_store.tree(name)}
    except FileNotFoundError:
        return Response(status_code=404)


@router.get("/skills/{name}/file")
def skills_read_file(name: str, path: str):
    try:
        return {"content": skills_store.read_file(name, path)}
    except (FileNotFoundError, ValueError) as exc:
        return Response(content=str(exc), status_code=404 if isinstance(exc, FileNotFoundError) else 400)
    except UnicodeDecodeError:
        return Response(content="binary file — cannot display as text", status_code=415)


@router.put("/skills/{name}/file")
def skills_write_file(name: str, body: SkillFileBody):
    try:
        skills_store.write_file(name, body.path, body.content)
        return {"ok": True}
    except ValueError as exc:
        return Response(content=str(exc), status_code=400)


@router.post("/skills/{name}/dir")
def skills_make_dir(name: str, body: SkillDirBody):
    try:
        skills_store.make_dir(name, body.path)
        return {"ok": True}
    except ValueError as exc:
        return Response(content=str(exc), status_code=400)


@router.delete("/skills/{name}/file")
def skills_delete_path(name: str, path: str):
    try:
        skills_store.delete_path(name, path)
        return {"ok": True}
    except ValueError as exc:
        return Response(content=str(exc), status_code=400)


@router.post("/skills")
def skills_create(body: SkillCreateBody):
    try:
        skills_store.create_skill(body.name, body.description, body.body)
        return {"ok": True}
    except FileExistsError:
        return Response(content=f"a skill named {body.name!r} already exists", status_code=409)
    except ValueError as exc:
        return Response(content=str(exc), status_code=400)


@router.delete("/skills/{name}")
def skills_delete(name: str):
    return {"ok": skills_store.delete_skill(name)}


# ─── learned skills (the self-improvement loop's output) ────────────────────────
@router.get("/skills/learned")
def learned_skills():
    """Every skill the Skill Learner authored — with lifecycle status, who it applies to, the
    traces it came from, and its measured metrics. The trust surface for autonomous learning."""
    from curry_leaves_assistant.stores import skill_meta
    return {"skills": skill_meta.learned_skills()}


class LifecycleBody(BaseModel):
    status: str  # proven | trial | retired


@router.post("/skills/{name}/lifecycle")
def set_lifecycle(name: str, body: LifecycleBody):
    """Manually override a learned skill's status — approve (proven), pause (trial), or kill
    (retired) it. This is the human's control over what the learner produced."""
    from curry_leaves_assistant.stores import skill_meta
    if body.status not in ("proven", "trial", "retired"):
        return Response(content="status must be proven | trial | retired", status_code=400)
    if not skill_meta.read_meta(name).get("status"):
        return Response(content="not a learned skill", status_code=404)
    skill_meta.write_meta(name, {"status": body.status})
    return {"ok": True, "status": body.status}
