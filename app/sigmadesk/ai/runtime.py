"""Built-in local AI: runs llama.cpp's ``llama-server`` (MIT) as a child process.

One process per role ("chat" and "embed"), bound to 127.0.0.1 on a free port,
OpenAI-compatible (/v1/chat/completions, /v1/embeddings) so ai.llm talks to it like
any other provider. Started on first use, restarted when the model file changes,
stopped when the app exits. Works fully offline once a .gguf model is present.
"""
from __future__ import annotations

import atexit
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests

from .. import config


class RuntimeError_(Exception):
    pass


def server_binary() -> Path | None:
    override = os.environ.get("SIGMADESK_LLAMA_SERVER")
    if override:
        return Path(override)
    name = "llama-server.exe" if os.name == "nt" else "llama-server"
    for base in (config.runtime_dir() / "llama", config.runtime_dir()):
        candidate = base / name
        if candidate.is_file():
            return candidate
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LlamaServer:
    def __init__(self, role: str):
        self.role = role
        self.proc: subprocess.Popen | None = None
        self.model: str | None = None
        self.port: int | None = None
        self.log_path = config.data_dir() / "logs" / f"llama-{role}.log"
        self._lock = threading.Lock()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def ensure(self, model_file: str, context: int = 8192, threads: int = 0, timeout: float = 180) -> str:
        """Start (or reuse) the server for ``model_file``; returns the OpenAI base URL."""
        with self._lock:
            if self.running() and self.model == model_file:
                return self.base_url
            self.stop()
            binary = server_binary()
            if not binary:
                raise RuntimeError_("The local AI engine (llama-server) is missing from this installation.")
            model_path = Path(model_file)
            if not model_path.is_file():
                raise RuntimeError_(f"Model file not found: {model_path.name}. Choose or download a model in Settings.")
            self.port = _free_port()
            args = [str(binary), "-m", str(model_path), "--host", "127.0.0.1", "--port", str(self.port),
                    "-c", str(int(context or 8192))]
            if threads:
                args += ["-t", str(int(threads))]
            if self.role == "embed":
                args += ["--embeddings", "-b", "2048", "-ub", "2048"]
            else:
                args += ["--jinja"]
            flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
            log = open(self.log_path, "ab")
            self.proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                         creationflags=flags, cwd=str(binary.parent))
            self.model = model_file
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self.proc.poll() is not None:
                    raise RuntimeError_(f"The local AI engine stopped while loading the model (see {self.log_path.name}).")
                try:
                    r = requests.get(f"http://127.0.0.1:{self.port}/health", timeout=2)
                    if r.status_code == 200:
                        return self.base_url
                except requests.RequestException:
                    pass
                time.sleep(0.5)
            self.stop()
            raise RuntimeError_("The local AI engine did not become ready in time.")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        self.model = None


CHAT = LlamaServer("chat")
EMBED = LlamaServer("embed")


@atexit.register
def _shutdown():
    CHAT.stop()
    EMBED.stop()


def status() -> dict:
    return {
        "binary": bool(server_binary()),
        "chat": {"running": CHAT.running(), "model": Path(CHAT.model).name if CHAT.model else None},
        "embed": {"running": EMBED.running(), "model": Path(EMBED.model).name if EMBED.model else None},
        "platform": sys.platform,
    }
