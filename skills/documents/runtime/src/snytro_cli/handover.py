# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Snytro — https://snytro.com
"""Checksummed, collection-scoped transfer between SQLite and PostgreSQL."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
from zipfile import ZIP_DEFLATED, ZipFile

from .embeddings import model_key
from .engine import source_id
from .extract import checksum
from .profiles import checked_profile, install_profile, profile_path
from .storage import dumps

MAX_BYTES = 256 * 1024 * 1024


def pack(value):
    return dumps(value).encode("utf-8")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_entry(bundle, entry, sha=None):
    if bundle.getinfo(entry).file_size > MAX_BYTES:
        raise ValueError("An archive entry exceeds the supported size.")
    raw = bundle.read(entry)
    if sha is not None and digest(raw) != sha:
        raise ValueError("Archive checksum verification failed.")
    return raw


def export_collection(engine, archive, with_originals):
    ids, _ = engine.scope(None, True)
    rows = engine.store.catalog_paths(engine.collection, ids)
    if not rows:
        raise ValueError("No eligible documents to export.")
    base = Path(engine.profile["root"]) if engine.profile else Path(os.path.commonpath([str(Path(row["path"]).parent) for row in rows]))
    archive = Path(archive).expanduser()
    manifest = {"version": 1, "source_collection": engine.collection, "profile": engine.profile["policy"] if engine.profile else None, "documents": []}
    # Exclusive creation and private permissions; never overwrite an existing export.
    descriptor = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle, ZipFile(handle, "w", ZIP_DEFLATED) as bundle:
            for position, row in enumerate(rows):
                record = engine.store.document(engine.collection, row["id"])
                path = Path(record.pop("path"))
                relative = path.relative_to(base).as_posix()
                asset = f"records/{position}.json"
                payload = pack({"record": record, "chunks": engine.store.export_chunks(engine.collection, row["id"])})
                if len(payload) > MAX_BYTES:
                    raise ValueError("A document snapshot exceeds the maximum portable entry size.")
                bundle.writestr(asset, payload)
                metadata = {"path": relative, "sha256": digest(payload), "original": False}
                if with_originals:
                    if not path.is_file() or path.stat().st_size > MAX_BYTES:
                        raise ValueError("An original is missing or too large; reindex or export snapshots without originals.")
                    original = path.read_bytes()
                    if digest(original) != record["checksum"]:
                        raise ValueError("An original differs from its indexed checksum. Reindex or omit --with-originals.")
                    bundle.writestr(f"originals/{position}", original)
                    metadata["original"] = True
                manifest["documents"].append(metadata)
            bundle.writestr("manifest.json", pack(manifest))
    except BaseException:
        archive.unlink(missing_ok=True)
        raise
    return {"archive": str(archive.resolve()), "documents": len(rows), "sha256": checksum(archive)}


def restore_collection(engine, archive, source_root):
    if engine.store.catalog_paths(engine.collection) or engine.profile:
        raise ValueError("Restore requires an empty collection with no profile. Choose a new collection.")
    base = Path(source_root).expanduser().resolve() if source_root else (engine.root.resolve() / "restored" / engine.collection)
    created = []
    profile_installed = False
    current = None if engine.settings["model"] == "none" else model_key(engine.settings)
    compatible = 0
    try:
        with ZipFile(Path(archive).expanduser()) as bundle:
            manifest = json.loads(read_entry(bundle, "manifest.json"))
            if manifest.get("version") != 1 or not isinstance(manifest.get("documents"), list) or not manifest["documents"]:
                raise ValueError("Unsupported or empty collection archive.")
            policy = checked_profile(manifest["profile"]) if manifest.get("profile") is not None else None
            names = set()
            with engine.store.connection() as connection:
                for position, entry in enumerate(manifest["documents"]):
                    relative = PurePosixPath(entry["path"])
                    if (not relative.parts or relative.is_absolute() or ".." in relative.parts
                            or "\\" in entry["path"] or relative.as_posix() != entry["path"] or relative in names):
                        raise ValueError("Invalid or duplicate archive source path.")
                    names.add(relative)
                    path = base.joinpath(*relative.parts)
                    if not path.is_relative_to(base) or path.resolve() != path:
                        raise ValueError("Restore source paths cannot traverse symbolic links.")
                    payload = json.loads(read_entry(bundle, f"records/{position}.json", entry["sha256"]))
                    record, chunks = payload["record"], payload["chunks"]
                    previous_id = record["id"]
                    new_id = source_id(engine.collection, path)
                    record.update(id=new_id, collection=engine.collection, path=str(path), name=path.name)
                    if entry["original"]:
                        original = read_entry(bundle, f"originals/{position}", record["checksum"])
                        if path.exists():
                            if not path.is_file() or checksum(path) != record["checksum"]:
                                raise ValueError("Restore would overwrite a different original file.")
                        else:
                            path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                            created.append(path)
                            with os.fdopen(fd, "wb") as handle:
                                handle.write(original)
                    for chunk in chunks:
                        if not chunk["id"].startswith(previous_id + ":") or chunk["stage"] not in {"taglines", "summaries", "pages", "chunks"}:
                            raise ValueError("Invalid archived index record.")
                        chunk["id"] = new_id + chunk["id"][len(previous_id):]
                    engine.store.save_document(record, chunks, connection=connection)
                    compatible += int(current is not None and current == record.get("embedding_key"))
                if policy is not None:
                    base.mkdir(parents=True, mode=0o700, exist_ok=True)
                    install_profile(engine.root, engine.collection, policy, base)
                    profile_installed = True
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        if profile_installed:
            profile_path(engine.root, engine.collection).unlink(missing_ok=True)
        raise
    return {"documents": len(names), "source_root": str(base), "matching_embedding_documents": compatible}
