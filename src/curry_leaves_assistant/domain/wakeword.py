"""Wake-word model management — weights on disk, served to the renderer.

Detection itself runs client-side (onnxruntime-web in a Worker); this module only
owns the weights. Same shape as tts.py / core.embeddings: model_dir / is_downloaded /
download / warm, with the files under ~/.curry-leaves/models/wakeword/ so api/backup.py's
`models` exclusion already keeps re-downloadable weights out of user backups.

The model is a single fused ONNX graph:

    16 kHz f32 PCM [1, 31840] → wakeword_allinone.onnx → logits [1, n]

The melspectrogram, the openWakeWord embedding, and the trained classifier are fused
into one file — the renderer only ever loads and runs this one graph. A sidecar
`<name>.onnx.json` carries the phrase labels and per-class thresholds.

Weights come from the project's own Hugging Face repo. It ships Apache-2.0 (both the
openWakeWord embedding it embeds and the trained classifier), so unlike the old
CC BY-NC-SA pretrained heads it is usable commercially with no swap needed.

A user can still drop their own fused `<name>.onnx` (+ optional `.onnx.json`) into the
models dir and it is picked up by the directory scan below — no code change, no config
edit — as long as it shares this input/output shape.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import urllib.request
from pathlib import Path

from curry_leaves_assistant.core.paths import MODELS_DIR
from curry_leaves_assistant.core.settings import wakeword_cfg

# Raw-bytes host for the fused model + its sidecar. `resolve/main` serves the file
# content directly (not the HTML page); pinned to a repo we own so the asset set is stable.
HF_BASE = "https://huggingface.co/ilayanambi/curry-leaves-open-wake-word-model/resolve/main"

# The fused model and its metadata sidecar. Downloaded together; the sidecar carries the
# phrase labels + per-class thresholds the renderer fires on.
MODEL_FILE = "wakeword_allinone.onnx"
# The sidecar is named `multi.onnx.json` in the HF repo, but we store it locally as
# `<model>.onnx.json` — the `<file>.json` convention head_meta() and the dir scan use for
# every model, builtin or user-supplied. Keeping one convention means no special-casing.
REMOTE_SIDECAR = "multi.onnx.json"
LOCAL_SIDECAR = f"{MODEL_FILE}.json"

# The built-in model, downloadable by id. A user-supplied fused model is discovered by
# scanning the models dir instead.
BUILTIN: dict[str, str] = {
    "id": "curry_leaves",
    "label": "Curry Leaves",
    "file": MODEL_FILE,
    "sidecar": LOCAL_SIDECAR,
}


def model_dir() -> Path:
    return MODELS_DIR / "wakeword"


def is_downloaded(filename: str) -> bool:
    p = model_dir() / filename
    return p.is_file() and p.stat().st_size > 0


def available() -> bool:
    """True when at least one fused model is on disk — what the frontend gates the
    Settings toggle and the Ask AI listener on."""
    return any(h["downloaded"] for h in list_heads())


def resolve_path(filename: str) -> Path | None:
    """Map a requested filename to a path inside the models dir, or None.

    `filename` arrives from the renderer, so containment is enforced by resolving and
    comparing parents — never by string prefix, which `..` walks straight past.
    """
    if not filename.endswith(".onnx") or "/" in filename or "\\" in filename:
        return None
    d = model_dir().resolve()
    p = (d / filename).resolve()
    if p.parent != d or not p.is_file():
        return None
    return p


def head_meta(filename: str) -> dict | None:
    """Sidecar metadata for a fused model: `<name>.onnx.json` next to the model.

    It carries the phrase labels and the per-class thresholds. The graph emits raw LOGITS
    (not a 0..1 sigmoid) and is multi-class — one logit per phrase, each compared against
    its own threshold. Without this the renderer has no phrase names and no cutoffs.
    """
    p = model_dir() / f"{filename}.json"
    if not p.is_file():
        return None
    try:
        d = json.loads(p.read_text())
    except Exception:  # noqa: BLE001 - a malformed sidecar just means "no metadata"
        return None
    return {
        "words": d.get("words") or [],
        "thresholds": d.get("thresholds") or [],
        "nClasses": d.get("n_classes"),
        "window": d.get("window"),
    }


def list_heads() -> list[dict]:
    """Builtin descriptor (if downloaded) ∪ any other .onnx found in the models dir.

    The scan is what makes a self-trained model work with no code change: drop
    `my_wake_word.onnx` into ~/.curry-leaves/models/wakeword/ and it appears here.
    """
    heads: list[dict] = []
    if is_downloaded(BUILTIN["file"]):
        heads.append({
            **BUILTIN,
            "builtin": True,
            "downloaded": True,
            "meta": head_meta(BUILTIN["file"]),
        })
    d = model_dir()
    if d.is_dir():
        for p in sorted(d.glob("*.onnx")):
            if p.name == BUILTIN["file"]:
                continue
            meta = head_meta(p.name)
            words = (meta or {}).get("words") or []
            # A multi-phrase model is better named by its phrases than by its filename.
            label = ", ".join(words) if words else p.stem.replace("_", " ").title()
            heads.append({
                "id": p.stem,
                "label": label,
                "file": p.name,
                "sidecar": f"{p.name}.json" if head_meta(p.name) else None,
                "builtin": False,
                "downloaded": True,
                "meta": meta,
            })
    return heads


def active_head() -> dict | None:
    """The configured model, falling back to the first available one."""
    want = (wakeword_cfg().get("active") or "").strip()
    heads = list_heads()
    for h in heads:
        if h["id"] == want and h["downloaded"]:
            return h
    return next((h for h in heads if h["downloaded"]), None)


def _fetch(filename: str, remote: str | None = None) -> None:
    """Download one asset, atomically. A killed download must not leave a truncated
    file behind — is_downloaded() would then report it present and the chain would
    fail at session-create with an opaque error.

    `remote` overrides the URL basename when the on-disk name differs from the repo's
    (the sidecar is `multi.onnx.json` upstream but stored as `<model>.onnx.json`)."""
    dest = model_dir() / filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = f"{HF_BASE}/{remote or filename}"
    with urllib.request.urlopen(url, timeout=120) as r:  # noqa: S310 - fixed https host
        if r.status != 200:
            raise RuntimeError(f"{url} → HTTP {r.status}")
        with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False, suffix=".part") as tmp:
            shutil.copyfileobj(r, tmp)
            tmp_path = Path(tmp.name)
    tmp_path.replace(dest)


def download(head_id: str | None = None) -> dict:
    """Fetch the built-in fused model and its sidecar.

    Only the built-in is fetchable — a non-builtin model is user-supplied and already
    on disk by definition, so any other `head_id` is a no-op that just re-reports state.
    """
    want = head_id or (wakeword_cfg().get("active") or BUILTIN["id"])
    if want == BUILTIN["id"]:
        if not is_downloaded(BUILTIN["file"]):
            _fetch(BUILTIN["file"])
        # The sidecar is small and re-fetchable; keep it in step with the model. Fetched
        # as `multi.onnx.json` upstream, saved as `<model>.onnx.json` locally.
        if not is_downloaded(BUILTIN["sidecar"]):
            _fetch(BUILTIN["sidecar"], remote=REMOTE_SIDECAR)

    return {"ok": available(), "heads": list_heads()}


def warm() -> None:
    """Pre-fetch weights when the feature is enabled, so first use isn't a download.

    Gated on `enabled` so a default-off install never touches the network. Blocking and
    best-effort — call from a thread off the boot path, as app.py does for tts/embeddings.
    """
    cfg = wakeword_cfg()
    if not cfg.get("enabled"):
        return
    if available():
        return
    try:
        download(cfg.get("active") or None)
    except Exception as e:  # noqa: BLE001 - best-effort warm, never fatal at boot
        print(f"[wakeword] warm failed: {e}")
