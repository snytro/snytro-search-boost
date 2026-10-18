# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Snytro — https://snytro.com
from __future__ import annotations

import hashlib
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .embeddings import MODELS, Embedder, model_key
from .extract import checksum, discover, extract_units, overview, text_chunks
from .settings import private_dir
from .storage import Store


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def public_document(record: dict) -> dict:
    return {key: value for key, value in record.items() if key not in {"units", "extractions"}}


@contextmanager
def collection_lock(root: Path, collection: str):
    import json
    import psutil
    lock = root / "locks" / (collection + ".json")
    private_dir(lock.parent)
    for attempt in range(2):
        try:
            descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as handle:
                json.dump({"pid": os.getpid(), "created": psutil.Process().create_time()}, handle)
            break
        except FileExistsError:
            # Empty lock during creation counts as busy; never steal a live writer lock.
            try:
                owner = json.loads(lock.read_text())
                live = psutil.pid_exists(owner["pid"]) and psutil.Process(owner["pid"]).create_time() == owner["created"]
            except (ValueError, KeyError, psutil.Error):
                live = time.time() - lock.stat().st_mtime < 300
            if live or attempt:
                raise ValueError("This collection is busy. Check its ingestion job and retry after completion.") from None
            lock.unlink(missing_ok=True)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


class Engine:
    def __init__(self, root: Path, settings: dict, collection: str):
        self.root, self.settings, self.collection = root, settings, collection
        self.store = Store(root, settings)
        self._embedder = None

    def embedder(self):
        if self._embedder is None:
            self._embedder = Embedder(self.root, self.settings)
        return self._embedder

    def make_chunks(self, record: dict, semantic: bool = False):
        chunks = []
        embedder = self.embedder() if semantic else None
        for unit in record["units"]:
            texts = embedder.chunks(unit["content"]) if semantic else text_chunks(unit["content"])
            # Inference is bounded per unit batch; do not encode a whole folder at once.
            for start in range(0, len(texts), self.settings["batch_size"]):
                batch = texts[start:start + self.settings["batch_size"]]
                vectors = embedder.encode(batch) if semantic else [None] * len(batch)
                for index, (text, vector) in enumerate(zip(batch, vectors), start):
                    chunk = {"id": f"{record['id']}:u{unit['number']}:c{index}", "content": text,
                             "locator": {**unit["locator"], "unit": unit["number"], "chunk": index}}
                    if semantic:
                        chunk.update(embedding=vector, dimensions=MODELS[self.settings["model"]]["dimensions"], model_key=model_key(self.settings))
                    chunks.append(chunk)
        return chunks

    def index_file(self, path: Path, *, lexical_only: bool = False):
        path = path.expanduser().resolve(strict=True)
        document_id = "doc_" + hashlib.sha256((self.collection + "\0" + str(path)).encode()).hexdigest()[:32]
        digest = checksum(path)
        try:
            existing = self.store.document(self.collection, document_id)
        except ValueError:
            existing = None
        if existing and existing["checksum"] == digest and existing.get("status") != "failed":
            record = existing
            changed = False
        else:
            units = extract_units(path)
            if checksum(path) != digest:
                raise ValueError("File changed during extraction. Retry indexing this file.")
            for unit in units:
                unit.update(overview(unit["content"]), summary_kind="extractive_preview")
            record = {
                "id": document_id, "collection": self.collection, "name": path.name, "path": str(path),
                "source": "local", "checksum": digest, "units": units, "unit_count": len(units),
                "summary_kind": "extractive_preview", "analysis_status": "pending", "extractions": {},
                "embedding_status": "disabled" if self.settings["model"] == "none" else "pending",
                "status": "partial", "updated_at": now(), **overview("\n".join(unit["summary"] for unit in units)),
            }
            # Text and keyword chunks become usable before model loading/inference.
            self.store.save_document(record, self.make_chunks(record))
            changed = True
        semantic = not lexical_only and self.settings["model"] != "none"
        if semantic and (changed or record.get("embedding_key") != model_key(self.settings) or record.get("embedding_status") != "ready"):
            try:
                chunks = self.make_chunks(record, semantic=True)
                record.update(embedding_status="ready", embedding_key=model_key(self.settings), updated_at=now())
                record.pop("embedding_error", None)
                record["status"] = "ready" if record["analysis_status"] == "ready" else "partial"
                self.store.save_document(record, chunks)
            except Exception as exc:
                record.update(embedding_status="failed", embedding_error=str(exc), status="partial", updated_at=now())
                self.store.save_document(record)
                raise ValueError(f"Text is indexed, but embeddings failed: {exc}") from exc
        return {**public_document(record), "index_action": "indexed" if changed else "unchanged"}

    def run_job(self, job_id: str):
        import psutil
        job = self.store.job(self.collection, job_id)
        if job["status"] not in {"queued", "interrupted", "failed", "completed_with_errors"}:
            raise ValueError("Only queued, interrupted or failed jobs can run/resume.")
        try:
            with collection_lock(self.root, self.collection):
                job.pop("error", None)
                job.update(status="running", pid=os.getpid(), process_created=psutil.Process().create_time(), updated_at=now(), result={"indexed": [], "errors": []})
                self.store.save_job(job)
                paths, errors = discover(job["paths"], recursive=job["recursive"])
                job["total"] = len(paths)
                job["result"]["errors"].extend(errors)
                for index, path in enumerate(paths):
                    job.update(current_path=str(path), processed=index, updated_at=now())
                    self.store.save_job(job)
                    try:
                        job["result"]["indexed"].append(self.index_file(path, lexical_only=job["lexical_only"]))
                    except Exception as exc:
                        job["result"]["errors"].append({"path": str(path), "error": str(exc)})
                        # An invalid changed file must not leave prior content searchable as current.
                        document_id = "doc_" + hashlib.sha256((self.collection + "\0" + str(path)).encode()).hexdigest()[:32]
                        try:
                            old = self.store.document(self.collection, document_id)
                            if old["checksum"] != checksum(path):
                                old.update(status="failed", error="Source changed and re-extraction failed.", units=[], summary="", tagline="", keywords=[], extractions={}, updated_at=now())
                                self.store.save_document(old, [])
                        except (ValueError, OSError):
                            pass
                    job.update(processed=index + 1, updated_at=now())
                    self.store.save_job(job)
                job.update(status="completed_with_errors" if job["result"]["errors"] else "completed", current_path=None, updated_at=now())
                self.store.save_job(job)
        except BaseException as exc:
            job.update(status="interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "failed", error=str(exc), updated_at=now())
            self.store.save_job(job)
            raise
        return job

    def search(self, query: str, stage: str, ids: list[str] | None, limit: int, semantic: bool):
        if semantic and stage != "chunks":
            raise ValueError("Semantic search applies to --stage chunks; use cheaper catalog stages first.")
        lexical = self.store.lexical(self.collection, query, stage, ids, limit * 3 if semantic else limit)
        if not semantic:
            return lexical[:limit]
        if ids == []:
            return []
        vectors = self.embedder().encode([query], query=True)
        dense = self.store.semantic(self.collection, vectors[0], model_key(self.settings), ids, limit * 3)
        # Reciprocal rank fusion keeps unlike SQL/vector scores from being compared numerically.
        fused = {}
        for source, results in (("keyword", lexical), ("semantic", dense)):
            for rank, row in enumerate(results, 1):
                entry = fused.setdefault(row["chunk_id"], {**row, "fusion_score": 0.0, "matched_by": []})
                entry["fusion_score"] += 1 / (60 + rank)
                entry["matched_by"].append(source)
        return sorted(fused.values(), key=lambda item: item["fusion_score"], reverse=True)[:limit]

    def save_analysis(self, document_id: str, data: dict):
        with collection_lock(self.root, self.collection):
            record = self.store.document(self.collection, document_id)
            if data.get("checksum") != record["checksum"]:
                raise ValueError("Analysis checksum differs from indexed content. Fetch current evidence and retry.")
            for key in ("tagline", "summary"):
                if not isinstance(data.get(key), str) or not data[key].strip():
                    raise ValueError(f"Analysis requires a nonempty {key}.")
            if not isinstance(data.get("keywords", []), list) or any(not isinstance(word, str) for word in data.get("keywords", [])):
                raise ValueError("Analysis keywords must be strings.")
            updates = data.get("units", [])
            if not isinstance(updates, list):
                raise ValueError("Analysis units must be an array.")
            for update in updates:
                unit = next((unit for unit in record["units"] if unit["number"] == update.get("number")), None)
                if not unit or not isinstance(update.get("summary"), str) or not update["summary"].strip() or not isinstance(update.get("tagline"), str) or not update["tagline"].strip():
                    raise ValueError("Every unit update needs a valid number, summary and tagline.")
                unit.update(summary=update["summary"], tagline=update["tagline"], summary_kind="agent_analysis")
            record.update(tagline=data["tagline"], summary=data["summary"], keywords=data.get("keywords", []), summary_kind="agent_analysis", updated_at=now())
            record["analysis_status"] = "ready" if all(unit["summary_kind"] == "agent_analysis" for unit in record["units"]) else "partial"
            record["status"] = "ready" if record["analysis_status"] == "ready" and (self.settings["model"] == "none" or record.get("embedding_key") == model_key(self.settings) and record["embedding_status"] == "ready") else "partial"
            self.store.save_document(record)
        return public_document(record)

    def save_extraction(self, document_id: str, data: dict):
        with collection_lock(self.root, self.collection):
            record = self.store.document(self.collection, document_id)
            if data.get("checksum") != record["checksum"]:
                raise ValueError("Extraction checksum differs from indexed content.")
            if not isinstance(data.get("schema"), str) or not isinstance(data.get("fields"), dict):
                raise ValueError("Extraction requires a schema name and fields object.")
            numbers = data.get("source_units")
            if not isinstance(numbers, list) or not numbers or any(number not in [unit["number"] for unit in record["units"]] for number in numbers):
                raise ValueError("Extraction requires valid source_units from original content.")
            record["extractions"][data["schema"]] = {"fields": data["fields"], "source_units": numbers, "checksum": record["checksum"], "updated_at": now()}
            self.store.save_document(record)
        return {"saved": True, "document_id": document_id, "schema": data["schema"]}
