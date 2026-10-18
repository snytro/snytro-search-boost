# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Snytro — https://snytro.com
"""Optional private Postgres cluster; never starts/stops the desktop app cluster."""
from __future__ import annotations

import os
import secrets
import shutil
import socket
import subprocess
import platform
from pathlib import Path

from .settings import atomic_json, private_dir


def managed_dsn(root: Path, managed: dict, database: str = "snytro") -> str:
    import json
    from psycopg.conninfo import make_conninfo
    password = json.loads((root / "postgres-auth.json").read_text())["password"]
    return make_conninfo(host="127.0.0.1", port=managed["port"], dbname=database, user="snytro", password=password)


def run(arguments: list[str]) -> subprocess.CompletedProcess:
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=120)
    if result.returncode:
        # Authentication/server logs may contain sensitive connection details.
        raise ValueError(f"Postgres command failed: {Path(arguments[0]).name}. Inspect the private postgres.log.")
    return result


def start_managed(root: Path, managed: dict):
    bin_dir = Path(managed["bin_dir"])
    cluster = root / "postgres"
    check = subprocess.run([str(bin_dir / "pg_ctl"), "-D", str(cluster), "status"], capture_output=True, timeout=15)
    if check.returncode == 0:
        return
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", managed["port"]))
        except OSError:
            raise ValueError("Snytro's Postgres port is occupied. Choose another --pg-port; no process was stopped.") from None
    run([str(bin_dir / "pg_ctl"), "-D", str(cluster), "-l", str(root / "postgres.log"), "-o", f"-h 127.0.0.1 -p {managed['port']}", "-w", "start"])


def prepare_managed(root: Path, *, bin_dir: str | None, port: int, install: bool = False) -> dict:
    import psycopg
    if not bin_dir and install:
        brew = shutil.which("brew")
        if platform.system() != "Darwin" or not brew:
            raise ValueError("Automatic Postgres installation currently requires macOS with Homebrew. Use --pg-bin-dir or SNYTRO_POSTGRES_URL on other platforms.")
        subprocess.run([brew, "install", "postgresql@17", "pgvector"], check=True)
    if not bin_dir:
        brew = shutil.which("brew")
        if brew and platform.system() == "Darwin":
            result = subprocess.run([brew, "--prefix", "postgresql@17"], capture_output=True, text=True)
            if result.returncode == 0:
                bin_dir = str(Path(result.stdout.strip()) / "bin")
        if not bin_dir and shutil.which("pg_ctl"):
            bin_dir = str(Path(shutil.which("pg_ctl")).parent)
    if not bin_dir or not all((Path(bin_dir) / executable).is_file() for executable in ("pg_ctl", "initdb")):
        raise ValueError("Postgres binaries are missing. Choose SQLite, supply --pg-bin-dir, or use --install-postgres on macOS/Homebrew.")
    managed = {"bin_dir": str(Path(bin_dir).resolve()), "port": port}
    private_dir(root)
    cluster = root / "postgres"
    if not (cluster / "PG_VERSION").exists():
        auth_path = root / "postgres-auth.json"
        if not auth_path.exists():
            atomic_json(auth_path, {"password": secrets.token_urlsafe(32)})
        import json
        password = json.loads(auth_path.read_text())["password"]
        password_file = root / ".initdb-password"
        descriptor = os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w") as handle:
                handle.write(password + "\n")
            run([str(Path(managed["bin_dir"]) / "initdb"), "-D", str(cluster), "-U", "snytro", "--pwfile", str(password_file), "--auth=scram-sha-256", "--encoding=UTF8", "--no-locale"])
        finally:
            password_file.unlink(missing_ok=True)
    start_managed(root, managed)
    try:
        with psycopg.connect(managed_dsn(root, managed, "postgres"), autocommit=True) as connection:
            if not connection.execute("SELECT 1 FROM pg_database WHERE datname='snytro'").fetchone():
                connection.execute("CREATE DATABASE snytro")
        with psycopg.connect(managed_dsn(root, managed)) as connection:
            connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
    except psycopg.Error:
        raise ValueError("Private Postgres could not initialize pgvector. Check the extension matches these server binaries.") from None
    return managed
