"""Minimal ``.env`` loader.

Loads ``KEY=VALUE`` pairs from a dotenv file into ``os.environ`` without
overwriting already-set variables, so real secrets live outside the repo. The
same parser backs the M2 systemd ``EnvironmentFile`` validation, so the keys the
control plane checks match the keys a scheduled service would actually see.
"""

from __future__ import annotations

import os
from pathlib import Path


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse ``KEY=VALUE`` pairs from a dotenv/EnvironmentFile into a dict.

    Blank lines, comments, and lines without ``=`` are ignored; a single layer
    of matching surrounding quotes is stripped from each value. Returns an empty
    dict when the file does not exist.
    """
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key:
            result[key] = _clean_value(value.strip())
    return result


def load_dotenv(path: Path = Path(".env")) -> None:
    """Load ``KEY=VALUE`` pairs from ``path`` into ``os.environ`` if unset."""
    for key, value in parse_env_file(path).items():
        if key not in os.environ:
            os.environ[key] = value


def _clean_value(value: str) -> str:
    """Strip a single layer of matching surrounding quotes from a value."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value

