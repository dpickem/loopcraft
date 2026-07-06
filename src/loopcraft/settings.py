"""Public/private configuration split — the prevailing loopcraft pattern.

A committed *public* config file holds only non-confidential placeholders. Real
values are *private* and are resolved at run time, highest precedence first:

1. an environment variable (from ``.env``, which is gitignored),
2. a gitignored ``*.local.*`` sibling file,
3. the public committed file.

The control plane resolves the effective value and materializes it into the
isolated run worktree, so confidential data never has to live in the source repo.
"""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath

#: Prefix for environment variables that override a skill asset file.
ENV_PREFIX = "LOOPCRAFT"


def clean_lines(text: str) -> list[str]:
    """Return non-empty, non-comment lines (stripped) from a list file."""
    lines: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(stripped)
    return lines


def split_env_list(value: str) -> list[str]:
    """Split an env value into entries on commas and/or newlines."""
    items: list[str] = []
    for chunk in value.replace("\n", ",").split(","):
        stripped = chunk.strip()
        if not stripped or stripped.startswith("#"):
            continue
        items.append(stripped)
    return items


def asset_env_var(rel_path: str) -> str:
    """Derive the override env var name for a skill asset path.

    ``skills/slack-triage/channels.txt`` -> ``LOOPCRAFT_SLACK_TRIAGE_CHANNELS``.
    The leading ``skills/`` segment and the file extension are dropped, and every
    run of non-alphanumeric characters becomes a single underscore.
    """
    parts = list(PurePosixPath(rel_path).parts)
    if parts and parts[0] == "skills":
        parts = parts[1:]
    joined = "/".join(parts)
    suffix = PurePosixPath(rel_path).suffix
    if suffix:
        joined = joined[: -len(suffix)]
    token = re.sub(r"[^0-9A-Za-z]+", "_", joined).strip("_").upper()
    return f"{ENV_PREFIX}_{token}"


def local_sibling_path(path: Path) -> Path:
    """Return the ``*.local.*`` sibling path of ``path``, existing or not.

    ``config/x_intel.yaml`` -> ``config/x_intel.local.yaml``. Callers that need
    the effective file should prefer :func:`local_override_path`; this helper is
    for code that must inspect or stage the private override itself.
    """
    return path.with_name(f"{path.stem}.local{path.suffix}")


def local_override_path(path: Path) -> Path:
    """Return the gitignored ``*.local.*`` sibling of ``path`` if it exists.

    Implements the public/private split for whole config files: given
    ``config/x_intel.yaml`` it returns ``config/x_intel.local.yaml`` when that
    private override is present, otherwise the original public ``path``.

    Args:
        path: The public (committed) config file path.

    Returns:
        The private override path when it exists, else ``path`` unchanged.
    """
    local = local_sibling_path(path)
    return local if local.exists() else path


def resolve_overridable_list(
    *,
    env_var: str,
    public_path: Path | None = None,
    local_path: Path | None = None,
    environ: dict[str, str] | None = None,
) -> list[str]:
    """Resolve a list setting using the public/private precedence.

    env var > ``*.local.*`` file > public file. Returns an empty list when none
    of the sources yield entries.
    """
    env = os.environ if environ is None else environ
    value = env.get(env_var)
    if value and value.strip():
        return split_env_list(value)
    if local_path is not None and local_path.exists():
        return clean_lines(local_path.read_text(encoding="utf-8"))
    if public_path is not None and public_path.exists():
        return clean_lines(public_path.read_text(encoding="utf-8"))
    return []
