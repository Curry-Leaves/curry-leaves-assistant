"""Skills as manageable files: browse, read, write, and delete anything inside a
skill's directory (SKILL.md, references/, scripts/, assets — arbitrary tree, not
just the flattened SKILL.md body that SkillRegistry.body() returns for agent
consumption). Mirrors the path-traversal guard in curry_leaves.skills._read_asset,
but for read AND write.
"""
from __future__ import annotations

from pathlib import Path

from curry_leaves.util.paths import home


def skills_dir() -> Path:
    """Same user skills dir the curry_leaves kernel's SkillRegistry discovers from."""
    d = Path(home()) / "skills"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _skill_dir(name: str) -> Path:
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise ValueError(f"invalid skill name: {name!r}")
    return skills_dir() / name


def _safe_path(name: str, rel: str) -> Path:
    """Resolve `rel` inside the skill's directory; raise if it would escape."""
    base = _skill_dir(name).resolve()
    target = (base / rel).resolve()
    if target != base and base not in target.parents:
        raise ValueError(f"path escapes skill directory: {rel!r}")
    return target


def list_skills() -> list[dict]:
    """Every skill dir under skills_dir() that has a SKILL.md, with its frontmatter."""
    from curry_leaves.util.frontmatter import parse_frontmatter
    out = []
    for d in sorted(skills_dir().iterdir()):
        md = d / "SKILL.md"
        if not d.is_dir() or not md.is_file():
            continue
        meta, _ = parse_frontmatter(md.read_text(encoding="utf-8"))
        out.append({
            "name": meta.get("name") or d.name,
            "description": meta.get("description") or "",
            "hide": (meta.get("hide") or "").lower() in ("true", "1", "yes") if isinstance(meta.get("hide"), str) else bool(meta.get("hide")),
        })
    return out


def tree(name: str) -> list[dict]:
    """Flat list of every file under the skill's directory: {path, isDir, size}."""
    base = _skill_dir(name)
    if not base.is_dir():
        raise FileNotFoundError(name)
    out = []
    for p in sorted(base.rglob("*")):
        rel = p.relative_to(base).as_posix()
        out.append({"path": rel, "isDir": p.is_dir(), "size": p.stat().st_size if p.is_file() else None})
    return out


def read_file(name: str, rel: str) -> str:
    target = _safe_path(name, rel)
    if not target.is_file():
        raise FileNotFoundError(rel)
    return target.read_text(encoding="utf-8")


def write_file(name: str, rel: str, content: str) -> None:
    target = _safe_path(name, rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def make_dir(name: str, rel: str) -> None:
    _safe_path(name, rel).mkdir(parents=True, exist_ok=True)


def delete_path(name: str, rel: str) -> None:
    """Delete a file, or a directory and everything under it."""
    target = _safe_path(name, rel)
    if target == _skill_dir(name).resolve():
        raise ValueError("cannot delete the skill's root via delete_path — use delete_skill")
    if target.is_dir():
        import shutil
        shutil.rmtree(target, ignore_errors=True)
    elif target.exists():
        target.unlink()


def create_skill(name: str, description: str, body: str) -> None:
    d = _skill_dir(name)
    if d.exists():
        raise FileExistsError(name)
    # Build the frontmatter with a real YAML dumper, NOT an f-string: a description
    # like "Route KB files: pick the folder by topic" has a colon-space that raw
    # interpolation writes unquoted, producing `description: Route KB files: ...`
    # which then throws yaml.ScannerError ("mapping values are not allowed here")
    # on every later read — poisoning _scoped_skills and crashing whole chat runs.
    # render_frontmatter (yaml.safe_dump) quotes such values correctly.
    from curry_leaves_assistant.stores.agent_store import render_frontmatter
    d.mkdir(parents=True)
    content = render_frontmatter({"name": name, "description": description}, body)
    (d / "SKILL.md").write_text(content, encoding="utf-8")


def delete_skill(name: str) -> bool:
    d = _skill_dir(name)
    if not d.is_dir():
        return False
    import shutil
    shutil.rmtree(d, ignore_errors=True)
    return True


# ─── Default skills (seeded on first run from the bundled seeds/skills/) ───────
SEED_SKILLS_DIR = Path(__file__).resolve().parents[1] / "seeds" / "skills"


def seed_default_skills() -> None:
    """Copy every bundled seed skill (seeds/skills/<name>/ — SKILL.md plus any starter
    files, e.g. skill-learner's index.md) that isn't on disk yet. Skills are
    user-manageable (Feature: Skills page) so, like the default agents, each is seeded
    once and never touched again — an existing SKILL.md (including user edits) leaves
    that whole skill alone. Delete the skill and restart to reseed the current
    built-in version."""
    import shutil
    seeded = 0
    for src in sorted(SEED_SKILLS_DIR.iterdir()):
        if not src.is_dir() or not (src / "SKILL.md").is_file():
            continue
        dst = _skill_dir(src.name)
        if (dst / "SKILL.md").exists():
            continue
        shutil.copytree(src, dst, dirs_exist_ok=True)
        seeded += 1
    if seeded:
        print(f"[skills] seeded {seeded} default skill(s)", flush=True)
