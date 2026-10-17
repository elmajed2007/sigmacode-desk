import re
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
FIXTURES = Path(__file__).parent / "fixtures"


def fake_answer(messages):
    user = messages[-1]["content"]
    m = re.search(r'<source n="1"[^>]*>\n(.*?)\n</source>', user, re.S)
    if not m:
        return "لا تتوفر في المصادر المعتمدة معلومات كافية للإجابة عن هذا السؤال."
    first = m.group(1).strip().splitlines()[0][:120]
    return f"{first} [1]."


def fake_vec(text, dim=64):
    import hashlib
    v = [0.0] * dim
    for w in re.findall(r"\w+", text):
        v[int(hashlib.md5(w.encode()).hexdigest(), 16) % dim] += 1.0
    n = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / n for x in v]


@pytest.fixture
def fake_llm(monkeypatch):
    from sigmadesk.ai import llm

    def chat(ptype, base, key, model, system, messages, **kw):
        t = fake_answer(messages)
        return {"text": t, "input_tokens": 10, "output_tokens": 5, "model": model}

    def chat_stream(ptype, base, key, model, system, messages, **kw):
        t = "<think>hidden</think>" + fake_answer(messages)
        full = ""
        for w in re.findall(r"\S+\s*|<think>|</think>", t):
            full += w
            yield ("delta", w)
        yield ("done", {"text": full, "input_tokens": 10, "output_tokens": 5, "model": model})

    def embed(ptype, base, key, model, texts, kind="document", **kw):
        return {"vectors": [fake_vec(x) for x in texts], "tokens": 1}

    monkeypatch.setattr(llm, "chat", chat)
    monkeypatch.setattr(llm, "chat_stream", chat_stream)
    monkeypatch.setattr(llm, "embed", embed)


@pytest.fixture
def desk(tmp_path):
    from sigmadesk.core import Desk
    d = Desk(tmp_path / "data")
    d.settings.update({"answer": {"provider": "custom"},
                       "providers": {"custom": {"base_url": "http://127.0.0.1:9/v1", "model": "fake"}},
                       "embeddings": {"provider": "custom", "model": "fake-embed", "min_similarity": 0.1}})
    yield d
    d.jobs.put(("stop",))


def wait_status(desk, did, statuses=("ready", "review", "error"), timeout=180):
    end = time.time() + timeout
    while time.time() < end:
        doc = desk.store.document(did)
        if doc["status"] in statuses:
            return doc
        time.sleep(0.2)
    raise AssertionError(f"timeout, status={desk.store.document(did)['status']}")
