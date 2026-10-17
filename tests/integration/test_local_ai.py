"""Offline AI path on the real llama.cpp runtime (run in CI on Windows; needs internet
once to fetch two small models). Enabled with SIGMADESK_IT=1."""
import os
import time

import pytest

from conftest import FIXTURES, wait_status

pytestmark = pytest.mark.skipif(os.environ.get("SIGMADESK_IT") != "1", reason="integration only")

CHAT_MODEL = {"id": "it-chat", "repo": "Qwen/Qwen3-0.6B-GGUF", "patterns": ["*Q8_0.gguf", "*Q4_K_M.gguf"]}


def fetch(entry):
    from sigmadesk.ai import catalog
    with catalog._lock:
        catalog._progress[entry["id"]] = {"state": "downloading", "done": 0, "total": 0, "error": ""}
    name = catalog.resolve_file(entry["repo"], entry["patterns"])
    dest = catalog.models_dir() / name
    if dest.is_file():
        return dest
    catalog._download(entry)
    p = catalog.progress()[entry["id"]]
    assert p["state"] == "done", p
    return dest


def test_offline_answer_with_local_models(tmp_path, monkeypatch):
    from sigmadesk.ai import catalog, runtime
    from sigmadesk.core import Desk

    assert runtime.server_binary(), "llama-server missing"
    embed_entry = next(e for e in catalog.CATALOG if e["role"] == "embed")
    embed_file = fetch(embed_entry)
    chat_file = fetch(CHAT_MODEL)

    desk = Desk(tmp_path / "data")
    desk.settings.update({"answer": {"provider": "local", "max_tokens": 256},
                          "providers": {"local": {"model_file": str(chat_file), "context": 4096}},
                          "embeddings": {"provider": "local", "local_model_file": str(embed_file)}})
    cid = desk.store.collections()[0]["id"]
    did = desk.import_file(FIXTURES / "scan2.pdf", cid)["id"]
    doc = wait_status(desk, did)
    if doc["status"] == "review":
        desk.index_document(did)
    desk.jobs.join()
    end = time.time() + 300
    sig = desk.embedding_backend()[0]
    while desk.store.embedding_progress(sig)["done"] < desk.store.embedding_progress(sig)["total"] and time.time() < end:
        time.sleep(1)
    assert not desk.last_embed_error, desk.last_embed_error
    info = {}
    hits = desk.retrieve("ما الذي يقيسه الاختبار التكيفي؟", None, 4, info=info)
    assert hits and info["mode"] == "hybrid", info
    chat = desk.store.add_chat("it", [])
    msg = desk.ask(chat, "ما الذي يقيسه الاختبار التكيفي؟")
    print("ANSWER:", msg["outcome"], msg["content"][:300])
    assert not msg["is_error"], msg
    assert "<think>" not in msg["content"]
    runtime.CHAT.stop()
    runtime.EMBED.stop()
    desk.jobs.put(("stop",))
