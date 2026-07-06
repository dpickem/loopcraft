"""Small JSONL helpers for ledger-backed loop state.

Several loops keep append-mostly or deduplicated raw records in JSONL files under
the memory ledger. Keeping the parsing and rewriting helpers here avoids each
store implementing subtly different newline/empty-line/error behavior.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read JSON objects from a JSONL file.

    Args:
        path: JSONL file path.

    Returns:
        Parsed JSON object records. Missing files return an empty list.
    """
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    """Write JSON object records to a JSONL file.

    Args:
        path: Destination file path.
        records: JSON-serializable object records.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
