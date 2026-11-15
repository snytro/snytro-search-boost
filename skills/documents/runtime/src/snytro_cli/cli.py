# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Snytro — https://snytro.com
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import subprocess
import sys
import time
import uuid

from . import __version__
from .embeddings import APIEmbedder, download_model, model_key
from .engine import HIERARCHY_VERSION, Engine, collection_lock, now, public_document
from .profiles import install_profile
from .providers import service_config
from .settings import MODELS, atomic_json, collection_name, data_root, load_settings, machine_report
from .storage import Store


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def bounded_integer(minimum: int, maximum: int):
    def parse(value):
        number = int(value)
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(f"Expected a number from {minimum} to {maximum}.")
        return number
    return parse


def parser():
    result = Parser(description="Snytro hierarchical document indexing and search. Results are JSON.")
    result.add_argument("--version", action="version", version=__version__)
    result.add_argument("--data-dir", help="Private runtime/config/index directory (or SNYTRO_DATA_DIR).")
    result.add_argument("--collection", default="default", type=collection_name)
    commands = result.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="Read-only diagnostics and model fit recommendation.")
    doctor.add_argument("--language", choices=["english", "multilingual"], default="english")
    doctor.add_argument("--priority", choices=["balanced", "accuracy"], default="balanced")
    setup = commands.add_parser("setup")
    setup.add_argument("--storage", choices=["sqlite", "postgres"], default=None)
    setup.add_argument("--model", choices=["auto", "ultrabert", "nemotron", "api", "none"], default=None)
    setup.add_argument("--embedding-provider", choices=["openai", "compatible"])
    setup.add_argument("--embedding-model")
    setup.add_argument("--embedding-base-url")
    setup.add_argument("--embedding-key-env")
    setup.add_argument("--embedding-dimensions", type=bounded_integer(1, 4096))
    setup.add_argument("--embedding-send-dimensions", action=argparse.BooleanOptionalAction, default=None)
    setup.add_argument("--summary-provider", choices=["host", "openai", "compatible"])
    setup.add_argument("--summary-model")
    setup.add_argument("--summary-base-url")
    setup.add_argument("--summary-key-env")
    setup.add_argument("--language", choices=["english", "multilingual"], default="english")
    setup.add_argument("--priority", choices=["balanced", "accuracy"], default="balanced")
    setup.add_argument("--download-model", action="store_true")
    setup.add_argument("--batch-size", type=bounded_integer(1, 16), default=None)
    setup.add_argument("--chunk-tokens", type=bounded_integer(128, 2048), default=None)
    setup.add_argument("--threads", type=bounded_integer(1, 64), default=None)
    setup.add_argument("--pg-bin-dir")
    setup.add_argument("--pg-port", type=bounded_integer(1024, 65535), default=54387)
    setup.add_argument("--install-postgres", action="store_true")
    commands.add_parser("model-download", help="Download/cache the selected model and verify CPU inference.")
    index = commands.add_parser("index")
    index.add_argument("paths", nargs="+")
    index.add_argument("--no-recursive", action="store_true")
    index.add_argument("--lexical-only", action="store_true", help="Extract now; defer embeddings until a later index run.")
    index.add_argument("--full", action="store_true", help="Generate and checkpoint summaries with the selected API.")
    index.add_argument("--rich", action="store_true", help="Use installed OCR/layout tools and retain more detailed source locators.")
    index.add_argument("--wait", action="store_true", help="Run synchronously instead of returning a job ID.")
    for command in ("status", "resume", "_worker"):
        job = commands.add_parser(command)
        job.add_argument("job_id")
    listing = commands.add_parser("list")
    listing.add_argument("--limit", type=bounded_integer(1, 500), default=100)
    listing.add_argument("--offset", type=bounded_integer(0, 10_000_000), default=0)
    listing.add_argument("--document-ids", nargs="*")
    summaries = commands.add_parser("summaries")
    summaries.add_argument("--document-ids", nargs="+", required=True)
    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--stage", choices=["taglines", "summaries", "pages", "chunks"], default="taglines")
    mode = search.add_mutually_exclusive_group()
    mode.add_argument("--semantic", action="store_true", help="Blend semantic and keyword results for the selected stage.")
    mode.add_argument("--dense-only", action="store_true", help="Use authority-adjusted semantic ranking only.")
    mode.add_argument("--exact", action="store_true", help="Find a literal phrase in this stage; use chunks to check complete raw units.")
    search.add_argument("--route")
    search.add_argument("--document-ids", nargs="*")
    search.add_argument("--limit", type=bounded_integer(1, 50), default=12)
    fetch = commands.add_parser("fetch")
    fetch.add_argument("document_id")
    fetch.add_argument("--unit", type=bounded_integer(1, 1_000_000), required=True)
    fetch.add_argument("--offset", type=bounded_integer(0, 100_000_000), default=0)
    fetch.add_argument("--max-chars", type=bounded_integer(1, 50_000), default=12000)
    structured = commands.add_parser("structured")
    structured.add_argument("--document-ids", nargs="+", required=True)
    structured.add_argument("--schema")
    structured.add_argument("--fields", nargs="+")
    for command in (listing, summaries, search, fetch, structured):
        command.add_argument("--include-explicit", action="store_true")
    for name in ("save-analysis", "save-extraction"):
        save = commands.add_parser(name)
        save.add_argument("document_id")
        save.add_argument("--input", required=True, help="JSON file, or - for stdin.")
    remove = commands.add_parser("remove")
    remove.add_argument("document_ids", nargs="+")
    remove.add_argument("--confirm", action="store_true")
    profile = commands.add_parser("profile", help="Show or install a reviewed collection profile.")
    profile.add_argument("--input")
    profile.add_argument("--source-root")
    export = commands.add_parser("export", help="Export only this collection and optional verified source files.")
    export.add_argument("archive")
    export.add_argument("--with-originals", action="store_true")
    restore = commands.add_parser("restore", help="Restore a collection into an empty target collection.")
    restore.add_argument("archive")
    restore.add_argument("--source-root")
    verify = commands.add_parser("verify", help="Check inventory, summary and embedding coverage.")
    verify.add_argument("--check-originals", action="store_true", help="Compare available originals with indexed checksums.")
    return result


def status(store, collection, job_id):
    import psutil
    job = store.job(collection, job_id)
    if job["status"] == "running":
        try:
            process = psutil.Process(job["pid"])
            live = process.create_time() == job["process_created"] and process.status() != psutil.STATUS_ZOMBIE
        except (psutil.Error, KeyError):
            live = False
        if not live:
            job.update(status="interrupted", error="Worker exited before completion. Use resume.", updated_at=now())
            store.save_job(job)
    elif job["status"] == "queued" and time.time() - job["created_epoch"] > 300:
        job.update(status="interrupted", error="Worker did not start. Use resume.", updated_at=now())
        store.save_job(job)
    return {key: value for key, value in job.items() if key != "settings"}


def start_worker(root, collection, job_id):
    log = root / ("job-" + job_id + ".log")
    descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "ab") as handle:
        subprocess.Popen([sys.executable, "-m", "snytro_cli.cli", "--data-dir", str(root), "--collection", collection, "_worker", job_id], stdin=subprocess.DEVNULL, stdout=handle, stderr=handle, start_new_session=True)
    return {"job_id": job_id, "status": "queued", "log_path": str(log)}


def input_json(value):
    from pathlib import Path
    source = sys.stdin.read() if value == "-" else Path(value).read_text(encoding="utf-8")
    data = json.loads(source, parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Nonfinite JSON values are not allowed.")))
    if not isinstance(data, dict):
        raise ValueError("Input must be a JSON object.")
    return data


def readiness(record, settings):
    result = public_document(record)
    if settings["model"] != "none" and record.get("embedding_key") != model_key(settings):
        result.update(embedding_status="pending", status="partial")
    result["hierarchy_embeddings_ready"] = (
        settings["model"] != "none" and record.get("embedding_key") == model_key(settings)
        and record.get("hierarchy_version") == HIERARCHY_VERSION and record.get("analysis_status") == "ready"
    )
    return result


def api_settings(args, old, purpose):
    previous = old.get(purpose + "_api") or {}
    selected = getattr(args, purpose + "_provider")
    if selected == "host":
        return None
    if selected and selected != previous.get("provider"):
        previous = {}
    provider = selected or previous.get("provider")
    attributes = ["model", "base_url", "key_env"]
    if purpose == "embedding":
        attributes += ["dimensions", "send_dimensions"]
    if not provider:
        if any(getattr(args, purpose + "_" + attr) is not None for attr in attributes):
            raise ValueError(f"Select --{purpose}-provider before configuring provider options.")
        return None
    model = getattr(args, purpose + "_model") or previous.get("model")
    if provider == "openai" and not model:
        model = "text-embedding-3-small" if purpose == "embedding" else "gpt-5.4-mini"
    config = service_config(provider, model, getattr(args, purpose + "_base_url") or previous.get("url"),
                            getattr(args, purpose + "_key_env") or previous.get("key_env"))
    if purpose == "embedding":
        dims = args.embedding_dimensions or previous.get("dimensions") or (768 if provider == "openai" else None)
        if not dims:
            raise ValueError("Set --embedding-dimensions to the size of the selected model's output.")
        send = args.embedding_send_dimensions
        if send is None:
            send = previous.get("send_dimensions", provider == "openai")
        config.update(dimensions=dims, send_dimensions=send)
    return config


def execute(args):
    root = data_root(args.data_dir)
    if args.command == "doctor":
        report = machine_report(root, language=args.language, priority=args.priority)
        versions = {}
        for package in ("sqlite-vec", "torch", "transformers", "sentence-transformers", "psycopg"):
            try:
                versions[package] = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                versions[package] = None
        report.update(version=__version__, data_dir=str(root), packages=versions, configured=(root / "settings.json").exists())
        if report["configured"]:
            settings = load_settings(root)
            report.update(storage=settings["storage"], selected_model=settings["model"], model_downloaded=bool(settings.get("model_path")),
                          embedding_api=settings.get("embedding_api"), summary_api=settings.get("summary_api"))
        return report
    if args.command == "setup":
        report = machine_report(root, language=args.language, priority=args.priority)
        old = load_settings(root) if (root / "settings.json").exists() else {}
        storage = args.storage or old.get("storage", "sqlite")
        if old and storage != old["storage"]:
            raise ValueError("Storage changes require a separate --data-dir. Automatic database migration is not implemented.")
        model = args.model or ("api" if args.embedding_provider else old.get("model", "auto"))
        if model == "auto":
            model = report["recommended_model"]
            if model is None:
                raise ValueError("Cannot recommend a model with the current available memory. Free RAM or choose --model explicitly.")
        embedding_api, summary_api = api_settings(args, old, "embedding"), api_settings(args, old, "summary")
        if model == "api" and not embedding_api:
            raise ValueError("Select --embedding-provider openai or compatible for --model api.")
        settings = {"version": 1, "runtime_schema": 2, "storage": storage, "model": model,
                    "batch_size": args.batch_size or old.get("batch_size", 16 if model == "api" else 1),
                    "chunk_tokens": args.chunk_tokens or old.get("chunk_tokens", 512), "threads": args.threads or old.get("threads", 2)}
        if model == "api":
            settings["embedding_api"] = embedding_api
        if summary_api:
            settings["summary_api"] = summary_api
        if model == old.get("model"):
            for key in ("model_revision", "model_path"):
                if old.get(key):
                    settings[key] = old[key]
        from .settings import private_dir
        private_dir(root)
        with collection_lock(root, "_setup"):
            if storage == "postgres" and not os.environ.get("SNYTRO_POSTGRES_URL"):
                from .postgres import prepare_managed
                previous = old.get("managed_postgres", {})
                settings["managed_postgres"] = prepare_managed(root, bin_dir=args.pg_bin_dir or previous.get("bin_dir"), port=previous.get("port", args.pg_port), install=args.install_postgres)
            Store(root, settings).initialize()
            if args.download_model:
                if model == "api":
                    APIEmbedder(settings).encode(["Snytro embedding connection test."])
                else:
                    settings = download_model(root, settings)
            atomic_json(root / "settings.json", settings)
        return {"configured": True, "storage": storage, "model": model, "model_ready": bool(settings.get("model_path")) or model == "api" and args.download_model,
                "embedding_api": settings.get("embedding_api"), "summary_api": summary_api, "data_dir": str(root), "recommendation": report,
                "next": "index" if model in {"none", "api"} or settings.get("model_path") else "model-download"}
    settings = load_settings(root)
    if settings.get("runtime_schema") != 2:
        with collection_lock(root, "_setup"):
            settings = load_settings(root)
            if settings.get("runtime_schema") != 2:
                Store(root, settings).initialize()
                settings["runtime_schema"] = 2
                atomic_json(root / "settings.json", settings)
    engine = Engine(root, settings, args.collection)
    store = engine.store
    if args.command == "model-download":
        if settings["model"] == "api":
            APIEmbedder(settings).encode(["Snytro embedding connection test."])
            return {"model_ready": True, "model": settings["embedding_api"]["model"]}
        with collection_lock(root, "_setup"):
            settings = download_model(root, settings)
            atomic_json(root / "settings.json", settings)
        return {"model_ready": True, "model": settings["model"], "revision": settings["model_revision"]}
    if args.command == "index":
        from pathlib import Path
        # Resolve relative selections before detached workers change directory/context.
        paths = [str(Path(path).expanduser().absolute()) for path in args.paths]
        if args.full and not settings.get("summary_api"):
            raise ValueError("Set a summary API provider before running index --full.")
        job = {"id": str(uuid.uuid4()), "collection": args.collection, "paths": paths, "recursive": not args.no_recursive,
               "full": args.full, "rich": args.rich, "source_profile": engine.profile,
               "lexical_only": args.lexical_only, "status": "queued", "created_epoch": time.time(), "updated_at": now(), "processed": 0, "settings": settings}
        store.save_job(job)
        return engine.run_job(job["id"]) if args.wait else start_worker(root, args.collection, job["id"])
    if args.command == "_worker":
        job = store.job(args.collection, args.job_id)
        worker = Engine(root, job["settings"], args.collection)
        worker.profile = job.get("source_profile", worker.profile)
        return worker.run_job(args.job_id, current_settings=settings)
    if args.command == "status":
        return status(store, args.collection, args.job_id)
    if args.command == "resume":
        job = status(store, args.collection, args.job_id)
        if job["status"] not in {"failed", "interrupted", "completed_with_errors"}:
            raise ValueError("Only failed or interrupted jobs can be resumed.")
        return start_worker(root, args.collection, args.job_id)
    if args.command in {"list", "summaries"}:
        ids, _ = engine.scope(args.document_ids, args.include_explicit)
        records = store.documents(args.collection, ids, getattr(args, "limit", 500), getattr(args, "offset", 0))
        return {"documents": [readiness(record, settings) for record in records], "offset": getattr(args, "offset", 0), "count": len(records)}
    if args.command == "search":
        results = engine.search(args.query, args.stage, args.document_ids, args.limit, args.semantic or args.dense_only,
                                exact=args.exact, include_explicit=args.include_explicit, route=args.route, dense_only=args.dense_only)
        ids = list(dict.fromkeys(row["document_id"] for row in results))
        return {"stage": args.stage, "mode": "exact" if args.exact else "semantic" if args.dense_only else "hybrid" if args.semantic else "keyword", "results": results,
                "readiness": [readiness(row, settings) for row in store.documents(args.collection, ids, 500)], "count": len(results)}
    if args.command == "fetch":
        ids, _ = engine.scope([args.document_id], args.include_explicit)
        if not ids:
            raise ValueError("Document is not eligible under this collection profile.")
        record = store.document(args.collection, args.document_id)
        unit = next((unit for unit in record["units"] if unit["number"] == args.unit), None)
        if not unit:
            raise ValueError("Unit is not present in this document.")
        end = min(args.offset + args.max_chars, len(unit["content"]))
        return {"document_id": record["id"], "source_file_name": record["name"], "path": record["path"], "checksum": record["checksum"],
                "unit": args.unit, "locator": unit["locator"], "content": unit["content"][args.offset:end], "offset": args.offset,
                "next_offset": end if end < len(unit["content"]) else None, "evidence_level": "original"}
    if args.command in {"save-analysis", "save-extraction"}:
        data = input_json(args.input)
        return engine.save_analysis(args.document_id, data) if args.command == "save-analysis" else engine.save_extraction(args.document_id, data)
    if args.command == "structured":
        results = []
        ids, _ = engine.scope(args.document_ids, args.include_explicit)
        for record in store.documents(args.collection, ids, 500):
            for schema, extraction in record["extractions"].items():
                if args.schema and args.schema != schema:
                    continue
                fields = extraction["fields"]
                if args.fields:
                    selected = {}
                    for path in args.fields:
                        value = fields
                        for key in path.split("."):
                            value = value.get(key) if isinstance(value, dict) else None
                        selected[path] = value
                    fields = selected
                results.append({"document_id": record["id"], "source_file_name": record["name"], "path": record["path"], "schema": schema, **extraction, "fields": fields, "evidence_level": "agent_extraction"})
        return {"results": results, "count": len(results)}
    if args.command == "remove":
        if not args.confirm:
            raise ValueError("Removal requires --confirm and explicit user authorization. Original files are never deleted.")
        with collection_lock(root, args.collection):
            existing = store.documents(args.collection, args.document_ids, 500)
            store.remove(args.collection, args.document_ids)
        return {"removed_count": len(existing)}
    if args.command == "profile":
        if args.input is None:
            return {"profile": engine.profile}
        if not args.source_root:
            raise ValueError("Set --source-root when installing a profile.")
        with collection_lock(root, args.collection):
            return {"profile": install_profile(root, args.collection, input_json(args.input), args.source_root)}
    if args.command == "verify":
        from pathlib import Path
        from .extract import checksum
        ids, _ = engine.scope(None, True)
        paths = store.catalog_paths(args.collection, ids)
        expected = {str(Path(engine.profile["root"]) / path) for path, policy in engine.profile["policy"]["sources"].items()
                    if policy["visibility"] != "excluded"} if engine.profile else set()
        missing = sorted(expected - {row["path"] for row in paths})
        coverage = {"documents": len(paths), "units": 0, "summaries_ready": 0, "hierarchy_embeddings_ready": 0, "vector_records": 0}
        issues = [{"path": path, "issue": "not indexed"} for path in missing]
        for row in paths:
            record = store.document(args.collection, row["id"])
            state = readiness(record, settings)
            coverage["units"] += len(record["units"])
            coverage["summaries_ready"] += int(state.get("analysis_status") == "ready")
            coverage["hierarchy_embeddings_ready"] += int(state["hierarchy_embeddings_ready"])
            coverage["vector_records"] += store.vector_count(args.collection, row["id"])
            if record.get("status") == "failed":
                issues.append({"path": row["path"], "issue": "indexing failed"})
            elif not record["units"]:
                issues.append({"path": row["path"], "issue": "no source units"})
            if args.check_originals:
                path = Path(row["path"])
                try:
                    original_issue = "changed original" if checksum(path) != record["checksum"] else None
                except OSError:
                    original_issue = "missing or unreadable original"
                if original_issue:
                    issues.append({"path": row["path"], "issue": original_issue})
        ready = (bool(paths) and not issues and coverage["summaries_ready"] == len(paths)
                 and (settings["model"] == "none" or coverage["hierarchy_embeddings_ready"] == len(paths)))
        return {"coverage": coverage, "complete": ready, "issues": issues}
    if args.command in {"export", "restore"}:
        from .handover import export_collection, restore_collection
        with collection_lock(root, args.collection):
            if args.command == "export":
                return export_collection(engine, args.archive, args.with_originals)
            return restore_collection(engine, args.archive, args.source_root)
    raise ValueError("Unknown command.")


def main():
    try:
        args = parser().parse_args()
        value = execute(args)
        print(json.dumps({"ok": True, "collection": args.collection, **value}, ensure_ascii=False, allow_nan=False))
        return 1 if value.get("status") in {"failed", "interrupted", "completed_with_errors"} else 0
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "error": "Interrupted; check job status before retrying."}))
        return 130
    except Exception as exc:
        # Never return DSNs/passwords or dependency tracebacks in agent-visible JSON.
        error = str(exc) if isinstance(exc, (ValueError, ImportError, FileNotFoundError)) else f"{type(exc).__name__}: operation failed."
        secret = os.environ.get("SNYTRO_POSTGRES_URL")
        if secret:
            error = error.replace(secret, "[redacted database URL]")
        print(json.dumps({"ok": False, "error": error}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
