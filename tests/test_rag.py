from conftest import FIXTURES, wait_status


def ingest(desk, name="two_pages.pdf"):
    cid = desk.store.collections()[0]["id"]
    res = desk.import_file(FIXTURES / name, cid)
    doc = wait_status(desk, res["id"])
    if doc["status"] == "review":
        desk.index_document(res["id"])
        doc = desk.store.document(res["id"])
    assert doc["status"] == "ready", doc
    return doc


def test_import_dedupe_and_index(desk, fake_llm):
    doc = ingest(desk)
    cid = desk.store.collections()[0]["id"]
    assert desk.import_file(FIXTURES / "two_pages.pdf", cid)["duplicate"]
    assert desk.store.chunks(doc["id"])


def test_retrieve_and_ask_with_citation(desk, fake_llm):
    doc = ingest(desk)
    desk.jobs.join()
    word = desk.store.chunks(doc["id"])[0]["content"].split()[3]
    info = {}
    hits = desk.retrieve(word, None, 5, info=info)
    assert hits and info["mode"] == "hybrid"
    chat = desk.store.add_chat("t", [])
    msg = desk.ask(chat, word)
    assert msg["grounded"] and msg["sources"][0]["document_id"] == doc["id"]


def test_strict_refuses_without_sources(desk, fake_llm):
    chat = desk.store.add_chat("t", [])
    msg = desk.ask(chat, "سؤال لا علاقة له بأي مستند zzqx")
    assert msg["refused"]


def test_stream_hides_thinking(desk, fake_llm):
    doc = ingest(desk)
    desk.jobs.join()
    word = desk.store.chunks(doc["id"])[0]["content"].split()[3]
    chat = desk.store.add_chat("t", [])
    events = list(desk.ask_stream(chat, word))
    text = "".join(d for k, d in events if k == "delta")
    assert "hidden" not in text and "think" not in text
    final = events[-1][1]
    assert events[-1][0] == "final" and final["grounded"] and "hidden" not in final["content"]


def test_export(desk, fake_llm):
    ingest(desk)
    path = desk.export_jsonl()
    assert path.read_text(encoding="utf-8").strip()
