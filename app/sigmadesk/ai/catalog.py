"""Local model catalog: recommended open models, download once, then fully offline.

Models are .gguf files in <data>/models. They can also be imported from a file
(USB, network share) for machines without internet. Download resolves the exact
file name from the Hugging Face repository listing, so a renamed quantisation
does not break the catalog.
"""
from __future__ import annotations

import fnmatch
import shutil
import threading
from pathlib import Path

import requests

from .. import config

CATALOG = [
    {
        "id": "qwen3-4b", "role": "chat", "recommended": True,
        "name": "Qwen3 4B", "repo": "Qwen/Qwen3-4B-GGUF", "patterns": ["*Q4_K_M.gguf", "*Q5_K_M.gguf", "*Q8_0.gguf"],
        "size_gb": 2.5, "license": "Apache-2.0", "ram_gb": 6,
        "note": "توازن جيد بين الجودة والسرعة، يدعم العربية. يحتاج ذاكرة 8 جيجابايت.",
    },
    {
        "id": "qwen3-1.7b", "role": "chat",
        "name": "Qwen3 1.7B (خفيف)", "repo": "Qwen/Qwen3-1.7B-GGUF", "patterns": ["*Q8_0.gguf", "*Q4_K_M.gguf"],
        "size_gb": 1.8, "license": "Apache-2.0", "ram_gb": 4,
        "note": "للأجهزة الضعيفة: أسرع وأقل دقة.",
    },
    {
        "id": "qwen3-8b", "role": "chat",
        "name": "Qwen3 8B (أعلى جودة)", "repo": "Qwen/Qwen3-8B-GGUF", "patterns": ["*Q4_K_M.gguf", "*Q5_K_M.gguf"],
        "size_gb": 5.0, "license": "Apache-2.0", "ram_gb": 12,
        "note": "أفضل إجابات، يحتاج ذاكرة 16 جيجابايت ومعالجاً حديثاً.",
    },
    {
        "id": "qwen3-embed-0.6b", "role": "embed", "recommended": True,
        "name": "Qwen3 Embedding 0.6B", "repo": "Qwen/Qwen3-Embedding-0.6B-GGUF", "patterns": ["*Q8_0.gguf", "*f16.gguf"],
        "size_gb": 0.64, "license": "Apache-2.0", "ram_gb": 2,
        "note": "بحث بالمعنى متعدد اللغات يدعم العربية.",
    },
]

_progress: dict = {}
_lock = threading.Lock()


def models_dir() -> Path:
    return config.data_dir() / "models"


def installed() -> list[dict]:
    out = []
    for f in sorted(models_dir().glob("*.gguf")):
        out.append({"file": str(f), "name": f.name, "size_gb": round(f.stat().st_size / 1e9, 2),
                    "role": guess_role(f.name)})
    return out


def guess_role(filename: str) -> str:
    low = filename.lower()
    return "embed" if any(k in low for k in ("embed", "e5", "bge", "gte", "minilm", "nomic")) else "chat"


def import_file(src: Path) -> Path:
    src = Path(src)
    if src.suffix.lower() != ".gguf":
        raise ValueError("Only .gguf model files can be imported.")
    dest = models_dir() / src.name
    shutil.copyfile(src, dest)
    return dest


def resolve_file(repo: str, patterns: list[str], timeout=30) -> str:
    r = requests.get(f"https://huggingface.co/api/models/{repo}", timeout=timeout)
    r.raise_for_status()
    names = [s.get("rfilename", "") for s in r.json().get("siblings", [])]
    for pat in patterns:
        for name in names:
            if fnmatch.fnmatch(name, pat):
                return name
    raise ValueError(f"No matching model file in {repo}.")


def progress() -> dict:
    with _lock:
        return dict(_progress)


def download(entry_id: str) -> None:
    """Download in a background thread; poll progress()."""
    entry = next((e for e in CATALOG if e["id"] == entry_id), None)
    if not entry:
        raise ValueError("Unknown model.")
    with _lock:
        if _progress.get(entry_id, {}).get("state") == "downloading":
            return
        _progress[entry_id] = {"state": "downloading", "done": 0, "total": 0, "error": ""}
    threading.Thread(target=_download, args=(entry,), daemon=True).start()


def _download(entry):
    eid = entry["id"]
    try:
        name = resolve_file(entry["repo"], entry["patterns"])
        url = f"https://huggingface.co/{entry['repo']}/resolve/main/{name}"
        dest = models_dir() / Path(name).name
        tmp = dest.with_suffix(".part")
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0)
            with _lock:
                _progress[eid]["total"] = total
            done = 0
            with open(tmp, "wb") as fh:
                for block in r.iter_content(1 << 20):
                    fh.write(block)
                    done += len(block)
                    with _lock:
                        _progress[eid]["done"] = done
        tmp.replace(dest)
        with _lock:
            _progress[eid].update(state="done", file=str(dest))
    except Exception as exc:  # noqa: BLE001 - reported to the UI
        with _lock:
            _progress[eid].update(state="error", error=str(exc)[:300])
