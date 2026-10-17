"""Paths and settings. Everything lives in one per-user data folder."""
from __future__ import annotations

import json
import os
import sys
import threading
from copy import deepcopy
from pathlib import Path

from . import keystore

APP_NAME = "SigmaCodeDesk"


def app_root() -> Path:
    """Folder holding bundled assets (PyInstaller: sys._MEIPASS)."""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else Path(__file__).resolve().parent.parent


def assets_dir() -> Path:
    return app_root() / "assets"


def runtime_dir() -> Path:
    """Bundled native tools (llama.cpp server). Next to the exe when frozen."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "runtime"
    return app_root().parent / "runtime"


def data_dir() -> Path:
    override = os.environ.get("SIGMADESK_DATA")
    if override:
        path = Path(override)
    elif os.name == "nt":
        path = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / APP_NAME
    else:
        path = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_NAME
    for sub in ("files", "models", "logs", "exports"):
        (path / sub).mkdir(parents=True, exist_ok=True)
    return path


DEFAULTS = {
    "ui_language": "ar",
    "ocr": {
        "pdf_scale": 2.0,
        "min_line_score": 0.5,
        "space_boost": 0.02,
        "respace_min": 12,
        "reverse_mode": "auto",
        "force_ocr": False,
        "auto_approve_confidence": 0.90,  # documents above this are indexed without review
        "max_pages": 500,
    },
    "chunking": {"size": 900, "overlap": 150, "low_confidence": 0.85},
    "answer": {
        "provider": "local",  # local | ollama | openai | anthropic | google | custom
        "strict": True,
        "max_sources": 6,
        "temperature": 0.2,
        "max_tokens": 1024,
        "history_turns": 3,
        "stream": True,
        "system_prompt": "",
    },
    "providers": {
        "local": {"model_file": "", "context": 8192, "threads": 0, "port": 0},
        "ollama": {"base_url": "http://127.0.0.1:11434/v1", "model": "qwen3:4b"},
        "openai": {"base_url": "", "api_key": "", "model": "gpt-4.1-mini"},
        "anthropic": {"base_url": "", "api_key": "", "model": ""},
        "google": {"base_url": "", "api_key": "", "model": "gemini-2.0-flash"},
        "custom": {"base_url": "", "api_key": "", "model": ""},
    },
    "embeddings": {
        "provider": "local",  # local | none | ollama | openai | google | custom
        "local_model_file": "",
        "model": "",  # remote providers
        "min_similarity": 0.45,
    },
}

_SECRET_KEYS = {"api_key"}


def _merge(base: dict, extra: dict) -> dict:
    out = deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Settings:
    """settings.json in the data folder; thread-safe, merged over DEFAULTS."""

    def __init__(self, folder: Path):
        self.path = folder / "settings.json"
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> dict:
        try:
            return _merge(DEFAULTS, json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return deepcopy(DEFAULTS)

    def get(self) -> dict:
        with self._lock:
            return deepcopy(self._data)

    def provider(self, name: str) -> dict:
        """Provider settings with the API key decrypted (server side only)."""
        prov = self.get()["providers"].get(name, {})
        if prov.get("api_key"):
            prov["api_key"] = keystore.reveal(prov["api_key"])
        return prov

    def public(self) -> dict:
        """Settings for the UI: API keys are reported as set/unset, never returned."""
        data = self.get()
        for prov in data["providers"].values():
            for k in _SECRET_KEYS:
                if k in prov:
                    prov[k] = "••••••" if prov[k] else ""
        return data

    def update(self, patch: dict) -> dict:
        with self._lock:
            patch = deepcopy(patch or {})
            # the UI sends the mask back unchanged: keep the stored key
            for name, prov in (patch.get("providers") or {}).items():
                for k in _SECRET_KEYS:
                    if prov.get(k) == "••••••":
                        prov.pop(k)
                    elif prov.get(k):
                        prov[k] = keystore.protect(prov[k])
            self._data = _merge(self._data, patch)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)
            return deepcopy(self._data)
