"""Application core: import -> extract/OCR -> review -> chunk/index -> embed -> answer.

Single-user, offline-first. Everything runs in-process; heavy work (OCR,
embeddings) runs in one background worker thread so the UI stays responsive.
"""
from __future__ import annotations

import hashlib
import logging
import queue
import re
import shutil
import threading
import time
from pathlib import Path

import numpy as np

from . import config
from .ai import llm, prompt, runtime
from .ocr import loader
from .ocr.engine import OcrEngine, OcrSettings
from .store import Store
from .text import chunker, search

_log = logging.getLogger("sigmadesk")
RRF_K = 60
_THINK = re.compile(r"<think>.*?</think>\s*", re.S)


class DeskError(Exception):
    pass


def strip_thinking(text: str) -> str:
    """Reasoning models (Qwen3) may emit <think>…</think>; never show or store it."""
    text = _THINK.sub("", text or "")
    if "<think>" in text:  # unterminated (cut by max_tokens)
        text = text.split("<think>")[0]
    return text.strip()


class Desk:
    def __init__(self, data: Path | None = None):
        self.data = Path(data) if data else config.data_dir()
        for sub in ("files", "models", "logs", "exports"):
            (self.data / sub).mkdir(parents=True, exist_ok=True)
        self.settings = config.Settings(self.data)
        self.store = Store(self.data / "desk.db")
        self._ocr: OcrEngine | None = None
        self._ocr_lock = threading.Lock()
        self.jobs: queue.Queue = queue.Queue()
        self._worker = threading.Thread(target=self._run_jobs, name="desk-worker", daemon=True)
        self._worker.start()
        # documents interrupted by a previous exit are resumed
        for d in self.store.all("SELECT id FROM documents WHERE status IN ('queued', 'processing')"):
            self.jobs.put(("ingest", d["id"]))
        self.jobs.put(("embed", None))

    # ------------------------------------------------------------------ OCR
    def ocr_engine(self) -> OcrEngine:
        with self._ocr_lock:
            if self._ocr is None:
                self._ocr = OcrEngine(config.assets_dir() / "ocr")
            return self._ocr

    def ocr_settings(self) -> OcrSettings:
        o = self.settings.get()["ocr"]
        return OcrSettings(min_line_score=o["min_line_score"], space_boost=o["space_boost"],
                           respace_min=int(o["respace_min"]), reverse_mode=o["reverse_mode"])

    # ------------------------------------------------------------------ import
    def import_file(self, src: Path, collection_id: int, display_name: str | None = None) -> dict:
        src = Path(src)
        kind = loader.kind_of(src.name)
        if not self.store.one("SELECT id FROM collections WHERE id = ?", (collection_id,)):
            raise DeskError("Collection not found.")
        digest = hashlib.sha256(src.read_bytes()).hexdigest()
        dup = self.store.find_duplicate(collection_id, digest)
        if dup:
            return {"id": dup["id"], "duplicate": True, "name": dup["name"]}
        did = self.store.add_document(collection_id, display_name or src.stem, src.name, "", kind, digest,
                                      src.stat().st_size)
        dest = self.data / "files" / f"{did}{src.suffix.lower()}"
        shutil.copyfile(src, dest)
        self.store.execute("UPDATE documents SET stored_path = ? WHERE id = ?", (str(dest), did))
        self.jobs.put(("ingest", did))
        return {"id": did, "duplicate": False}

    def reprocess(self, did: int, force_ocr: bool = False):
        self.store.set_status(did, "queued", progress=0, error="")
        self.jobs.put(("ingest", did, force_ocr))

    # ------------------------------------------------------------------ worker
    def _run_jobs(self):
        while True:
            job = self.jobs.get()
            try:
                if job[0] == "ingest":
                    self._ingest(job[1], force_ocr=job[2] if len(job) > 2 else False)
                elif job[0] == "embed":
                    self._embed_pending()
                elif job[0] == "stop":
                    return
            except Exception:  # noqa: BLE001 - a job must never kill the worker
                _log.exception("job %s failed", job)
            finally:
                self.jobs.task_done()

    def _ingest(self, did: int, force_ocr: bool = False):
        doc = self.store.document(did)
        if not doc:
            return
        o = self.settings.get()["ocr"]
        self.store.set_status(did, "processing", progress=0, error="")

        def progress(done, total):
            self.store.execute("UPDATE documents SET progress = ? WHERE id = ?", (round(done / max(total, 1), 3), did))

        try:
            pages = loader.extract(Path(doc["stored_path"]), doc["kind"], self.ocr_engine, self.ocr_settings(),
                                   pdf_scale=o["pdf_scale"], force_ocr=force_ocr or o["force_ocr"],
                                   max_pages=int(o["max_pages"]), progress=progress)
        except Exception as exc:  # noqa: BLE001
            self.store.set_status(did, "error", error=str(exc)[:500])
            return
        if not any(p["text"].strip() for p in pages):
            self.store.save_pages(did, pages)
            self.store.set_status(did, "error", page_count=len(pages),
                                  error="No text could be read from this file.")
            return
        self.store.save_pages(did, pages)
        ocr_pages = [p for p in pages if p["method"] == "ocr"]
        methods = {p["method"] for p in pages}
        conf = None
        if ocr_pages:
            n = sum(len(p["text"]) for p in ocr_pages) or 1
            conf = round(sum((p["confidence"] or 0) * len(p["text"]) for p in ocr_pages) / n, 4)
        method = "mixed" if len(methods) > 1 else methods.pop()
        self.store.set_status(did, "processing", page_count=len(pages), confidence=conf, method=method, progress=1)
        if conf is not None and conf < float(o["auto_approve_confidence"]):
            self.store.set_status(did, "review")  # a person checks the OCR text first
            return
        self.index_document(did)

    def index_document(self, did: int):
        """Chunk the (reviewed) page text and index it; queues embeddings."""
        c = self.settings.get()["chunking"]
        pages = self.store.pages(did)
        text = "\n\n".join(p["text"] for p in pages)
        page_lines = []
        for p in pages:
            if p["method"] == "ocr" and not p["edited"] and p["lines"]:
                lines = [{"text": l["text"], "score": l.get("score")} for l in p["lines"]]
            else:
                lines = [{"text": l, "score": None} for l in p["text"].split("\n") if l.strip()]
            page_lines.append({"page": p["page_no"], "lines": lines})
        pieces = chunker.chunk(text, page_lines, size=int(c["size"]), overlap=int(c["overlap"]))
        low = float(c["low_confidence"])
        chunks, terms = [], []
        for i, piece in enumerate(pieces, start=1):
            min_score = piece.get("min_score")
            chunks.append({
                "seq": i, "content": piece["content"], "page_start": piece["page_start"],
                "page_end": piece["page_end"] or piece["page_start"], "confidence": piece["confidence"],
                "needs_review": min_score is not None and min_score < low,
            })
            terms.append(search.index_text(piece["content"]))
        if not chunks:
            self.store.set_status(did, "error", error="The document has no text to index.")
            return
        self.store.replace_chunks(did, chunks, terms)
        self.store.set_status(did, "ready")
        self.jobs.put(("embed", None))

    # ------------------------------------------------------------------ embeddings
    def embedding_backend(self):
        """(signature, embed(texts, kind) -> vectors) or None when semantic search is off."""
        e = self.settings.get()["embeddings"]
        prov = e["provider"]
        if prov == "none":
            return None
        if prov == "local":
            model = e.get("local_model_file")
            if not model or not Path(model).is_file() or not runtime.server_binary():
                return None
            sig = f"local:{Path(model).name}"

            def embed(texts, kind="document"):
                base = runtime.EMBED.ensure(model, context=8192)
                return llm.embed("custom", base, None, Path(model).stem, _prefix(texts, kind, model))["vectors"]

            return sig, embed
        p = self.settings.provider(prov)
        model = e.get("model")
        if not model:
            return None
        ptype = {"ollama": "local"}.get(prov, prov)
        sig = f"{prov}:{model}"

        def embed(texts, kind="document"):
            return llm.embed(ptype, p.get("base_url") or None, p.get("api_key"), model, _prefix(texts, kind, model),
                             kind=kind)["vectors"]

        return sig, embed

    def _embed_pending(self):
        backend = self.embedding_backend()
        if not backend:
            return
        sig, embed = backend
        while True:
            rows = self.store.pending_embeddings(sig, 32)
            if not rows:
                return
            try:
                vecs = embed(["%s\n%s" % (r["document_name"], r["content"]) for r in rows], "document")
            except Exception as exc:  # noqa: BLE001 - retried on the next trigger
                _log.warning("embedding failed: %s", exc)
                self.last_embed_error = str(exc)[:300]
                return
            self.last_embed_error = ""
            self.store.store_vectors([(r["id"], v) for r, v in zip(rows, vecs)], sig)

    last_embed_error = ""

    # ------------------------------------------------------------------ retrieval
    def retrieve(self, query: str, collection_ids: list[int] | None, limit: int = 6, include_flagged=False,
                 semantic=True, info: dict | None = None) -> list[dict]:
        info = info if info is not None else {}
        info.update(mode="lexical", semantic_hits=0)
        query = (query or "")[:4000]
        limit = max(1, min(int(limit), 30))
        lexical = self._lexical(query, collection_ids, limit * 4, include_flagged)
        similar = {}
        backend = self.embedding_backend() if semantic else None
        if backend and query.strip():
            sig, embed = backend
            try:
                qv = np.asarray(embed([query], "query")[0], np.float32)
            except Exception as exc:  # noqa: BLE001
                info.update(mode="lexical_fallback", error=str(exc)[:200])
            else:
                info["mode"] = "hybrid"
                ids, mat = self.store.vectors(sig, collection_ids)
                if len(ids) and mat.shape[1] == qv.shape[0]:
                    sims = mat @ qv
                    floor = float(self.settings.get()["embeddings"]["min_similarity"])
                    order = np.argsort(-sims)[: limit * 4]
                    info["top_similarity"] = float(sims[order[0]]) if len(order) else None
                    similar = {int(ids[i]): float(sims[i]) for i in order if sims[i] >= floor}
                info["semantic_hits"] = len(similar)
        if not lexical and not similar:
            return []
        all_ids = list(dict.fromkeys(list(lexical) + list(similar)))
        if similar:
            lr = {c: i for i, c in enumerate(sorted(lexical, key=lambda c: -lexical[c][0]), start=1)}
            sr = {c: i for i, c in enumerate(sorted(similar, key=lambda c: -similar[c]), start=1)}
            best = 2.0 / (RRF_K + 1)
            scores = {c: sum(1.0 / (RRF_K + r[c]) for r in (lr, sr) if c in r) / best for c in all_ids}
        else:
            scores = {c: lexical[c][0] for c in all_ids}
        ranked = sorted(all_ids, key=lambda c: (-scores[c], c))[:limit]
        out = []
        for cid in ranked:
            ch = self.store.chunk(cid)
            if not ch:
                continue
            out.append({
                "chunk_id": cid, "document_id": ch["document_id"], "document_name": ch["document_name"],
                "document_version": ch["document_version"] or None,
                "page_start": ch["page_start"], "page_end": ch["page_end"] or ch["page_start"],
                "content": ch["content"], "score": round(scores[cid], 4),
                "similarity": round(similar[cid], 4) if cid in similar else None,
                "match": "both" if cid in lexical and cid in similar else ("semantic" if cid in similar else "lexical"),
                "snippet": search.snippet(query, ch["content"]),
            })
        return out

    def _lexical(self, query, collection_ids, n, include_flagged):
        terms = search.query_terms(query)
        surface = [t for t in search.tokens(query) if t not in search.STOPWORDS and len(t) >= 2]
        words = list(dict.fromkeys(terms + surface))
        if not words:
            return {}
        match = " OR ".join('"%s"' % w.replace('"', "") for w in words)
        sql = """SELECT c.id, bm25(chunks_fts) AS rank, c.content FROM chunks_fts
                   JOIN chunks c ON c.id = chunks_fts.rowid
                   JOIN documents d ON d.id = c.document_id
                  WHERE chunks_fts MATCH ? AND d.status = 'ready'"""
        params = [match]
        if not include_flagged:
            sql += " AND c.needs_review = 0"
        if collection_ids:
            sql += " AND d.collection_id IN (%s)" % ",".join("?" * len(collection_ids))
            params += list(collection_ids)
        sql += " ORDER BY rank LIMIT ?"
        params.append(n)
        rows = self.store.all(sql, params)
        if not rows:
            return {}
        best = min(r["rank"] for r in rows) or -1.0  # bm25: lower is better (negative)
        out = {}
        for r in rows:
            cov = search.coverage(query, r["content"])
            rel = (r["rank"] / best) if best else 0.0
            out[r["id"]] = (0.6 * cov + 0.4 * rel, cov)
        return out

    # ------------------------------------------------------------------ answering
    def chat_call(self) -> tuple[dict, str]:
        """Provider call parameters for the configured answer provider."""
        a = self.settings.get()["answer"]
        prov = a["provider"]
        if prov == "local":
            p = self.settings.get()["providers"]["local"]
            if not p.get("model_file"):
                raise DeskError("Choose a local model in Settings (or connect a provider).")
            base = runtime.CHAT.ensure(p["model_file"], context=int(p.get("context") or 8192),
                                       threads=int(p.get("threads") or 0))
            return {"provider_type": "custom", "base_url": base, "api_key": None,
                    "model": Path(p["model_file"]).stem}, Path(p["model_file"]).name
        p = self.settings.provider(prov)
        if not p.get("model"):
            raise DeskError("Set the model name for the selected provider in Settings.")
        ptype = {"ollama": "local"}.get(prov, prov)
        return {"provider_type": ptype, "base_url": p.get("base_url") or None, "api_key": p.get("api_key"),
                "model": p["model"]}, p["model"]

    def _history(self, chat_id, turns):
        msgs = [m for m in self.store.messages(chat_id) if m["role"] in ("user", "assistant") and not m["is_error"]]
        msgs = msgs[-2 * turns:] if turns > 0 else []
        out = [{"role": m["role"], "content": m["content"]} for m in msgs]
        while out and sum(len(m["content"]) for m in out) > 12000:
            out.pop(0)
        while out and out[0]["role"] != "user":
            out.pop(0)
        return out

    def prepare(self, chat_id: int, question: str) -> dict:
        question = (question or "").strip()
        if not question:
            raise DeskError("Please type a question.")
        if len(question) > 4000:
            raise DeskError("The question is too long (4000 characters max).")
        chat = self.store.chat(chat_id)
        if not chat:
            raise DeskError("Conversation not found.")
        a = self.settings.get()["answer"]
        history = self._history(chat_id, int(a["history_turns"]))
        info = {}
        hits = self.retrieve(question, chat["collection_ids"], int(a["max_sources"]),
                             include_flagged=not a["strict"], info=info)
        prep = {"chat_id": chat_id, "question": question, "hits": hits, "strict": bool(a["strict"]),
                "retrieval": info, "started": time.monotonic(), "outcome": None, "call": None}
        if a["strict"] and not hits:
            prep.update(outcome="refused_no_sources", answer=prompt.REFUSAL)
            self._record_question(chat, question)
            return prep
        call, model_label = self.chat_call()  # may raise: then nothing is recorded
        system = prompt.system_prompt(a.get("system_prompt"), a["strict"])
        if "qwen3" in model_label.lower():
            system += "\n/no_think"
        call.update(system=system,
                    messages=history + [{"role": "user", "content": prompt.user_turn(question, hits)}],
                    temperature=a["temperature"], max_tokens=int(a["max_tokens"]), timeout=300)
        prep.update(call=call, model=model_label, provider=a["provider"])
        self._record_question(chat, question)
        return prep

    def _record_question(self, chat, question):
        self.store.add_message(chat["id"], "user", question)
        if chat["title"] in ("", "محادثة جديدة", "New conversation"):
            self.store.update_chat(chat["id"], title=question[:60])

    def complete(self, prep: dict, result: dict | None = None, error: Exception | None = None) -> dict:
        """Validate the model output and store the assistant turn."""
        hits = prep["hits"]
        usage = {}
        check = None
        if prep["outcome"]:
            outcome, answer = prep["outcome"], prep["answer"]
        elif error is not None:
            outcome, answer = "error", "تعذّر الحصول على إجابة من نموذج الذكاء الاصطناعي. حاول مرة أخرى."
            _log.warning("provider error: %s", error)
        else:
            usage = result or {}
            check = prompt.check_citations(strip_thinking(usage.get("text", "")), len(hits), prep["strict"])
            answer = check["answer"]
            outcome = ("refused_ungrounded" if check["replaced"] else "refused_by_model") if check["refused"] else "answered"
        cited = check["cited"] if check else []
        sources = [{"n": n, "chunk_id": hits[n - 1]["chunk_id"], "document_id": hits[n - 1]["document_id"],
                    "document_name": hits[n - 1]["document_name"],
                    "document_version": hits[n - 1].get("document_version"),
                    "page_start": hits[n - 1]["page_start"], "page_end": hits[n - 1]["page_end"]} for n in cited]
        mid = self.store.add_message(
            prep["chat_id"], "assistant", answer, sources=sources,
            grounded=1 if check and check["grounded"] else 0,
            refused=1 if outcome.startswith("refused") else 0, is_error=1 if outcome == "error" else 0,
            outcome=outcome, provider=prep.get("provider", ""), model=prep.get("model", ""),
            tokens_in=int(usage.get("input_tokens") or 0), tokens_out=int(usage.get("output_tokens") or 0),
            latency_ms=int((time.monotonic() - prep["started"]) * 1000))
        return self.store.message(mid)

    def ask(self, chat_id: int, question: str) -> dict:
        prep = self.prepare(chat_id, question)
        if prep["outcome"]:
            return self.complete(prep)
        c = prep["call"]
        try:
            result = llm.chat(c["provider_type"], c["base_url"], c["api_key"], c["model"], c["system"],
                              c["messages"], temperature=c["temperature"], max_tokens=c["max_tokens"],
                              timeout=c["timeout"])
        except (llm.LLMError, runtime.RuntimeError_) as exc:
            return self.complete(prep, error=exc)
        return self.complete(prep, result=result)

    def ask_stream(self, chat_id: int, question: str):
        """Yields ("delta", text) while writing, then ("final", message). Strict mode
        holds text back until it carries a valid citation."""
        prep = self.prepare(chat_id, question)
        if prep["outcome"]:
            yield ("final", self.complete(prep))
            return
        c = prep["call"]
        parts, sent, released = [], 0, not prep["strict"]
        result, error = None, None
        try:
            for kind, data in llm.chat_stream(c["provider_type"], c["base_url"], c["api_key"], c["model"],
                                              c["system"], c["messages"], temperature=c["temperature"],
                                              max_tokens=c["max_tokens"], timeout=c["timeout"]):
                if kind != "delta":
                    result = data
                    continue
                parts.append(data)
                full = "".join(parts)
                if "<think>" in full and "</think>" not in full:
                    continue  # reasoning block: never shown
                visible = strip_thinking(full)
                if not released:
                    if not prompt.has_valid_citation(visible, len(prep["hits"])):
                        continue
                    released = True
                new, sent = visible[sent:], len(visible)
                if new:
                    yield ("delta", new)
        except (llm.LLMError, runtime.RuntimeError_) as exc:
            error = exc
        yield ("final", self.complete(prep, result=result if error is None else None, error=error))

    # ------------------------------------------------------------------ export
    def export_jsonl(self, collection_ids: list[int] | None = None) -> Path:
        """RAG dataset: one JSON object per chunk (text, document, pages, confidence)."""
        import json

        q = """SELECT c.id, c.content, c.page_start, c.page_end, c.confidence, d.name AS document,
                      d.version, d.filename, col.name AS collection
                 FROM chunks c JOIN documents d ON d.id = c.document_id
                 JOIN collections col ON col.id = d.collection_id WHERE d.status = 'ready'"""
        params = []
        if collection_ids:
            q += " AND d.collection_id IN (%s)" % ",".join("?" * len(collection_ids))
            params = list(collection_ids)
        rows = self.store.all(q + " ORDER BY d.id, c.seq", params)
        path = self.data / "exports" / time.strftime("sigmacode-rag-%Y%m%d-%H%M%S.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps({"id": r["id"], "text": r["content"], "document": r["document"],
                                     "file": r["filename"], "version": r["version"] or None,
                                     "collection": r["collection"], "page_start": r["page_start"],
                                     "page_end": r["page_end"], "ocr_confidence": r["confidence"]},
                                    ensure_ascii=False) + "\n")
        return path


def _prefix(texts, kind, model):
    """Instruction prefixes the embedding models were trained with."""
    low = str(model).lower()
    if "qwen3-embedding" in low or "qwen3_embedding" in low:
        if kind == "query":
            return [f"Instruct: Given a question, retrieve passages that answer it\nQuery: {t}" for t in texts]
        return list(texts)
    if "e5" in low:
        return [("query: " if kind == "query" else "passage: ") + t for t in texts]
    return list(texts)
