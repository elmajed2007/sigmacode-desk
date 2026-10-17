from fastapi.testclient import TestClient

from conftest import FIXTURES, wait_status
from sigmadesk.server import create_app


def client(desk):
    app = create_app(desk, token="tok")
    return TestClient(app), {"X-Desk-Token": "tok"}


def test_token_required(desk):
    c, hdr = client(desk)
    assert c.get("/api/bootstrap").status_code == 401
    assert c.get("/api/bootstrap", headers={"X-Desk-Token": "bad"}).status_code == 401
    assert c.get("/api/bootstrap", headers=hdr).status_code == 200
    assert c.get("/api/collections?t=tok").status_code == 200


def test_foreign_host_rejected(desk):
    c, hdr = client(desk)
    assert c.get("/api/bootstrap", headers={**hdr, "Host": "evil.example"}).status_code == 403
    assert c.get("/").status_code == 200


def test_keys_are_masked(desk):
    c, hdr = client(desk)
    c.put("/api/settings", headers=hdr, json={"providers": {"openai": {"api_key": "sk-secret"}}})
    s = c.get("/api/settings", headers=hdr).json()
    assert s["providers"]["openai"]["api_key"] != "sk-secret"
    # saving the mask back keeps the real key
    c.put("/api/settings", headers=hdr, json={"providers": {"openai": {"api_key": s["providers"]["openai"]["api_key"]}}})
    assert desk.settings.provider("openai")["api_key"] == "sk-secret"


def test_upload_review_stream_flow(desk, fake_llm):
    c, hdr = client(desk)
    cid = c.get("/api/collections", headers=hdr).json()[0]["id"]
    with open(FIXTURES / "two_pages.pdf", "rb") as fh:
        r = c.post("/api/documents", headers=hdr, data={"collection_id": str(cid)},
                   files=[("files", ("two_pages.pdf", fh, "application/pdf"))])
    did = r.json()[0]["id"]
    doc = wait_status(desk, did)
    if doc["status"] == "review":
        assert c.post(f"/api/documents/{did}/approve", headers=hdr).json()["status"] == "ready"
    assert c.get(f"/api/documents/{did}/pages/1/image?t=tok").headers["content-type"] == "image/png"
    pages = c.get(f"/api/documents/{did}/pages", headers=hdr).json()
    word = pages[0]["text"].split()[3]
    chat = c.post("/api/chats", headers=hdr, json={}).json()
    with c.stream("POST", f"/api/chats/{chat['id']}/stream", headers=hdr, json={"question": word}) as s:
        lines = [l for l in s.iter_lines() if l]
    import json
    final = json.loads(lines[-1])
    assert final["type"] == "final" and final["data"]["sources"]
    chunk = c.get(f"/api/chunks/{final['data']['sources'][0]['chunk_id']}", headers=hdr).json()
    assert chunk["has_image"]
