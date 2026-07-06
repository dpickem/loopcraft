"""Minimal ``.env`` loader.

Loads ``KEY=VALUE`` pairs from a dotenv file into ``os.environ`` without
overwriting already-set variables, so real secrets live outside the repo.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: Path = Path(".env")) -> None:
    """Load ``KEY=VALUE`` pairs from ``path`` into ``os.environ`` if unset."""
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = _clean_value(value.strip())
        if key and key not in os.environ:
            os.environ[key] = value


def _clean_value(value: str) -> str:
    """Strip a single layer of matching surrounding quotes from a value."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value

