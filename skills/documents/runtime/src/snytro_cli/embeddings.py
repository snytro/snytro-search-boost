# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Snytro — https://snytro.com
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path

from .settings import MODELS


def model_key(settings: dict) -> str:
    return hashlib.sha256(json.dumps({
        "model": settings["model"], "revision": settings.get("model_revision"),
        "processing": "snytro-bge-small-onnx-normalized-v2" if settings["model"] == "bge-small" else "snytro-cpu-fp32-normalized-prompts-v1",
        "chunk_tokens": settings["chunk_tokens"],
    }, sort_keys=True).encode()).hexdigest()


def validate_vector(value, dimensions: int) -> list[float]:
    result = [float(x) for x in value]
    if len(result) != dimensions or not all(math.isfinite(x) for x in result):
        raise ValueError("Embedding dimensions or numeric values do not match the collection model.")
    norm = math.sqrt(sum(x * x for x in result))
    if not norm:
        raise ValueError("Embedding model returned a zero vector.")
    return [x / norm for x in result]


class Embedder:
    def __init__(self, root: Path, settings: dict):
        if settings["model"] == "none":
            raise ValueError("Semantic search is disabled. Select an embedding model in setup.")
        if not settings.get("model_path") or not settings.get("model_revision"):
            raise ValueError("Model is not downloaded. Run model-download first.")
        self.settings = settings
        if settings["model"] == "bge-small":
            if settings["chunk_tokens"] > 512:
                raise ValueError("BGE-small supports at most 512 tokens per chunk.")
            from fastembed import TextEmbedding
            from tokenizers import Tokenizer
            self.model = TextEmbedding(
                model_name=MODELS["bge-small"]["inference_name"],
                specific_model_path=settings["model_path"], local_files_only=True,
                threads=max(1, min(settings.get("threads", 2), os.cpu_count() or 1)),
                providers=["CPUExecutionProvider"],
            )
            if self.model.embedding_size != 384:
                raise ValueError("Downloaded model has an unexpected embedding dimension.")
            # FastEmbed's inference tokenizer truncates to its context window.
            # Chunk the entire extraction with a separate, unrestricted tokenizer.
            self.chunk_tokenizer = Tokenizer.from_file(str(Path(settings["model_path"]) / "tokenizer.json"))
            self.chunk_tokenizer.no_truncation()
            self.chunk_tokenizer.no_padding()
            return
        # Never execute downloaded model code implicitly. Both integrations must use native support.
        import torch
        from sentence_transformers import SentenceTransformer
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        torch.set_num_threads(max(1, min(settings.get("threads", 2), os.cpu_count() or 1)))
        self.model = SentenceTransformer(
            settings["model_path"], device="cpu", local_files_only=True, trust_remote_code=False,
            model_kwargs={"dtype": torch.float32, "attn_implementation": "sdpa"},
        )
        self.model.max_seq_length = settings["chunk_tokens"]
        if self.model.get_embedding_dimension() != MODELS[settings["model"]]["dimensions"]:
            raise ValueError("Downloaded model has an unexpected embedding dimension.")

    def chunks(self, text: str) -> list[str]:
        tokenizer = self.chunk_tokenizer if self.settings["model"] == "bge-small" else self.model.tokenizer
        encoded = tokenizer.encode(text, add_special_tokens=False)
        ids = encoded.ids if self.settings["model"] == "bge-small" else encoded
        size = self.settings["chunk_tokens"] - 32
        overlap = min(64, size // 4)
        pieces = []
        for start in range(0, len(ids), size - overlap):
            piece = tokenizer.decode(ids[start:start + size], skip_special_tokens=True)
            if piece.strip():
                pieces.append(piece)
            if start + size >= len(ids):
                break
        return pieces

    def encode(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        if self.settings["model"] == "bge-small":
            output = self.model.query_embed(texts) if query else self.model.embed(texts, batch_size=self.settings["batch_size"])
            return [validate_vector(row, 384) for row in output]
        arguments = {"batch_size": self.settings["batch_size"], "normalize_embeddings": True, "show_progress_bar": False}
        if self.settings["model"] == "ultrabert":
            arguments["prompt"] = "Instruct: Retrieve relevant passages that answer the question.\nQuery: " if query else ""
        else:
            arguments["prompt"] = "query: " if query else "passage: "
        output = self.model.encode(texts, **arguments)
        return [validate_vector(row, MODELS[self.settings["model"]]["dimensions"]) for row in output]


def download_model(root: Path, settings: dict) -> dict:
    from huggingface_hub import HfApi, snapshot_download
    name = settings["model"]
    if name == "none":
        raise ValueError("Choose an embedding model before downloading it.")
    # Resolve once, save immutable commit, and reuse it on all subsequent CLI calls.
    revision = settings.get("model_revision") or HfApi().model_info(MODELS[name]["id"]).sha
    if not revision:
        raise ValueError("Could not resolve an immutable model revision.")
    patterns = ["config.json", "model_optimized.onnx", "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json"] if name == "bge-small" else None
    path = snapshot_download(MODELS[name]["id"], revision=revision, cache_dir=str(root / "models"), allow_patterns=patterns)
    candidate = {**settings, "model_revision": revision, "model_path": path}
    # A real CPU query/document inference is required before reporting the model ready.
    engine = Embedder(root, candidate)
    engine.encode(["A short Snytro document indexing test."])
    engine.encode(["What is this document about?"], query=True)
    return candidate
