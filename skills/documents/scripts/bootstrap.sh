#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Snytro — https://snytro.com
# This entry point works without Python, pip, Node, Ollama or a compiler.
set -euo pipefail
umask 077
PACKAGE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -z "${SNYTRO_DATA_DIR:-}" ]]; then
  if [[ "$(uname -s)" == Darwin ]]; then
    export SNYTRO_DATA_DIR="$HOME/Library/Application Support/SnytroCLI"
  else
    export SNYTRO_DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/SnytroCLI"
  fi
fi
mkdir -p "$SNYTRO_DATA_DIR/tools" "$SNYTRO_DATA_DIR/runtime"
SNYTRO_ROOT="$(cd -- "$SNYTRO_DATA_DIR" && pwd)"
export UV_CACHE_DIR="$SNYTRO_ROOT/runtime/uv-cache"
export UV_PYTHON_INSTALL_DIR="$SNYTRO_ROOT/runtime/python"
export UV_PYTHON_INSTALL_BIN=0
UV_VERSION=0.9.8
UV_EXECUTABLE="$SNYTRO_ROOT/tools/uv"
if [[ ! -x "$UV_EXECUTABLE" ]]; then
  if command -v uv >/dev/null 2>&1; then
    UV_EXECUTABLE="$(command -v uv)"
  else
    case "$(uname -s):$(uname -m)" in
      Darwin:arm64) UV_TARGET=aarch64-apple-darwin ;;
      Darwin:x86_64) UV_TARGET=x86_64-apple-darwin ;;
      Linux:x86_64) UV_TARGET=x86_64-unknown-linux-gnu ;;
      Linux:aarch64|Linux:arm64) UV_TARGET=aarch64-unknown-linux-gnu ;;
      *) echo 'Unsupported platform. Use the Windows PowerShell launcher or a supported macOS/Linux machine.' >&2; exit 1 ;;
    esac
    DOWNLOAD_DIR="$(mktemp -d "$SNYTRO_ROOT/tools/.download.XXXXXX")"
    trap 'rm -rf -- "$DOWNLOAD_DIR"' EXIT
    ARCHIVE="uv-$UV_TARGET.tar.gz"
    RELEASE="https://github.com/astral-sh/uv/releases/download/$UV_VERSION"
    curl --fail --location --proto '=https' --tlsv1.2 "$RELEASE/$ARCHIVE" -o "$DOWNLOAD_DIR/$ARCHIVE"
    curl --fail --location --proto '=https' --tlsv1.2 "$RELEASE/$ARCHIVE.sha256" -o "$DOWNLOAD_DIR/checksum"
    if command -v shasum >/dev/null 2>&1; then
      ACTUAL="$(shasum -a 256 "$DOWNLOAD_DIR/$ARCHIVE" | cut -d ' ' -f 1)"
    else
      ACTUAL="$(sha256sum "$DOWNLOAD_DIR/$ARCHIVE" | cut -d ' ' -f 1)"
    fi
    read -r EXPECTED _ < "$DOWNLOAD_DIR/checksum"
    [[ "$ACTUAL" == "$EXPECTED" ]] || { echo 'uv checksum verification failed.' >&2; exit 1; }
    tar -xzf "$DOWNLOAD_DIR/$ARCHIVE" -C "$DOWNLOAD_DIR"
    install -m 700 "$DOWNLOAD_DIR/uv-$UV_TARGET/uv" "$SNYTRO_ROOT/tools/uv"
  fi
fi
EXTRAS=""
for option in "$@"; do
  case "$option" in
    --embeddings) EXTRAS="${EXTRAS:+$EXTRAS,}embeddings" ;;
    --postgres) EXTRAS="${EXTRAS:+$EXTRAS,}postgres" ;;
    *) echo "Unknown bootstrap option: $option" >&2; exit 1 ;;
  esac
done
PYTHON="$SNYTRO_ROOT/runtime/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  "$UV_EXECUTABLE" venv --managed-python --python 3.12 "$SNYTRO_ROOT/runtime/.venv" >&2
fi
# Sync uses the lock's exact artifact hashes AND registry sources (including CPU
# PyTorch). Inexact retains already-installed optional extras during later setup.
export UV_PROJECT_ENVIRONMENT="$SNYTRO_ROOT/runtime/.venv"
SYNC_ARGS=(sync --project "$PACKAGE_DIR/runtime" --frozen --no-dev --no-editable --inexact)
if [[ "$EXTRAS" == *embeddings* ]]; then SYNC_ARGS+=(--extra embeddings); fi
if [[ "$EXTRAS" == *postgres* ]]; then SYNC_ARGS+=(--extra postgres); fi
"$UV_EXECUTABLE" "${SYNC_ARGS[@]}" >&2
echo "Snytro runtime ready at $SNYTRO_ROOT. Run scripts/snytro doctor." >&2
