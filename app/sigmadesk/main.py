"""Desktop launcher: starts the local server and opens the app window.

Window: Microsoft Edge in app mode (installed on every Windows 10/11) with its own
profile, so closing the window ends the app. Falls back to Chrome/Chromium app
mode, then to the default browser (the server then stops after the UI has been
silent for a while). A second launch reuses the running instance.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

import requests
import uvicorn

from . import config
from .core import Desk
from .server import create_app

IDLE_EXIT_SECONDS = 180  # browser fallback: stop when no window has pinged for this long


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _lock_file() -> Path:
    return config.data_dir() / "instance.json"


def _existing_instance():
    try:
        info = json.loads(_lock_file().read_text(encoding="utf-8"))
        r = requests.post(f"http://127.0.0.1:{info['port']}/api/ping",
                          headers={"X-Desk-Token": info["token"]}, timeout=2)
        if r.status_code == 200:
            return info
    except (OSError, ValueError, KeyError, requests.RequestException):
        pass
    return None


def _app_browser() -> list[str] | None:
    candidates = []
    if os.name == "nt":
        for base in (os.environ.get("PROGRAMFILES(X86)"), os.environ.get("PROGRAMFILES"), os.environ.get("LOCALAPPDATA")):
            if base:
                candidates.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
                candidates.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
    for name in ("microsoft-edge", "google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    for c in candidates:
        if c.is_file():
            return [str(c)]
    return None


def open_window(url: str, profile: Path) -> subprocess.Popen | None:
    browser = _app_browser()
    if browser:
        args = browser + [f"--app={url}", f"--user-data-dir={profile}", "--no-first-run",
                          "--no-default-browser-check", "--window-size=1360,880", "--lang=ar"]
        return subprocess.Popen(args)
    webbrowser.open(url)
    return None


def self_test(pdf: str) -> int:
    """Packaging check used by CI: OCR a file, start the API, report the runtime."""
    from .ai import runtime
    from .ocr import loader
    from .ocr.engine import OcrEngine, OcrSettings

    eng = OcrEngine(config.assets_dir() / "ocr")
    pages = loader.extract(Path(pdf), loader.kind_of(pdf), lambda: eng, OcrSettings(), force_ocr=True)
    text = " ".join(p["text"] for p in pages)
    desk = Desk(config.data_dir())
    from fastapi.testclient import TestClient  # noqa: PLC0415
    client = TestClient(create_app(desk, token="t"))
    boot = client.get("/api/bootstrap", headers={"X-Desk-Token": "t"}).status_code
    llama = runtime.server_binary()
    report = {"pages": len(pages), "chars": len(text), "confidence": pages[0]["confidence"] if pages else None,
              "api": boot, "llama_server": str(llama) if llama else None, "sample": text[:80]}
    out = json.dumps(report, ensure_ascii=False)
    (config.data_dir() / "selftest.json").write_text(out, encoding="utf-8")
    if sys.stdout:  # the windowed exe has no console
        print(out)
    ok = len(text) > 50 and boot == 200 and llama is not None
    desk.jobs.put(("stop",))
    return 0 if ok else 1


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--self-test":
        return self_test(sys.argv[2])
    data = config.data_dir()
    logging.basicConfig(filename=str(data / "logs" / "app.log"), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    profile = data / "window-profile"

    running = _existing_instance()
    if running:
        open_window(f"http://127.0.0.1:{running['port']}/#t={running['token']}", profile)
        return

    desk = Desk(data)
    port = _free_port()
    app = create_app(desk, port=port)
    token = app.state.token
    _lock_file().write_text(json.dumps({"port": port, "token": token, "pid": os.getpid()}), encoding="utf-8")

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                           access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)

    url = f"http://127.0.0.1:{port}/#t={token}"
    window = open_window(url, profile)
    try:
        if window is not None:
            # Edge/Chrome with a private profile: the process lives as long as the window
            window.wait()
            # the browser may hand off to an already-running process of the same profile:
            # then rely on the UI heartbeat
            while time.monotonic() - app.state.last_ping < 15:
                time.sleep(2)
        else:
            while time.monotonic() - app.state.last_ping < IDLE_EXIT_SECONDS:
                time.sleep(5)
    except KeyboardInterrupt:
        pass
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        try:
            _lock_file().unlink(missing_ok=True)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
