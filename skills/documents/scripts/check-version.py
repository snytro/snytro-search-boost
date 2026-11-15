# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Snytro — https://snytro.com
import sys
import tomllib
from pathlib import Path

import snytro_cli

manifest = Path(__file__).resolve().parents[1] / "runtime" / "pyproject.toml"
sys.exit(tomllib.loads(manifest.read_text(encoding="utf-8"))["project"]["version"] != snytro_cli.__version__)
