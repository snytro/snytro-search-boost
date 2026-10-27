# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Snytro — https://snytro.com
from __future__ import annotations

import json
import os
import sqlite3
import struct
from contextlib import contextmanager
from pathlib import Path

from .embeddings import validate_vector
from .extract import SEARCH_STOPWORDS, terms
from .settings import private_dir


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def decoded(value):
    return json.loads(value) if isinstance(value, str) else value


class Store:
    """A dedicated CLI database, never the desktop app's session tables.

    All public operations take a collection; explicit empty ID scopes match nothing.
    SQLite vector and FTS updates share the document transaction. PostgreSQL uses
    the same records with JSONB/pgvector and its own SQL search expressions.
    """

    def __init__(self, root: Path, settings: dict):
        self.root, self.settings = root, settings
        self.postgres = settings["storage"] == "postgres"

    @contextmanager
    def connection(self):
        if self.postgres:
            import psycopg
            from psycopg.rows import dict_row
            dsn = os.environ.get("SNYTRO_POSTGRES_URL")
            if not dsn and self.settings.get("managed_postgres"):
                from .postgres import managed_dsn, start_managed
                start_managed(self.root, self.settings["managed_postgres"])
                dsn = managed_dsn(self.root, self.settings["managed_postgres"])
            if not dsn:
                raise ValueError("Set SNYTRO_POSTGRES_URL to a dedicated PostgreSQL database with pgvector.")
            try:
                connection = psycopg.connect(dsn, row_factory=dict_row, connect_timeout=8)
            except psycopg.Error:
                raise ValueError("PostgreSQL connection failed. Check SNYTRO_POSTGRES_URL and server readiness.") from None
        else:
            connection = sqlite3.connect(self.root / "documents.sqlite3", timeout=30)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
            import sqlite_vec
            connection.enable_load_extension(True)
            try:
                sqlite_vec.load(connection)
            finally:
                connection.enable_load_extension(False)
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def execute(self, connection, query: str, params=()):
        return connection.execute(query.replace("?", "%s") if self.postgres else query, params)

    def initialize(self):
        private_dir(self.root)
        kind = "JSONB" if self.postgres else "TEXT"
        with self.connection() as connection:
            if self.postgres:
                # Do not install extensions into a user's server implicitly.
                exists = connection.execute("SELECT 1 FROM pg_extension WHERE extname='vector'").fetchone()
                if not exists:
                    raise ValueError("PostgreSQL needs pgvector enabled in this database (CREATE EXTENSION vector).")
            else:
                connection.execute("PRAGMA journal_mode=WAL")
            statements = [
                f"CREATE TABLE IF NOT EXISTS snytro_documents (id TEXT PRIMARY KEY, collection TEXT NOT NULL, checksum TEXT NOT NULL, record {kind} NOT NULL)",
                "CREATE INDEX IF NOT EXISTS snytro_documents_collection ON snytro_documents(collection)",
                f"CREATE TABLE IF NOT EXISTS snytro_jobs (id TEXT PRIMARY KEY, collection TEXT NOT NULL, record {kind} NOT NULL)",
                f"CREATE TABLE IF NOT EXISTS snytro_chunks (id TEXT PRIMARY KEY, collection TEXT NOT NULL, document_id TEXT NOT NULL REFERENCES snytro_documents(id) ON DELETE CASCADE, content TEXT NOT NULL, locator {kind} NOT NULL, model_key TEXT, embedding {'vector' if self.postgres else 'BLOB'})",
                "CREATE INDEX IF NOT EXISTS snytro_chunks_scope ON snytro_chunks(collection,document_id)",
            ]
            for statement in statements:
                connection.execute(statement)
            if self.postgres:
                connection.execute("CREATE TABLE IF NOT EXISTS snytro_catalog (id TEXT PRIMARY KEY, collection TEXT NOT NULL, document_id TEXT NOT NULL REFERENCES snytro_documents(id) ON DELETE CASCADE, stage TEXT NOT NULL, content TEXT NOT NULL, data JSONB NOT NULL)")
            else:
                connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS snytro_catalog USING fts5(id UNINDEXED,collection UNINDEXED,document_id UNINDEXED,stage UNINDEXED,content,data UNINDEXED)")
                for dimensions in (384, 768, 2048):
                    connection.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS snytro_vectors_{dimensions} USING vec0(collection TEXT PARTITION KEY, document_id TEXT, model_key TEXT, embedding float[{dimensions}])")
        if not self.postgres and os.name != "nt":
            for name in ("documents.sqlite3", "documents.sqlite3-wal", "documents.sqlite3-shm"):
                path = self.root / name
                if path.exists():
                    path.chmod(0o600)

    def scope(self, collection: str, ids: list[str] | None, alias: str = ""):
        prefix = f"{alias}." if alias else ""
        clause, args = f"{prefix}collection=?", [collection]
        if ids is not None:
            if not ids:
                clause += " AND 1=0"
            else:
                clause += f" AND {prefix}document_id IN ({','.join('?' for _ in ids)})"
                args.extend(ids)
        return clause, args

    def documents(self, collection: str, ids: list[str] | None = None, limit: int = 100, offset: int = 0):
        # documents use id, rather than document_id, for their primary key.
        clause, args = self.scope(collection, ids)
        clause = clause.replace("document_id IN", "id IN")
        with self.connection() as connection:
            rows = self.execute(connection, f"SELECT record FROM snytro_documents WHERE {clause} ORDER BY id LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()
        return [decoded(row["record"]) for row in rows]

    def document(self, collection: str, document_id: str):
        records = self.documents(collection, [document_id], 1)
        if not records:
            raise ValueError("Document is not in the selected collection.")
        return records[0]

    def _delete_chunks(self, connection, collection: str, document_id: str):
        if not self.postgres:
            rows = self.execute(connection, "SELECT rowid FROM snytro_chunks WHERE collection=? AND document_id=?", (collection, document_id)).fetchall()
            for row in rows:
                for dimensions in (384, 768, 2048):
                    connection.execute(f"DELETE FROM snytro_vectors_{dimensions} WHERE rowid=?", (row["rowid"],))
        self.execute(connection, "DELETE FROM snytro_chunks WHERE collection=? AND document_id=?", (collection, document_id))

    def _catalog(self, connection, record: dict):
        self.execute(connection, "DELETE FROM snytro_catalog WHERE collection=? AND document_id=? AND stage!='chunks'", (record["collection"], record["id"]))
        common = {"document_id": record["id"], "source_file_name": record["name"], "path": record["path"], "checksum": record["checksum"], "summary_kind": record["summary_kind"], "status": record["status"]}
        entries = [
            ("taglines", "document", record["name"] + " " + record["tagline"] + " " + " ".join(record["keywords"]), {"tagline": record["tagline"], "keywords": record["keywords"]}),
            ("summaries", "document", record["summary"], {"summary": record["summary"]}),
        ]
        for unit in record["units"]:
            entries.append(("pages", str(unit["number"]), unit["summary"] + " " + unit["tagline"], {"unit": unit["number"], "locator": unit["locator"], "summary": unit["summary"], "tagline": unit["tagline"], "summary_kind": unit["summary_kind"]}))
        for stage, part, content, data in entries:
            self.execute(connection, "INSERT INTO snytro_catalog(id,collection,document_id,stage,content,data) VALUES(?,?,?,?,?,?)", (record["id"] + ":" + stage + ":" + part, record["collection"], record["id"], stage, content, dumps({**common, **data})))

    def save_document(self, record: dict, chunks: list[dict] | None = None):
        with self.connection() as connection:
            self.execute(connection, "INSERT INTO snytro_documents(id,collection,checksum,record) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET checksum=excluded.checksum,record=excluded.record", (record["id"], record["collection"], record["checksum"], dumps(record)))
            if chunks is not None:
                self._delete_chunks(connection, record["collection"], record["id"])
                self.execute(connection, "DELETE FROM snytro_catalog WHERE collection=? AND document_id=? AND stage='chunks'", (record["collection"], record["id"]))
                for chunk in chunks:
                    vector = chunk.get("embedding")
                    if vector is not None:
                        vector = validate_vector(vector, chunk["dimensions"])
                    embedding = dumps(vector) if self.postgres and vector is not None else struct.pack(f"<{len(vector)}f", *vector) if vector is not None else None
                    marker = "?::vector" if self.postgres else "?"
                    cursor = self.execute(connection, f"INSERT INTO snytro_chunks(id,collection,document_id,content,locator,model_key,embedding) VALUES(?,?,?,?,?,?,{marker})", (chunk["id"], record["collection"], record["id"], chunk["content"], dumps(chunk["locator"]), chunk.get("model_key"), embedding))
                    if vector is not None and not self.postgres:
                        connection.execute(f"INSERT INTO snytro_vectors_{chunk['dimensions']}(rowid,collection,document_id,model_key,embedding) VALUES(?,?,?,?,?)", (cursor.lastrowid, record["collection"], record["id"], chunk["model_key"], embedding))
                    data = {"chunk_id": chunk["id"], "document_id": record["id"], "source_file_name": record["name"], "path": record["path"], "checksum": record["checksum"], "locator": chunk["locator"], "content": chunk["content"]}
                    self.execute(connection, "INSERT INTO snytro_catalog(id,collection,document_id,stage,content,data) VALUES(?,?,?,?,?,?)", (chunk["id"], record["collection"], record["id"], "chunks", chunk["content"], dumps(data)))
            self._catalog(connection, record)

    def lexical(self, collection: str, query: str, stage: str, ids: list[str] | None, limit: int):
        words = list(dict.fromkeys(word for word in terms(query) if word not in SEARCH_STOPWORDS))[:40]
        if not words or ids == []:
            return []
        scope, args = self.scope(collection, ids)
        with self.connection() as connection:
            if self.postgres:
                sql = f"SELECT data,ts_rank(to_tsvector('simple',content),to_tsquery('simple',?)) AS score FROM snytro_catalog WHERE {scope} AND stage=? AND to_tsvector('simple',content) @@ to_tsquery('simple',?) ORDER BY score DESC LIMIT ?"
                query_text = " | ".join(words)
                rows = self.execute(connection, sql, [query_text, *args, stage, query_text, limit]).fetchall()
            else:
                query_text = " OR ".join('"' + word + '"' for word in words)
                rows = connection.execute(f"SELECT data,-bm25(snytro_catalog) AS score FROM snytro_catalog WHERE {scope} AND stage=? AND snytro_catalog MATCH ? ORDER BY bm25(snytro_catalog) LIMIT ?", [*args, stage, query_text, limit]).fetchall()
        return [{**decoded(row["data"]), "score": float(row["score"]), "evidence_level": stage} for row in rows]

    def semantic(self, collection: str, vector: list[float], key: str, ids: list[str] | None, limit: int):
        if ids == []:
            return []
        vector = validate_vector(vector, len(vector))
        scope, args = self.scope(collection, ids, "c")
        with self.connection() as connection:
            if self.postgres:
                rows = self.execute(connection, f"SELECT c.*,c.embedding <=> ?::vector AS distance FROM snytro_chunks c WHERE {scope} AND model_key=? AND embedding IS NOT NULL ORDER BY distance LIMIT ?", [dumps(vector), *args, key, limit]).fetchall()
            else:
                # Filters apply DURING KNN, never after a global top-k.
                where, parameters = self.scope(collection, ids)
                rows = connection.execute(f"SELECT rowid,distance FROM snytro_vectors_{len(vector)} WHERE {where} AND model_key=? AND embedding MATCH ? AND k=? ORDER BY distance", [*parameters, key, struct.pack(f"<{len(vector)}f", *vector), limit]).fetchall()
                result = []
                for row in rows:
                    chunk = connection.execute(f"SELECT c.* FROM snytro_chunks c WHERE rowid=? AND {scope} AND model_key=?", [row["rowid"], *args, key]).fetchone()
                    if chunk:
                        result.append({**dict(chunk), "distance": row["distance"]})
                rows = result
            results = []
            for row in rows:
                record = self.execute(connection, "SELECT record FROM snytro_documents WHERE id=? AND collection=?", (row["document_id"], collection)).fetchone()
                document = decoded(record["record"])
                results.append({"chunk_id": row["id"], "document_id": row["document_id"], "source_file_name": document["name"], "path": document["path"], "checksum": document["checksum"], "locator": decoded(row["locator"]), "content": row["content"], "distance": float(row["distance"]), "evidence_level": "chunks"})
            return results

    def save_job(self, job: dict):
        with self.connection() as connection:
            self.execute(connection, "INSERT INTO snytro_jobs(id,collection,record) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET record=excluded.record", (job["id"], job["collection"], dumps(job)))

    def job(self, collection: str, job_id: str):
        with self.connection() as connection:
            row = self.execute(connection, "SELECT record FROM snytro_jobs WHERE collection=? AND id=?", (collection, job_id)).fetchone()
        if not row:
            raise ValueError("Job is not in the selected collection.")
        return decoded(row["record"])

    def remove(self, collection: str, ids: list[str]):
        with self.connection() as connection:
            for document_id in ids:
                self._delete_chunks(connection, collection, document_id)
                self.execute(connection, "DELETE FROM snytro_catalog WHERE collection=? AND document_id=?", (collection, document_id))
                self.execute(connection, "DELETE FROM snytro_documents WHERE collection=? AND id=?", (collection, document_id))
