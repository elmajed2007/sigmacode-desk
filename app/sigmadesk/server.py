"""Local HTTP API + UI (127.0.0.1 only).

Protection against other local processes and web pages: every /api call needs the
per-launch token (header X-Desk-Token, or ?t= for <img>/<a> URLs), and the Host
header must be the loopback address (blocks DNS-rebinding).
"""
from __future__ import annotations

import json
import secrets as pysecrets
import tempfile
import time
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, config
from .ai import catalog, llm, runtime
from .core import Desk, DeskError
from .ocr import loader


def create_app(desk: Desk, token: str | None = None, port: int | None = None) -> FastAPI:
    app = FastAPI(title="SigmaCode Desk", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.desk = desk
    app.state.token = token or pysecrets.token_urlsafe(24)
    app.state.port = port
    app.state.last_ping = time.monotonic()
    web = Path(__file__).parent / "web"

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = (request.headers.get("host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost", "testserver"):
            return JSONResponse({"detail": "Forbidden host"}, status_code=403)
        if request.url.path.startswith("/api/"):
            supplied = request.headers.get("x-desk-token") or request.query_params.get("t")
            if not supplied or not pysecrets.compare_digest(supplied, app.state.token):
                return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def fail(exc: Exception, code=400):
        raise HTTPException(code, str(exc))

    # ------------------------------------------------------------------ app
    @app.get("/")
    def index():
        return FileResponse(web / "index.html", media_type="text/html")

    app.mount("/static", StaticFiles(directory=str(web)), name="static")

    @app.post("/api/ping")
    def ping():
        app.state.last_ping = time.monotonic()
        return {"ok": True}

    @app.get("/api/bootstrap")
    def bootstrap():
        return {
            "version": __version__,
            "settings": desk.settings.public(),
            "collections": desk.store.collections(),
            "stats": desk.store.stats(),
            "runtime": runtime.status(),
            "data_dir": str(desk.data),
        }

    # ------------------------------------------------------------------ collections
    @app.get("/api/collections")
    def collections():
        return desk.store.collections()

    @app.post("/api/collections")
    def add_collection(body: dict):
        name = (body.get("name") or "").strip()
        if not name:
            fail("Name is required.")
        return {"id": desk.store.add_collection(name, body.get("description", ""))}

    @app.patch("/api/collections/{cid}")
    def edit_collection(cid: int, body: dict):
        desk.store.rename_collection(cid, body.get("name", ""), body.get("description"))
        return {"ok": True}

    @app.delete("/api/collections/{cid}")
    def delete_collection(cid: int):
        if len(desk.store.collections()) <= 1:
            fail("At least one collection must remain.")
        desk.store.delete_collection(cid)
        return {"ok": True}

    # ------------------------------------------------------------------ documents
    @app.get("/api/documents")
    def documents(collection: int | None = None):
        return desk.store.documents(collection)

    @app.post("/api/documents")
    async def upload(collection_id: int = Form(...), files: list[UploadFile] = File(...)):
        results = []
        for f in files:
            name = Path(f.filename or "file").name
            try:
                loader.kind_of(name)
            except loader.LoadError as exc:
                results.append({"file": name, "error": str(exc)})
                continue
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / name
                with open(path, "wb") as fh:
                    while chunk := await f.read(1 << 20):
                        fh.write(chunk)
                try:
                    res = desk.import_file(path, collection_id)
                except DeskError as exc:
                    results.append({"file": name, "error": str(exc)})
                    continue
            results.append({"file": name, **res})
        return results

    def _doc(did):
        d = desk.store.document(did)
        if not d:
            fail("Document not found.", 404)
        return d

    @app.get("/api/documents/{did}")
    def document(did: int):
        d = _doc(did)
        d["chunks"] = desk.store.chunks(did)
        return d

    @app.patch("/api/documents/{did}")
    def edit_document(did: int, body: dict):
        _doc(did)
        desk.store.update_document(did, **{k: v for k, v in body.items() if k in ("name", "version", "collection_id")})
        return {"ok": True}

    @app.delete("/api/documents/{did}")
    def delete_document(did: int):
        d = _doc(did)
        desk.store.delete_document(did)
        try:
            Path(d["stored_path"]).unlink(missing_ok=True)
        except OSError:
            pass
        return {"ok": True}

    @app.post("/api/documents/{did}/reprocess")
    def reprocess(did: int, body: dict | None = None):
        _doc(did)
        desk.reprocess(did, force_ocr=bool((body or {}).get("force_ocr")))
        return {"ok": True}

    @app.get("/api/documents/{did}/pages")
    def pages(did: int):
        _doc(did)
        return desk.store.pages(did)

    @app.put("/api/documents/{did}/pages/{page_no}")
    def edit_page(did: int, page_no: int, body: dict):
        _doc(did)
        desk.store.edit_page(did, page_no, body.get("text", ""))
        return {"ok": True}

    @app.post("/api/documents/{did}/approve")
    def approve(did: int):
        d = _doc(did)
        if d["status"] not in ("review", "ready"):
            fail("The document is not ready for approval yet.")
        desk.index_document(did)
        return desk.store.document(did)

    @app.get("/api/documents/{did}/pages/{page_no}/image")
    def page_image(did: int, page_no: int):
        d = _doc(did)
        try:
            png = loader.page_png(Path(d["stored_path"]), d["kind"], page_no)
        except (loader.LoadError, IndexError) as exc:
            fail(exc, 404)
        return Response(png, media_type="image/png")

    @app.get("/api/documents/{did}/file")
    def original(did: int):
        d = _doc(did)
        return FileResponse(d["stored_path"], filename=d["filename"])

    @app.get("/api/chunks/{cid}")
    def chunk(cid: int):
        c = desk.store.chunk(cid)
        if not c:
            fail("This source no longer exists.", 404)
        c.pop("stored_path", None)
        c["has_image"] = c["kind"] in ("pdf", "image")
        return c

    @app.post("/api/search")
    def search_api(body: dict):
        info = {}
        hits = desk.retrieve(body.get("query", ""), body.get("collection_ids") or None,
                             int(body.get("limit") or 8), include_flagged=bool(body.get("include_flagged")), info=info)
        return {"hits": hits, "info": info}

    # ------------------------------------------------------------------ chats
    @app.get("/api/chats")
    def chats():
        return desk.store.chats()

    @app.post("/api/chats")
    def new_chat(body: dict):
        cid = desk.store.add_chat("محادثة جديدة", body.get("collection_ids") or [])
        return desk.store.chat(cid)

    @app.get("/api/chats/{chat_id}")
    def chat(chat_id: int):
        c = desk.store.chat(chat_id)
        if not c:
            fail("Conversation not found.", 404)
        c["messages"] = desk.store.messages(chat_id)
        return c

    @app.patch("/api/chats/{chat_id}")
    def edit_chat(chat_id: int, body: dict):
        desk.store.update_chat(chat_id, **{k: v for k, v in body.items() if k in ("title", "collection_ids")})
        return {"ok": True}

    @app.delete("/api/chats/{chat_id}")
    def delete_chat(chat_id: int):
        desk.store.delete_chat(chat_id)
        return {"ok": True}

    @app.post("/api/chats/{chat_id}/ask")
    def ask(chat_id: int, body: dict):
        try:
            return desk.ask(chat_id, body.get("question", ""))
        except (DeskError, runtime.RuntimeError_) as exc:
            fail(exc)

    @app.post("/api/chats/{chat_id}/stream")
    def ask_stream(chat_id: int, body: dict):
        gen = desk.ask_stream(chat_id, body.get("question", ""))
        try:
            first = next(gen)  # surface preparation errors as a normal HTTP error
        except (DeskError, runtime.RuntimeError_) as exc:
            fail(exc)

        def lines():
            item = first
            while True:
                kind, data = item
                yield (json.dumps({"type": kind, "data": data}, ensure_ascii=False) + "\n").encode()
                if kind == "final":
                    return
                item = next(gen)

        return StreamingResponse(lines(), media_type="application/x-ndjson")

    @app.post("/api/messages/{mid}/feedback")
    def feedback(mid: int, body: dict):
        value = body.get("value") or ""
        if value not in ("", "up", "down"):
            fail("Invalid feedback.")
        desk.store.execute("UPDATE messages SET feedback = ? WHERE id = ? AND role = 'assistant'", (value, mid))
        return {"ok": True}

    # ------------------------------------------------------------------ settings & AI
    @app.get("/api/settings")
    def get_settings():
        return desk.settings.public()

    @app.put("/api/settings")
    def put_settings(body: dict):
        before = desk.embedding_backend()
        desk.settings.update(body)
        after = desk.embedding_backend()
        if (before and before[0]) != (after and after[0]):
            desk.jobs.put(("embed", None))
        return desk.settings.public()

    @app.post("/api/settings/test")
    def test_ai():
        out = {}
        try:
            call, label = desk.chat_call()
            r = llm.chat(call["provider_type"], call["base_url"], call["api_key"], call["model"],
                         "Reply with the single word: OK", [{"role": "user", "content": "ping"}],
                         temperature=None, max_tokens=16, timeout=120)
            out["chat"] = {"ok": True, "model": label, "reply": r["text"][:80]}
        except Exception as exc:  # noqa: BLE001
            out["chat"] = {"ok": False, "error": str(exc)[:300]}
        backend = desk.embedding_backend()
        if backend:
            try:
                v = backend[1](["اختبار"], "query")[0]
                out["embeddings"] = {"ok": True, "dimensions": len(v), "signature": backend[0]}
            except Exception as exc:  # noqa: BLE001
                out["embeddings"] = {"ok": False, "error": str(exc)[:300]}
        else:
            out["embeddings"] = {"ok": None}
        return out

    @app.get("/api/ai/status")
    def ai_status():
        backend = desk.embedding_backend()
        return {
            "runtime": runtime.status(),
            "embeddings": desk.store.embedding_progress(backend[0]) if backend else None,
            "embedding_error": desk.last_embed_error,
        }

    @app.get("/api/models")
    def models():
        return {"catalog": catalog.CATALOG, "installed": catalog.installed(), "progress": catalog.progress(),
                "folder": str(catalog.models_dir())}

    @app.post("/api/models/download")
    def download(body: dict):
        try:
            catalog.download(body.get("id", ""))
        except ValueError as exc:
            fail(exc)
        return {"ok": True}

    @app.post("/api/models/import")
    async def import_model(file: UploadFile = File(...)):
        name = Path(file.filename or "").name
        if not name.lower().endswith(".gguf"):
            fail("Only .gguf model files can be imported.")
        dest = catalog.models_dir() / name
        with open(dest, "wb") as fh:
            while chunk := await file.read(8 << 20):
                fh.write(chunk)
        return {"file": str(dest), "name": name, "role": catalog.guess_role(name)}

    @app.delete("/api/models/{name}")
    def delete_model(name: str):
        path = catalog.models_dir() / Path(name).name
        s = desk.settings.get()
        if str(path) in (s["providers"]["local"].get("model_file"), s["embeddings"].get("local_model_file")):
            fail("This model is in use. Choose another model first.")
        path.unlink(missing_ok=True)
        return {"ok": True}

    # ------------------------------------------------------------------ export
    @app.post("/api/export")
    def export(body: dict):
        path = desk.export_jsonl(body.get("collection_ids") or None)
        return {"file": path.name}

    @app.get("/api/exports/{name}")
    def export_file(name: str):
        path = desk.data / "exports" / Path(name).name
        if not path.is_file():
            fail("Export not found.", 404)
        return FileResponse(path, filename=path.name, media_type="application/x-ndjson")

    return app
