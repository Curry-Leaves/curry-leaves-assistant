"""Recording name/tag normalization on update, and the derived tag vocabulary.

Tag order is load-bearing — the Recordings rail groups a recording under tags[0] — so these
pin down that normalization dedupes *in place* rather than sorting.
"""
from __future__ import annotations

import shutil

import pytest

from curry_leaves_assistant.domain import recordings


@pytest.fixture(autouse=True)
def _clean_recordings():
    """Start from an empty recordings dir. The shared conftest fixture clears todos and pool
    items but not recordings, and tag_suggestions() scans every recording on disk — so drafts
    left by an earlier test would otherwise inflate the counts asserted here."""
    from curry_leaves_assistant.core.paths import RECORDINGS_DIR

    if RECORDINGS_DIR.exists():
        shutil.rmtree(RECORDINGS_DIR, ignore_errors=True)
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    yield


def test_tags_dedupe_case_insensitively_preserving_order():
    rec = recordings.create_draft(name="Standup")
    out = recordings.update(rec["id"], {"tags": ["Standup", "  urgent ", "standup", "", "URGENT"]})
    assert out is not None
    # First-seen spelling wins, blanks drop, and the lead tag stays the lead tag.
    assert out["tags"] == ["Standup", "urgent"]


def test_blank_name_falls_back_to_the_sentinel():
    rec = recordings.create_draft(name="Kickoff")
    blanked = recordings.update(rec["id"], {"name": "   "})
    trimmed = recordings.update(rec["id"], {"name": "  Kickoff  "})
    assert blanked is not None and trimmed is not None
    assert blanked["name"] == "Untitled recording"
    assert trimmed["name"] == "Kickoff"


def test_tag_suggestions_rank_by_use_then_alphabetically():
    # Tags are derived by scanning every recording in the (session-shared) data dir, so this
    # uses names unique to this test rather than asserting on the whole vocabulary.
    a = recordings.create_draft(name="A")
    b = recordings.create_draft(name="B")
    c = recordings.create_draft(name="C")
    recordings.update(a["id"], {"tags": ["rank-dup", "rank-zeta"]})
    recordings.update(b["id"], {"tags": ["rank-dup"]})
    recordings.update(c["id"], {"tags": ["Rank-Dup", "rank-alpha"]})  # same tag, other casing

    got = {t["tag"]: t["count"] for t in recordings.tag_suggestions()}
    # All three casings are one tag, labelled with the majority spelling (2× lower vs 1× title)
    # rather than whichever recording iterdir() happened to reach first.
    assert got["rank-dup"] == 3
    assert "Rank-Dup" not in got
    assert got["rank-alpha"] == 1 and got["rank-zeta"] == 1

    order = [t["tag"] for t in recordings.tag_suggestions() if t["tag"].startswith("rank-")]
    assert order[0] == "rank-dup"   # most-used first…
    assert order.index("rank-alpha") < order.index("rank-zeta")  # …then alphabetical


def _organizer(rec_id: str, patch: dict) -> str | None:
    meta = recordings.update(rec_id, patch)
    assert meta is not None
    return meta.get("organizer")


def test_organizer_must_be_an_attendee():
    """The organizer is a name from `attendees`, so the two can never drift apart — whichever
    field is patched, and whatever case it's typed in."""
    rec = recordings.create_draft(name="Sync")["id"]
    assert (recordings.get(rec) or {})["organizer"] is None

    # Snapped to the attendee-list spelling, so the UI can compare by equality.
    assert _organizer(rec, {"attendees": ["Priya", "Nambi"], "organizer": "nambi"}) == "Nambi"
    # Dropping the organizer from the attendee list clears the role rather than dangling.
    assert _organizer(rec, {"attendees": ["Priya"]}) is None
    # A name nobody in the meeting has is refused outright.
    assert _organizer(rec, {"organizer": "Nobody"}) is None


def test_organizer_reaches_agent_context():
    rec = recordings.create_draft(name="Sync")["id"]
    recordings.update(rec, {"attendees": ["Priya", "Sam"], "organizer": "Sam"})
    assert "Organizer" in recordings.agent_context(rec)


def test_tag_suggestions_ignores_non_recording_entries():
    """RECORDINGS_DIR also holds vocabulary.json — scanning must not trip over a plain file."""
    from curry_leaves_assistant.core.paths import RECORDINGS_DIR

    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    (RECORDINGS_DIR / "vocabulary.json").write_text('{"terms": {}}')
    recordings.tag_suggestions()  # must not raise
