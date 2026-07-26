"""Local text embeddings via all-MiniLM-L6-v2 (sentence-transformers/all-MiniLM-L6-v2, Apache-2.0).

The embedder behind every vector + hybrid search in the app — the Knowledge Hub bundle and the
memory scopes (user profile, per-agent notes) alike. It lives in `core` precisely because both
`domain` and `stores` need it: it's a capability over `core.paths`, not domain logic.

Same shape as ``domain/tts.py`` and ``domain/transcribe.py``: weights live under
``~/.curry-leaves/models/minilm/`` (covered by the same backup exclusion), one warm handle is
kept, and every entry point is BLOCKING — call from a thread if you're on the event loop.

Runs in-process on torch (Metal/MPS when available, else CPU); nothing leaves the machine, and no
API key is involved. torch + transformers already ship with the backend for the whisper/docling
stack, so this adds no new dependency — only a ~90 MB weights download on first use.

384 dimensions, 256-token window. Mean-pooled over the last hidden state and L2-normalized, which
is what MiniLM was trained for (cosine similarity over normalized mean-pooled vectors).

``embed()`` is the ``Callable[[list[str]], list[list[float]]]`` cl_memory's ``VectorIndex`` wants.
It raises if the model can't load; the KB shim treats that as "no vector tier" and stays on BM25.
"""
from __future__ import annotations

import os
import threading
from typing import Any

from curry_leaves_assistant.core.paths import MODELS_DIR

MINILM_REPO = "sentence-transformers/all-MiniLM-L6-v2"
MODEL_NAME = "all-MiniLM-L6-v2"
DIM = 384
MAX_TOKENS = 256          # the model's trained window; longer text is truncated
_BATCH = 32

_lock = threading.Lock()
_handle: tuple[Any, Any, Any] | None = None   # (tokenizer, model, torch)


def model_dir():
    return MODELS_DIR / "minilm"


def is_downloaded() -> bool:
    d = model_dir()
    if not d.is_dir():
        return False
    files = {f.name for f in d.rglob("*")}
    has_weights = any(f.endswith((".bin", ".safetensors")) for f in files)
    return has_weights and "config.json" in files


def download_model() -> bool:
    """Fetch the MiniLM weights into ~/.curry-leaves/models/minilm/ (~90 MB)."""
    from huggingface_hub import snapshot_download

    snapshot_download(
        repo_id=MINILM_REPO,
        local_dir=str(model_dir()),
        # Skip the framework copies we can't use — the repo also ships TF/ONNX/OpenVINO
        # variants that would triple the download for nothing.
        ignore_patterns=["*.h5", "*.ot", "*.msgpack", "onnx/*", "openvino/*"],
    )
    return is_downloaded()


def available() -> bool:
    """True when this backend could embed — i.e. torch + transformers import. Doesn't touch the
    network or load weights, so it's cheap enough for a boot-time probe."""
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
    except Exception:
        return False
    return True


def enabled() -> bool:
    """False when the user has forced everything back to keyword-only search."""
    return os.environ.get("CURRY_LEAVES_VECTOR_SEARCH", "1").strip().lower() not in (
        "0", "false", "no", "off")


def vector_ready() -> bool:
    """True when recall can match on MEANING — the user hasn't opted out, the libraries import,
    and the weights are on disk. The one truth every scope checks."""
    return enabled() and available() and is_downloaded()


def vector_index() -> Any:
    """A ``cl_memory.VectorIndex`` over this embedder, or None to stay keyword-only.

    The single gate every vector-capable scope shares (the Knowledge Hub bundle and each memory
    scope). Three conditions, all cheap and offline: the user hasn't opted out, the libraries
    import, and the weights are already on disk.

    The weights check is what keeps the *first* search fast: cl_memory embeds lazily on the
    search path, so handing back a VectorIndex before the ~90 MB download exists would put that
    download in front of a user's query. `warm()` fetches in the background at boot instead, and
    the tier comes up on the next start.
    """
    if not vector_ready():
        return None
    from cl_memory import VectorIndex

    return VectorIndex(embed=embed, model=MODEL_NAME, dim=DIM)


def _load() -> tuple[Any, Any, Any]:
    """Build (tokenizer, model, torch) once, downloading weights if absent. Caller holds _lock."""
    global _handle
    if _handle is not None:
        return _handle
    import torch
    from transformers import AutoModel, AutoTokenizer

    if not is_downloaded():
        download_model()
    src = str(model_dir())
    tok = AutoTokenizer.from_pretrained(src)
    mdl = AutoModel.from_pretrained(src)
    mdl.eval()
    # MPS (Apple Silicon) is a big win here and is already used by the rest of the ML stack;
    # fall back to CPU everywhere else. Never CUDA-assume — this ships on laptops.
    if torch.backends.mps.is_available():
        mdl = mdl.to("mps")
    _handle = (tok, mdl, torch)
    return _handle


def warm() -> None:
    """Fetch weights (if absent) and build the model, so the first search doesn't pay for it.
    Blocking and best-effort: call from a thread off the boot path, and let any failure fall
    through — the KB just stays on keyword search."""
    if not available():
        return
    try:
        with _lock:
            _load()
    except Exception as exc:
        print(f"[embeddings] warm failed ({exc}) — knowledge search stays on keyword mode",
              flush=True)


def reset_model() -> None:
    """Drop the warm handle (tests / freeing memory)."""
    global _handle
    with _lock:
        _handle = None


def embed(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts -> one 384-dim L2-normalized vector each.

    The ``embed=`` callable for cl_memory's VectorIndex. Blocking (torch inference); cl_memory
    calls it lazily from the search path, so the caller should already be off the event loop.
    Raises if the model can't load — the shim treats that as "no vector tier"."""
    if not texts:
        return []
    with _lock:
        tok, mdl, torch = _load()
        out: list[list[float]] = []
        for i in range(0, len(texts), _BATCH):
            chunk = texts[i:i + _BATCH]
            enc = tok(chunk, padding=True, truncation=True, max_length=MAX_TOKENS,
                      return_tensors="pt")
            enc = {k: v.to(mdl.device) for k, v in enc.items()}
            with torch.no_grad():
                hidden = mdl(**enc).last_hidden_state          # (batch, tokens, 384)
            # Mean-pool over real tokens only: padding must not drag the vector toward zero.
            mask = enc["attention_mask"].unsqueeze(-1).expand(hidden.size()).float()
            pooled = (hidden * mask).sum(1) / mask.sum(1).clamp(min=1e-9)
            # L2-normalize so cosine == dot product and every vector is comparable.
            pooled = torch.nn.functional.normalize(pooled, p=2, dim=1)
            out.extend(pooled.cpu().tolist())
        return out
