# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Snytro — https://snytro.com
from __future__ import annotations

import json
import os
import sqlite3
import struct
from contextlib import contextmanager, nullcontext
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
                connection.execute("BEGIN IMMEDIATE")
            statements = [
                f"CREATE TABLE IF NOT EXISTS snytro_documents (id TEXT PRIMARY KEY, collection TEXT NOT NULL, checksum TEXT NOT NULL, record {kind} NOT NULL)",
                "CREATE INDEX IF NOT EXISTS snytro_documents_collection ON snytro_documents(collection)",
                f"CREATE TABLE IF NOT EXISTS snytro_jobs (id TEXT PRIMARY KEY, collection TEXT NOT NULL, record {kind} NOT NULL)",
                f"CREATE TABLE IF NOT EXISTS snytro_chunks (id TEXT PRIMARY KEY, collection TEXT NOT NULL, document_id TEXT NOT NULL REFERENCES snytro_documents(id) ON DELETE CASCADE, content TEXT NOT NULL, locator {kind} NOT NULL, model_key TEXT, embedding {'vector' if self.postgres else 'BLOB'})",
                "CREATE INDEX IF NOT EXISTS snytro_chunks_scope ON snytro_chunks(collection,document_id)",
            ]
            for statement in statements:
                connection.execute(statement)
            vector_type = "vector" if self.postgres else "BLOB"
            columns = {"stage": "TEXT NOT NULL DEFAULT 'chunks'", "context_embedding": vector_type}
            if self.postgres:
                for name, definition in columns.items():
                    connection.execute(f"ALTER TABLE snytro_chunks ADD COLUMN IF NOT EXISTS {name} {definition}")
            else:
                existing = {row["name"] for row in connection.execute("PRAGMA table_info(snytro_chunks)")}
                for name, definition in columns.items():
                    if name not in existing:
                        connection.execute(f"ALTER TABLE snytro_chunks ADD COLUMN {name} {definition}")
            connection.execute("CREATE INDEX IF NOT EXISTS snytro_chunks_stage_model ON snytro_chunks(collection,stage,model_key,document_id)")
            if self.postgres:
                connection.execute("CREATE TABLE IF NOT EXISTS snytro_catalog (id TEXT PRIMARY KEY, collection TEXT NOT NULL, document_id TEXT NOT NULL REFERENCES snytro_documents(id) ON DELETE CASCADE, stage TEXT NOT NULL, content TEXT NOT NULL, data JSONB NOT NULL)")
            else:
                connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS snytro_catalog USING fts5(id UNINDEXED,collection UNINDEXED,document_id UNINDEXED,stage UNINDEXED,content,data UNINDEXED)")
                for dimensions in (768, 2048):
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

    def catalog_paths(self, collection, ids=None):
        where, args = self.scope(collection, ids)
        where = where.replace("document_id IN", "id IN")
        field = "record->>'path'" if self.postgres else "json_extract(record, '$.path')"
        with self.connection() as connection:
            return self.execute(connection, f"SELECT id,{field} AS path FROM snytro_documents WHERE {where} ORDER BY id", args).fetchall()

    def _delete_chunks(self, connection, collection: str, document_id: str):
        if not self.postgres:
            rows = self.execute(connection, "SELECT rowid FROM snytro_chunks WHERE collection=? AND document_id=?", (collection, document_id)).fetchall()
            for row in rows:
                for dimensions in (768, 2048):
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

    def save_document(self, record: dict, chunks: list[dict] | None = None, *, connection=None, invalidate_context=False):
        with (self.connection() if connection is None else nullcontext(connection)) as connection:
            self.execute(connection, "INSERT INTO snytro_documents(id,collection,checksum,record) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET checksum=excluded.checksum,record=excluded.record", (record["id"], record["collection"], record["checksum"], dumps(record)))
            if chunks is not None:
                self._delete_chunks(connection, record["collection"], record["id"])
                self.execute(connection, "DELETE FROM snytro_catalog WHERE collection=? AND document_id=? AND stage='chunks'", (record["collection"], record["id"]))
                for chunk in chunks:
                    stage = chunk.get("stage", "chunks")
                    vector = chunk.get("embedding")
                    context = chunk.get("context_embedding")
                    if vector is not None:
                        vector = validate_vector(vector, chunk["dimensions"])
                    if context is not None:
                        context = validate_vector(context, chunk["dimensions"])
                    def encode(value):
                        if value is None:
                            return None
                        return dumps(value) if self.postgres else struct.pack(f"<{len(value)}f", *value)
                    marker = "?::vector" if self.postgres else "?"
                    self.execute(connection, f"INSERT INTO snytro_chunks(id,collection,document_id,content,locator,model_key,embedding,stage,context_embedding) VALUES(?,?,?,?,?,?,{marker},?,{marker})", (chunk["id"], record["collection"], record["id"], chunk["content"], dumps(chunk["locator"]), chunk.get("model_key"), encode(vector), stage, encode(context)))
                    if stage != "chunks":
                        continue
                    data = {"chunk_id": chunk["id"], "document_id": record["id"], "source_file_name": record["name"], "path": record["path"], "checksum": record["checksum"], "locator": chunk["locator"], "content": chunk["content"]}
                    self.execute(connection, "INSERT INTO snytro_catalog(id,collection,document_id,stage,content,data) VALUES(?,?,?,?,?,?)", (chunk["id"], record["collection"], record["id"], "chunks", chunk["content"], dumps(data)))
            self._catalog(connection, record)
            if invalidate_context:
                self.execute(connection, "DELETE FROM snytro_chunks WHERE collection=? AND document_id=? AND stage!='chunks'", (record["collection"], record["id"]))
                self.execute(connection, "UPDATE snytro_chunks SET context_embedding=NULL WHERE collection=? AND document_id=?", (record["collection"], record["id"]))

    def export_chunks(self, collection, document_id):
        cast = "::text" if self.postgres else ""
        with self.connection() as connection:
            rows = self.execute(connection, f"""SELECT id,content,locator,model_key,stage,
                embedding{cast} AS embedding,context_embedding{cast} AS context_embedding
                FROM snytro_chunks WHERE collection=? AND document_id=? ORDER BY id""", (collection, document_id)).fetchall()
        chunks = []
        for row in rows:
            item = dict(row)
            item["locator"] = decoded(item["locator"])
            for field in ("embedding", "context_embedding"):
                value = item[field]
                if value is not None:
                    item[field] = decoded(value) if self.postgres else list(struct.unpack(f"<{len(value)//4}f", value))
            if item["embedding"] is not None:
                item["dimensions"] = len(item["embedding"])
            chunks.append(item)
        return chunks

    def vector_count(self, collection, document_id):
        with self.connection() as connection:
            return self.execute(connection, "SELECT COUNT(*) AS n FROM snytro_chunks WHERE collection=? AND document_id=? AND embedding IS NOT NULL",
                                (collection, document_id)).fetchone()["n"]

    def lexical(self, collection: str, query: str, stage: str, ids: list[str] | None, limit: int, ranks=None):
        words = list(dict.fromkeys(word for word in terms(query) if word not in SEARCH_STOPWORDS))[:40]
        if not words or ids == []:
            return []
        scope, args = self.scope(collection, ids)
        with self.connection() as connection:
            if self.postgres:
                sql = f"""SELECT data,ts_rank(to_tsvector('simple',content),to_tsquery('simple',?)) AS score,
                    COALESCE((?::jsonb ->> document_id)::int,1) AS authority_rank FROM snytro_catalog
                    WHERE {scope} AND stage=? AND to_tsvector('simple',content) @@ to_tsquery('simple',?)
                    ORDER BY score DESC,authority_rank,id LIMIT ?"""
                query_text = " | ".join(words)
                rows = self.execute(connection, sql, [query_text, dumps(ranks or {}), *args, stage, query_text, limit]).fetchall()
            else:
                connection.create_function("snytro_rank", 1, lambda doc_id: (ranks or {}).get(doc_id, 1), deterministic=True)
                query_text = " OR ".join('"' + word + '"' for word in words)
                rows = connection.execute(f"SELECT data,-bm25(snytro_catalog) AS score,snytro_rank(document_id) AS authority_rank FROM snytro_catalog WHERE {scope} AND stage=? AND snytro_catalog MATCH ? ORDER BY bm25(snytro_catalog),authority_rank,id LIMIT ?", [*args, stage, query_text, limit]).fetchall()
        return [{**decoded(row["data"]), "score": float(row["score"]), "authority_rank": row["authority_rank"], "evidence_level": stage} for row in rows]

    def exact(self, collection, literal, stage, ids, limit, ranks=None):
        literal = literal.strip().casefold()
        if not literal or ids == []:
            return []
        scope, args = self.scope(collection, ids)
        # Scan stored units for raw exact search, including matches that span
        # chunk boundaries. Keep the original spelling and source locator.
        if stage == "chunks":
            with self.connection() as connection:
                where = scope.replace("document_id IN", "id IN")
                records = self.execute(connection, f"SELECT record FROM snytro_documents WHERE {where} ORDER BY id", args)
                matches = []
                for row in records:
                    record = decoded(row["record"])
                    rank = (ranks or {}).get(record["id"], 1)
                    for unit in record["units"]:
                        folded = unit["content"].casefold()
                        if literal in folded:
                            matches.append({"document_id": record["id"], "source_file_name": record["name"],
                                            "path": record["path"], "checksum": record["checksum"],
                                            "unit": unit["number"], "locator": unit["locator"],
                                            "authority_rank": rank, "evidence_level": "exact"})
                            if len(matches) > limit:
                                matches.sort(key=lambda hit: (hit["authority_rank"], hit["document_id"], hit["unit"]))
                                matches.pop()
                return sorted(matches, key=lambda hit: (hit["authority_rank"], hit["document_id"], hit["unit"]))
        with self.connection() as connection:
            matches = []
            for row in self.execute(connection, f"SELECT data,content FROM snytro_catalog WHERE {scope} AND stage=? ORDER BY id", [*args, stage]):
                if literal in row["content"].casefold():
                    data = decoded(row["data"])
                    matches.append({**data, "authority_rank": (ranks or {}).get(data["document_id"], 1), "evidence_level": stage})
            return sorted(matches, key=lambda hit: hit["authority_rank"])[:limit]

    def semantic(self, collection: str, vector: list[float], key: str, ids: list[str] | None, limit: int,
                 stage="chunks", ranks=None):
        if ids == []:
            return []
        vector = validate_vector(vector, len(vector))
        scope, args = self.scope(collection, ids, "c")
        with self.connection() as connection:
            rank_data = dumps(ranks or {})
            if self.postgres:
                query = dumps(vector)
                distances = "LEAST(c.embedding <=> ?::vector, COALESCE(c.context_embedding <=> ?::vector, 2.0))"
                rank_sql = "COALESCE((?::jsonb ->> c.document_id)::int, 1)"
            else:
                query = struct.pack(f"<{len(vector)}f", *vector)
                distances = "MIN(vec_distance_cosine(c.embedding, ?), CASE WHEN c.context_embedding IS NULL THEN 2.0 ELSE vec_distance_cosine(c.context_embedding, ?) END)"
                authority = ranks or {}
                connection.create_function("snytro_rank", 1, lambda document_id: authority.get(document_id, 1), deterministic=True)
                rank_sql = "snytro_rank(c.document_id)"
            # Apply stage, model and source eligibility before top-k.
            rank_params = [rank_data] if self.postgres else []
            rows = self.execute(connection, f"""SELECT * FROM (
                SELECT c.id,c.document_id,c.content,c.locator,{distances} AS distance,{rank_sql} AS authority_rank
                FROM snytro_chunks c WHERE {scope} AND c.stage=? AND c.model_key=? AND c.embedding IS NOT NULL
                ) scored ORDER BY distance + 0.025 * (authority_rank - 1),distance,id LIMIT ?""",
                [query, query, *rank_params, *args, stage, key, limit]).fetchall()
            results = []
            for row in rows:
                record = self.execute(connection, "SELECT record FROM snytro_documents WHERE id=? AND collection=?", (row["document_id"], collection)).fetchone()
                document = decoded(record["record"])
                locator = decoded(row["locator"])
                results.append({"chunk_id": row["id"], "document_id": row["document_id"], "source_file_name": document["name"], "path": document["path"], "checksum": document["checksum"], "locator": locator, "unit": locator.get("unit"), "content": row["content"], "distance": float(row["distance"]), "authority_rank": row["authority_rank"], "evidence_level": stage})
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
