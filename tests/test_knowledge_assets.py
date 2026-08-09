"""Image attachments: the bytes and the filename both come from the browser, so the tests
that matter are the ones proving neither can steer a write outside the assets dir or get a
non-image served back from our origin."""
from __future__ import annotations

import base64

import pytest

from curry_leaves_assistant.core.paths import KNOWLEDGE_DIR
from curry_leaves_assistant.domain import knowledge_assets as assets

# 1x1 transparent PNG.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
GIF = b"GIF89a" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 8


def test_save_returns_bundle_relative_path_and_writes_the_file():
    r = assets.save(PNG, "diagram.png")
    assert r["path"].startswith("assets/") and r["path"].endswith(".png")
    assert r["mime"] == "image/png"
    assert (KNOWLEDGE_DIR / r["path"]).read_bytes() == PNG


@pytest.mark.parametrize("data,ext", [(PNG, "png"), (GIF, "gif"), (JPEG, "jpg"), (WEBP, "webp")])
def test_extension_comes_from_magic_bytes_not_the_supplied_name(data, ext):
    # Every upload claims ".png"; the stored extension must follow the CONTENT instead.
    r = assets.save(data, "whatever.png")
    assert r["path"].endswith(f".{ext}")
    assert assets.content_type(r["path"]) == r["mime"]


def test_rejects_non_image_bytes():
    # The stored-XSS case: HTML wearing an image filename must never land in the bundle.
    with pytest.raises(ValueError):
        assets.save(b"<html><script>alert(1)</script></html>", "pic.png")


def test_rejects_svg_because_it_can_carry_script():
    with pytest.raises(ValueError):
        assets.save(b"<svg xmlns='http://www.w3.org/2000/svg'><script/></svg>", "vector.svg")


def test_rejects_empty_and_oversize():
    with pytest.raises(ValueError):
        assets.save(b"", "empty.png")
    with pytest.raises(ValueError):
        assets.save(b"\x89PNG\r\n\x1a\n" + b"0" * assets.MAX_BYTES, "huge.png")


@pytest.mark.parametrize("hostile", [
    "../../../../etc/passwd.png",
    "/etc/passwd.png",
    "..\\..\\windows\\system32\\evil.png",
    "....//....//escape.png",
])
def test_hostile_filenames_cannot_escape_the_assets_dir(hostile):
    r = assets.save(PNG, hostile)
    written = (KNOWLEDGE_DIR / r["path"]).resolve()
    assert (KNOWLEDGE_DIR / "assets").resolve() == written.parent
    assert ".." not in r["path"]


def test_same_day_collisions_get_distinct_names():
    a = assets.save(PNG, "shot.png")
    b = assets.save(PNG, "shot.png")
    assert a["path"] != b["path"]
    assert (KNOWLEDGE_DIR / a["path"]).exists() and (KNOWLEDGE_DIR / b["path"]).exists()


def test_assets_dir_is_hidden_from_the_browsable_hub():
    """The tree/search/graph/health all gate on `_is_hub`. An attachments folder is not a page,
    so it must not appear as one — same reasoning that hides the `memory/` scopes."""
    from curry_leaves_assistant.domain import knowledge

    assets.save(PNG, "hidden.png")
    assert "assets" not in knowledge.list_dirs()
    assert not any((n.get("path") or "").startswith("assets/") for n in knowledge.list_notes())
    assert not knowledge._is_hub("assets")
    assert not knowledge._is_hub("assets/2026-08-08-x.png")
    # A note legitimately called "assets-review.md" must still be browsable — the guard is on the
    # folder, not on any path that happens to start with those letters.
    assert knowledge._is_hub("notes/assets-review.md")
    assert knowledge._is_hub("assets-review.md")


def test_unknown_extension_is_not_served_as_a_renderable_type():
    # Belt-and-braces for the GET route: anything we didn't assign downloads, never executes.
    assert assets.content_type("assets/x.html") == "application/octet-stream"
    assert assets.content_type("assets/x.svg") == "application/octet-stream"
