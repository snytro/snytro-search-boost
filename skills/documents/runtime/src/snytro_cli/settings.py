# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Snytro — https://snytro.com
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from platformdirs import user_data_path


MODELS = {
    "bge-small": {
        "id": "qdrant/bge-small-en-v1.5-onnx-q", "inference_name": "BAAI/bge-small-en-v1.5",
        "dimensions": 384, "download_gb": 0.067, "language": "English", "weight_ram_gb_fp32": 0.067,
    },
    "ultrabert": {
        "id": "dmis-lab/UltraBERT-Embed-150M", "dimensions": 768,
        "download_gb": 0.61, "language": "English", "weight_ram_gb_fp32": 0.61,
    },
    "nemotron": {
        "id": "nvidia/Nemotron-3-Embed-1B-BF16", "dimensions": 2048,
        "download_gb": 2.3, "language": "multilingual", "weight_ram_gb_fp32": 4.6,
    },
}


def data_root(value: str | None = None) -> Path:
    return Path(value or os.environ.get("SNYTRO_DATA_DIR") or user_data_path("SnytroCLI", "Snytro")).expanduser().resolve()


def private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        path.chmod(0o700)


def atomic_json(path: Path, value: dict) -> None:
    private_dir(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=".snytro-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, allow_nan=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_settings(root: Path) -> dict:
    path = root / "settings.json"
    if not path.is_file():
        raise ValueError("Snytro is not configured. Run setup first.")
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("version") != 1 or value.get("storage") not in {"sqlite", "postgres"}:
        raise ValueError("Unsupported settings version or storage backend.")
    return value


def collection_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value):
        raise ValueError("Collection names must be 1–64 letters, digits, underscores, dots or hyphens.")
    return value


def machine_report(root: Path, *, language: str = "english", priority: str = "balanced") -> dict:
    import psutil
    memory = psutil.virtual_memory()
    disk_path = root
    while not disk_path.exists():
        disk_path = disk_path.parent
    available = memory.available / 1e9
    total = memory.total / 1e9
    # These are fit heuristics, not claims about measured accuracy or throughput.
    intel_mac = platform.system() == "Darwin" and platform.machine() == "x86_64"
    recommend = "nemotron" if available >= 8 and (language != "english" or priority == "accuracy") else "ultrabert"
    warnings = []
    if intel_mac:
        if language == "english":
            recommend = "bge-small"
        else:
            recommend = None
            warnings.append("Intel Mac bootstrap supports English BGE-small only; multilingual embeddings are unavailable. Use --model none for keyword search or use a machine with Nemotron support.")
    cpu = platform.processor() or platform.machine()
    features = []
    if platform.system() == "Darwin":
        result = subprocess.run(["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True)
        if result.returncode == 0:
            cpu = result.stdout.strip()
        for key in ("machdep.cpu.features", "machdep.cpu.leaf7_features"):
            result = subprocess.run(["/usr/sbin/sysctl", "-n", key], capture_output=True, text=True)
            if result.returncode == 0:
                features.extend(result.stdout.lower().split())
    elif platform.system() == "Linux" and Path("/proc/cpuinfo").exists():
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            key, _, value = line.partition(":")
            if key.strip() == "model name":
                cpu = value.strip()
            elif key.strip() in {"flags", "Features"}:
                features = value.lower().split()
                break
    if language != "english" and recommend in {"ultrabert", "bge-small"}:
        recommend = "nemotron" if available >= 6 else None
        warnings.append("The English-only models are unsuitable for multilingual documents.")
    if available < 3:
        recommend = None
        warnings.append("Low available RAM. Close applications before enabling local embeddings.")
    return {
        "system": platform.system(), "architecture": platform.machine(),
        "cpu": cpu, "cpu_features": sorted(set(features)), "logical_cpus": os.cpu_count(),
        "total_ram_gb": round(total, 2), "available_ram_gb": round(available, 2),
        "free_disk_gb": round(shutil.disk_usage(disk_path).free / 1e9, 2),
        "recommended_model": recommend, "language": language, "priority": priority,
        "reason": "Based on platform, available memory, and language/priority; validate latency on this computer.",
        "models": MODELS, "warnings": warnings,
    }
