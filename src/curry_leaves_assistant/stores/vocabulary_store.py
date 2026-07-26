"""Learned transcription vocabulary — terms mined from what the user *typed*, at
~/.curry-leaves/recordings/vocabulary.json.

Why notes: a user typing "Ilayanambi" or "mlx-whisper" into a meeting note is giving us
the correct spelling of exactly the word Whisper is most likely to mangle. That's a far
cleaner signal than mining the transcript, which is the thing being corrected — learning
from it would feed Whisper's own mistakes back to it.

The hard constraint is Whisper's prompt: `initial_prompt` is capped at 224 tokens and
**silently truncated from the front**. So this store can't just accumulate; it ranks and
evicts. `top_terms()` returns only what fits a caller-supplied budget, best-first.

Schema — {"terms": {"<lower>": {"term": str, "count": int, "last": iso, "blocked": bool}}}
keyed lowercase so "Priya"/"priya" are one entry; `term` keeps the best-seen casing.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from curry_leaves_assistant.core.paths import RECORDINGS_DIR
from curry_leaves_assistant.core.store import read_json, write_json

VOCAB_PATH = RECORDINGS_DIR / "vocabulary.json"

# A term must be seen this many times before it biases transcription. One appearance is
# as likely to be a typo as a real name, and a wrong term actively corrupts transcripts.
MIN_COUNT = 2

# Inflectional endings that mark an ordinary English word rather than a name. Used only to
# rescue the ambiguous case — a capitalized word at a sentence start, where the capital is
# positional. "Discussed"/"Probably"/"Meeting" get rejected; "Ilayanambi"/"Kubernetes"
# survive. Names ending this way (Cummings, Fielding) are the known false negative; they
# still get learned the moment they appear mid-sentence, which in practice they do.
_COMMON_SUFFIXES = ("ed", "ing", "ly", "tion", "sion", "ness", "ment", "able", "ible")

# Rough chars-per-token for the budget maths. Whisper's BPE averages ~4 chars/token on
# ordinary text, but proper nouns and hyphenated jargon fragment worse — 3 is the
# conservative side of that, which is the side we want when overflow is silent.
_CHARS_PER_TOKEN = 3

# Common words carry no recognition value (Whisper has them cold) and spending prompt
# budget on them evicts the terms that matter. Deliberately short: the shape rules below
# do most of the filtering, and a long list here is a maintenance burden that silently
# rots. This covers what actually survives those rules in practice — sentence-initial
# capitals and note-taking filler.
_STOPWORDS = {
    "a", "about", "after", "all", "also", "an", "and", "any", "are", "as", "ask", "asked",
    "at", "back", "be", "because", "been", "before", "but", "by", "call", "called", "can",
    "check", "could", "did", "do", "does", "done", "each", "email", "every", "final",
    "first", "for", "from", "get", "go", "going", "good", "got", "had", "has", "have",
    "he", "her", "here", "his", "how", "i", "if", "in", "into", "is", "it", "its", "just",
    "know", "last", "let", "like", "look", "made", "make", "may", "me", "meet", "meeting",
    "more", "most", "much", "must", "my", "need", "needs", "new", "next", "no", "not",
    "note", "notes", "now", "of", "off", "ok", "on", "one", "only", "or", "other", "our",
    "out", "over", "plan", "please", "put", "review", "said", "same", "see", "send",
    "set", "she", "should", "so", "some", "soon", "still", "such", "sure", "take",
    "talk", "team", "than", "that", "the", "their", "them", "then", "there",
    "these", "they", "thing", "things", "think", "this", "those", "time", "to", "today",
    "todo", "tomorrow", "too", "try", "up", "us", "use", "very", "want", "was", "we",
    "week", "well", "were", "what", "when", "where", "which", "who", "why", "will",
    "with", "work", "would", "yes", "yet", "you", "your",
}

# Tokens worth keeping: a word, optionally hyphenated/dotted/underscored (mlx-whisper,
# node.js, snake_case), or an ALLCAPS acronym. Trailing punctuation is stripped by the
# split, and possessives are handled in _normalize.
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-._][A-Za-z0-9]+)*")

# The NoteComposer prefixes pinned notes with an elapsed stamp — "[04:12] text". That
# marker is ours, not the user's words, so it never becomes vocabulary.
_STAMP_RE = re.compile(r"^\s*\[\d{1,2}:\d{2}(?::\d{2})?\]\s*", re.MULTILINE)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize(tok: str) -> str:
    """Strip a possessive tail so "Priya's" and "Priya" are the same term."""
    for suffix in ("'s", "’s"):
        if tok.endswith(suffix):
            return tok[: -len(suffix)]
    return tok


def _is_candidate(tok: str, at_sentence_start: bool) -> bool:
    """Does this token look like a name/jargon term rather than an ordinary word?

    Kept mechanical on purpose — notes are short, so an LLM pass would cost a model call
    per recording to decide something these rules get right, and rules stay inspectable
    when a bad term shows up in the list.
    """
    if len(tok) < 3 or len(tok) > 40:
        return False
    if tok.lower() in _STOPWORDS:
        return False
    if tok.isdigit():
        return False
    # Internal punctuation or digits → jargon/product name (mlx-whisper, gpt-4, node.js).
    if any(c in tok for c in "-._") or any(c.isdigit() for c in tok):
        return True
    # ALLCAPS → acronym (OKF, API). Length-capped above, so it isn't shouted prose.
    if tok.isupper():
        return True
    # Internal capital → CamelCase product name (JavaScript, PyTorch).
    if any(c.isupper() for c in tok[1:]):
        return True
    # Plain Capitalized word — the rule that catches names. Mid-sentence the capital is
    # itself the signal, so take it.
    if not tok[0].isupper():
        return False
    if not at_sentence_start:
        return True
    # At a sentence/line start the capital is positional and tells us nothing. Notes very
    # often *open* with the person they're about ("Ilayanambi to sync…"), so rather than
    # discard the position outright, fall back to shape: ordinary English words carry
    # inflectional endings that names almost never do. That separates "Discussed" and
    # "Probably" from "Ilayanambi" and "Kubernetes" without needing a full dictionary.
    if len(tok) < 5:
        return False
    return not tok.lower().endswith(_COMMON_SUFFIXES)


def extract_terms(text: str) -> list[str]:
    """Mine candidate vocabulary terms from a block of user-written text."""
    if not text or not text.strip():
        return []
    text = _STAMP_RE.sub("", text)
    found: dict[str, str] = {}  # lower -> best casing seen
    for line in text.splitlines():
        # A newline or sentence end means the next token's capital is positional, not a
        # signal. Track that so "Meeting with Priya" yields Priya but not Meeting.
        at_start = True
        for raw in line.split():
            tok_match = _TOKEN_RE.match(raw)
            ends_sentence = raw.endswith((".", "!", "?", ":", ";"))
            if not tok_match:
                at_start = at_start or ends_sentence
                continue
            tok = _normalize(tok_match.group(0))
            if _is_candidate(tok, at_start):
                key = tok.lower()
                # Prefer the casing with a capital — "Priya" over a stray "priya".
                if key not in found or (tok[:1].isupper() and not found[key][:1].isupper()):
                    found[key] = tok
            at_start = ends_sentence
    return list(found.values())


def read_all() -> dict:
    data = read_json(VOCAB_PATH, {})
    if not isinstance(data.get("terms"), dict):
        data["terms"] = {}
    return data


def _write(data: dict) -> None:
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    write_json(VOCAB_PATH, data)


def learn(text: str, source: str | None = None) -> list[str]:
    """Mine `text` and fold the terms into the store. Returns the terms seen this pass.

    `source` (a recording id) makes counting **idempotent per source**: a term already
    counted for that recording is not counted again. This matters because notes are saved
    by read-modify-write — the live note composer re-sends the whole accumulated note on
    every keystroke-batch, so without this a term typed early would rack up dozens of
    sightings from a single meeting.

    That keeps `count` meaning "seen in N distinct recordings", which is what makes
    MIN_COUNT a real confidence bar rather than a measure of how much someone typed.
    Callers with no source (a bare text pass) fall back to counting once per call.
    """
    terms = extract_terms(text)
    if not terms:
        return []
    data = read_all()
    store = data["terms"]
    now = _now()
    for t in terms:
        key = t.lower()
        cur = store.get(key)
        if cur is None:
            store[key] = {"term": t, "count": 1, "last": now, "blocked": False,
                          "sources": [source] if source else []}
            continue
        seen = cur.setdefault("sources", [])
        if source and source in seen:
            cur["last"] = now  # still fresh, just not a new sighting
        else:
            cur["count"] = int(cur.get("count", 0)) + 1
            cur["last"] = now
            if source:
                seen.append(source)
                del seen[:-50]  # bound the tail; only recent provenance is useful
        if t[:1].isupper() and not str(cur.get("term", ""))[:1].isupper():
            cur["term"] = t
    _write(data)
    return terms


def _rank_key(rec: dict) -> tuple:
    """Frequency first, then recency. Ties broken by term so output is stable."""
    return (-int(rec.get("count", 0)), str(rec.get("last", "")) or "", str(rec.get("term", "")))


def top_terms(max_tokens: int) -> list[str]:
    """Best learned terms that fit `max_tokens` of Whisper prompt budget, best-first.

    Ranked by count then recency, so a term used across many meetings outranks a recent
    one-off. Callers get a bounded list rather than everything — overflowing the prompt
    truncates silently, dropping terms nobody chose to lose.
    """
    if max_tokens <= 0:
        return []
    ranked = sorted(
        (r for r in read_all()["terms"].values()
         if not r.get("blocked") and int(r.get("count", 0)) >= MIN_COUNT),
        key=_rank_key,
    )
    out: list[str] = []
    used = 0
    for rec in ranked:
        term = str(rec.get("term") or "").strip()
        if not term:
            continue
        cost = max(1, (len(term) + _CHARS_PER_TOKEN) // _CHARS_PER_TOKEN)  # +1 for the space
        if used + cost > max_tokens:
            break
        out.append(term)
        used += cost
    return out


def list_terms() -> list[dict]:
    """Every learned term, best-first, for the Settings list. Includes below-threshold
    and blocked ones so the user can see what's pending and what they've removed."""
    return [
        {"term": str(r.get("term") or ""), "count": int(r.get("count", 0)),
         "last": str(r.get("last") or ""), "blocked": bool(r.get("blocked")),
         "active": not r.get("blocked") and int(r.get("count", 0)) >= MIN_COUNT}
        for r in sorted(read_all()["terms"].values(), key=_rank_key)
        if str(r.get("term") or "").strip()
    ]


def block(term: str) -> None:
    """Stop a term biasing transcription, permanently.

    Blocked rather than deleted: a plain delete would be undone by the next note that
    mentions it, so "remove" has to mean "and stay removed".
    """
    data = read_all()
    rec = data["terms"].get(term.lower())
    if not rec:
        return
    rec["blocked"] = True
    _write(data)


def unblock(term: str) -> None:
    data = read_all()
    rec = data["terms"].get(term.lower())
    if not rec:
        return
    rec["blocked"] = False
    _write(data)


def clear() -> None:
    _write({"terms": {}})
