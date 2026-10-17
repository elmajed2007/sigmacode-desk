"""SQLite store: collections, documents, pages, chunks (FTS5 + vectors), chats, log.

One connection guarded by a lock (the app is single-user; ingestion runs in a
worker thread). WAL mode keeps the UI responsive while a document is indexed.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS collections (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    name TEXT NOT NULL, filename TEXT NOT NULL, stored_path TEXT NOT NULL,
    kind TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',   -- queued|processing|review|ready|error
    progress REAL DEFAULT 0, page_count INTEGER DEFAULT 0, confidence REAL,
    method TEXT DEFAULT '',                   -- text|ocr|mixed
    error TEXT DEFAULT '', version TEXT DEFAULT '',
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS documents_collection ON documents(collection_id);
CREATE TABLE IF NOT EXISTS pages (
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_no INTEGER NOT NULL, text TEXT NOT NULL DEFAULT '', method TEXT NOT NULL,
    confidence REAL, lines TEXT, edited INTEGER DEFAULT 0,
    PRIMARY KEY (document_id, page_no)
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL, content TEXT NOT NULL, page_start INTEGER, page_end INTEGER,
    confidence REAL, needs_review INTEGER DEFAULT 0,
    embedding BLOB, embed_sig TEXT
);
CREATE INDEX IF NOT EXISTS chunks_document ON chunks(document_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(terms, tokenize = 'unicode61 remove_diacritics 0');
CREATE TABLE IF NOT EXISTS chats (
    id INTEGER PRIMARY KEY, title TEXT NOT NULL, collection_ids TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    role TEXT NOT NULL, content TEXT NOT NULL, sources TEXT DEFAULT '[]',
    grounded INTEGER DEFAULT 0, refused INTEGER DEFAULT 0, is_error INTEGER DEFAULT 0,
    outcome TEXT DEFAULT '', provider TEXT DEFAULT '', model TEXT DEFAULT '',
    tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0, latency_ms INTEGER DEFAULT 0,
    feedback TEXT DEFAULT '', created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_chat ON messages(chat_id);
"""


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        self._vec_cache: dict = {}
        if not self.one("SELECT id FROM collections LIMIT 1"):
            self.execute("INSERT INTO collections(name, description, created_at) VALUES (?, ?, ?)",
                         ("مستنداتي", "", time.time()))

    # -------------------------------------------------------------- helpers
    def execute(self, sql, params=()):
        with self.lock:
            return self.db.execute(sql, params)

    def one(self, sql, params=()):
        with self.lock:
            row = self.db.execute(sql, params).fetchone()
            return dict(row) if row else None

    def all(self, sql, params=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, params).fetchall()]

    def transaction(self):
        store = self

        class _Tx:
            def __enter__(self):
                store.lock.acquire()
                store.db.execute("BEGIN")
                return store.db

            def __exit__(self, exc_type, exc, tb):
                try:
                    store.db.execute("ROLLBACK" if exc_type else "COMMIT")
                finally:
                    store.lock.release()
                return False

        return _Tx()

    # -------------------------------------------------------------- collections
    def collections(self):
        return self.all(
            """SELECT c.*, (SELECT count(*) FROM documents d WHERE d.collection_id = c.id) AS documents
                 FROM collections c ORDER BY c.id""")

    def add_collection(self, name, description=""):
        cur = self.execute("INSERT INTO collections(name, description, created_at) VALUES (?, ?, ?)",
                           (name.strip(), description or "", time.time()))
        return cur.lastrowid

    def rename_collection(self, cid, name, description=None):
        self.execute("UPDATE collections SET name = ?, description = coalesce(?, description) WHERE id = ?",
                     (name.strip(), description, cid))

    def delete_collection(self, cid):
        with self.transaction() as db:
            ids = [r[0] for r in db.execute(
                "SELECT c.id FROM chunks c JOIN documents d ON d.id = c.document_id WHERE d.collection_id = ?", (cid,))]
            db.executemany("DELETE FROM chunks_fts WHERE rowid = ?", [(i,) for i in ids])
            db.execute("DELETE FROM collections WHERE id = ?", (cid,))
        self._vec_cache.clear()

    # -------------------------------------------------------------- documents
    def add_document(self, collection_id, name, filename, stored_path, kind, sha256, size):
        now = time.time()
        cur = self.execute(
            """INSERT INTO documents(collection_id, name, filename, stored_path, kind, sha256, size,
                                     status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)""",
            (collection_id, name, filename, str(stored_path), kind, sha256, size, now, now))
        return cur.lastrowid

    def document(self, did):
        return self.one("SELECT * FROM documents WHERE id = ?", (did,))

    def documents(self, collection_id=None):
        sql = """SELECT d.*, (SELECT count(*) FROM chunks c WHERE c.document_id = d.id) AS chunks
                   FROM documents d"""
        if collection_id:
            return self.all(sql + " WHERE d.collection_id = ? ORDER BY d.id DESC", (collection_id,))
        return self.all(sql + " ORDER BY d.id DESC")

    def set_status(self, did, status, **fields):
        fields.update(status=status, updated_at=time.time())
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE documents SET {cols} WHERE id = ?", (*fields.values(), did))

    def update_document(self, did, **fields):
        allowed = {k: v for k, v in fields.items() if k in ("name", "version", "collection_id")}
        if allowed:
            allowed["updated_at"] = time.time()
            cols = ", ".join(f"{k} = ?" for k in allowed)
            self.execute(f"UPDATE documents SET {cols} WHERE id = ?", (*allowed.values(), did))

    def delete_document(self, did):
        with self.transaction() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM chunks WHERE document_id = ?", (did,))]
            db.executemany("DELETE FROM chunks_fts WHERE rowid = ?", [(i,) for i in ids])
            db.execute("DELETE FROM documents WHERE id = ?", (did,))
        self._vec_cache.clear()

    def find_duplicate(self, collection_id, sha256):
        return self.one("SELECT id, name FROM documents WHERE collection_id = ? AND sha256 = ?",
                        (collection_id, sha256))

    # -------------------------------------------------------------- pages
    def save_pages(self, did, pages):
        with self.transaction() as db:
            db.execute("DELETE FROM pages WHERE document_id = ?", (did,))
            db.executemany(
                "INSERT INTO pages(document_id, page_no, text, method, confidence, lines) VALUES (?, ?, ?, ?, ?, ?)",
                [(did, p["page"], p["text"], p["method"], p.get("confidence"),
                  json.dumps(p.get("lines") or [], ensure_ascii=False)) for p in pages])

    def pages(self, did):
        rows = self.all("SELECT * FROM pages WHERE document_id = ? ORDER BY page_no", (did,))
        for r in rows:
            r["lines"] = json.loads(r["lines"] or "[]")
        return rows

    def edit_page(self, did, page_no, text):
        self.execute("UPDATE pages SET text = ?, edited = 1 WHERE document_id = ? AND page_no = ?",
                     (text, did, page_no))

    # -------------------------------------------------------------- chunks
    def replace_chunks(self, did, chunks, terms):
        """chunks: list of dicts; terms: matching list of FTS index strings."""
        with self.transaction() as db:
            old = [r[0] for r in db.execute("SELECT id FROM chunks WHERE document_id = ?", (did,))]
            db.executemany("DELETE FROM chunks_fts WHERE rowid = ?", [(i,) for i in old])
            db.execute("DELETE FROM chunks WHERE document_id = ?", (did,))
            for c, t in zip(chunks, terms):
                cur = db.execute(
                    """INSERT INTO chunks(document_id, seq, content, page_start, page_end, confidence, needs_review)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (did, c["seq"], c["content"], c.get("page_start"), c.get("page_end"),
                     c.get("confidence"), 1 if c.get("needs_review") else 0))
                db.execute("INSERT INTO chunks_fts(rowid, terms) VALUES (?, ?)", (cur.lastrowid, t))
        self._vec_cache.clear()

    def chunks(self, did):
        return self.all("""SELECT id, seq, content, page_start, page_end, confidence, needs_review,
                                  embed_sig IS NOT NULL AS embedded
                             FROM chunks WHERE document_id = ? ORDER BY seq""", (did,))

    def chunk(self, cid):
        return self.one("""SELECT c.id, c.document_id, c.seq, c.content, c.page_start, c.page_end,
                                  c.confidence, d.name AS document_name, d.version AS document_version,
                                  d.kind, d.stored_path, d.collection_id
                             FROM chunks c JOIN documents d ON d.id = c.document_id WHERE c.id = ?""", (cid,))

    def pending_embeddings(self, signature, limit=64):
        return self.all("""SELECT c.id, c.content, d.name AS document_name FROM chunks c
                             JOIN documents d ON d.id = c.document_id
                            WHERE d.status = 'ready' AND (c.embed_sig IS NULL OR c.embed_sig != ?)
                            ORDER BY c.id LIMIT ?""", (signature, limit))

    def store_vectors(self, items, signature):
        with self.transaction() as db:
            db.executemany("UPDATE chunks SET embedding = ?, embed_sig = ? WHERE id = ?",
                           [(np.asarray(v, np.float32).tobytes(), signature, cid) for cid, v in items])
        self._vec_cache.clear()

    def embedding_progress(self, signature):
        row = self.one("""SELECT count(*) AS total, sum(c.embed_sig = ?) AS done FROM chunks c
                            JOIN documents d ON d.id = c.document_id WHERE d.status = 'ready'""", (signature,))
        return {"total": row["total"] or 0, "done": row["done"] or 0}

    def vectors(self, signature, collection_ids):
        """(ids array, matrix) of embedded chunks in the given collections, cached."""
        key = (signature, tuple(sorted(collection_ids or [])))
        if key in self._vec_cache:
            return self._vec_cache[key]
        q = """SELECT c.id, c.embedding FROM chunks c JOIN documents d ON d.id = c.document_id
                WHERE d.status = 'ready' AND c.embed_sig = ? AND c.needs_review = 0"""
        params = [signature]
        if collection_ids:
            q += " AND d.collection_id IN (%s)" % ",".join("?" * len(collection_ids))
            params += list(collection_ids)
        rows = self.all(q, params)
        if rows:
            ids = np.array([r["id"] for r in rows], np.int64)
            mat = np.vstack([np.frombuffer(r["embedding"], np.float32) for r in rows])
        else:
            ids, mat = np.zeros(0, np.int64), np.zeros((0, 1), np.float32)
        self._vec_cache[key] = (ids, mat)
        return ids, mat

    # -------------------------------------------------------------- chats
    def chats(self):
        return self.all("SELECT * FROM chats ORDER BY updated_at DESC")

    def add_chat(self, title, collection_ids):
        now = time.time()
        cur = self.execute("INSERT INTO chats(title, collection_ids, created_at, updated_at) VALUES (?, ?, ?, ?)",
                           (title, json.dumps(collection_ids or []), now, now))
        return cur.lastrowid

    def chat(self, chat_id):
        row = self.one("SELECT * FROM chats WHERE id = ?", (chat_id,))
        if row:
            row["collection_ids"] = json.loads(row["collection_ids"] or "[]")
        return row

    def update_chat(self, chat_id, **fields):
        if "collection_ids" in fields:
            fields["collection_ids"] = json.dumps(fields["collection_ids"] or [])
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE chats SET {cols} WHERE id = ?", (*fields.values(), chat_id))

    def delete_chat(self, chat_id):
        self.execute("DELETE FROM chats WHERE id = ?", (chat_id,))

    def messages(self, chat_id):
        rows = self.all("SELECT * FROM messages WHERE chat_id = ? ORDER BY id", (chat_id,))
        for r in rows:
            r["sources"] = json.loads(r["sources"] or "[]")
        return rows

    def add_message(self, chat_id, role, content, **fields):
        fields.setdefault("sources", [])
        fields["sources"] = json.dumps(fields["sources"], ensure_ascii=False)
        cols = ["chat_id", "role", "content", "created_at", *fields.keys()]
        vals = [chat_id, role, content, time.time(), *fields.values()]
        cur = self.execute(f"INSERT INTO messages({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
        self.execute("UPDATE chats SET updated_at = ? WHERE id = ?", (time.time(), chat_id))
        return cur.lastrowid

    def message(self, mid):
        row = self.one("SELECT * FROM messages WHERE id = ?", (mid,))
        if row:
            row["sources"] = json.loads(row["sources"] or "[]")
        return row

    def stats(self):
        return {
            "documents": self.one("SELECT count(*) AS n FROM documents")["n"],
            "ready": self.one("SELECT count(*) AS n FROM documents WHERE status = 'ready'")["n"],
            "review": self.one("SELECT count(*) AS n FROM documents WHERE status = 'review'")["n"],
            "chunks": self.one("SELECT count(*) AS n FROM chunks")["n"],
            "questions": self.one("SELECT count(*) AS n FROM messages WHERE role = 'assistant'")["n"],
        }
