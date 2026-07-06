"""Runtime-neutral capability probes and declared-dependency checks.

Any runner (not just Codex) can reuse these helpers to validate a loop's
declared dependencies before execution: the skill/verify/content-config assets,
tools on PATH, required environment variables, auth bundles, and APIs. The probe registries are
module-level dicts so tests and future runtimes can substitute individual probes
without touching a specific runner.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import yaml

from loopcraft.config import LoopcraftConfig, SourcePathError
from loopcraft.manifest import LoopManifest
from loopcraft.paths import assert_under
from loopcraft.probes import run_probe
from loopcraft.settings import local_override_path, local_sibling_path

#: Tool collections map to a CLI binary that must be on PATH for preflight.
TOOL_BINARIES: dict[str, str] = {"nv-tools": "nv-tools"}

#: Seconds allowed for a bounded capability probe (a single targeted API read).
_PROBE_TIMEOUT_S = 30


def probe_nv_tools_auth(config: LoopcraftConfig) -> str | None:
    """Check the nv-tools connector is installed.

    The auth *bundle* is verified by presence of the CLI; the live credential
    check happens per declared API (e.g. the Slack probe below), so we avoid the
    slow, all-services ``nv-tools health`` that fails on unrelated services.

    Args:
        config: Resolved control-plane config (unused; kept for probe symmetry).

    Returns:
        A problem string if nv-tools is missing, else None.
    """
    if config.which("nv-tools") is None:
        return "auth bundle 'nv-tools': nv-tools CLI not found on PATH"
    return None


def probe_slack_api(config: LoopcraftConfig) -> str | None:
    """Verify Slack access with a bounded, read-only, single-service probe.

    Uses ``nv-tools slack list-channels --limit 1`` rather than ``nv-tools
    health`` so the check is fast and scoped to the one service the loop needs,
    instead of failing when some other nv-tools service is unhealthy.

    Args:
        config: Resolved control-plane config (unused; kept for probe symmetry).

    Returns:
        A problem string if the Slack read probe fails, else None.
    """
    if config.which("nv-tools") is None:
        return "api 'slack': requires the nv-tools connector on PATH"
    rc = run_probe(
        ["nv-tools", "slack", "list-channels", "--limit", "1", "--format", "json"],
        timeout_s=_PROBE_TIMEOUT_S,
    )
    if rc is None:
        return "api 'slack': could not run the Slack read probe (nv-tools slack list-channels)"
    if rc != 0:
        return (
            "api 'slack': Slack read probe failed — check `nv-tools slack list-channels` "
            "(auth/config)"
        )
    return None


def probe_x_api_auth(config: LoopcraftConfig) -> str | None:
    """Verify X API credentials are present for X intelligence loops.

    Args:
        config: Resolved control-plane config (for centralized env access).

    Returns:
        A problem string if no X token is configured, else None.
    """
    if not config.env_value("X_API_BEARER_TOKEN") and not config.env_value("X_API_OAUTH2_ACCESS_TOKEN"):
        return "auth bundle 'x-api': X_API_BEARER_TOKEN or X_API_OAUTH2_ACCESS_TOKEN is required"
    return None


def probe_x_api(config: LoopcraftConfig) -> str | None:
    """No-op API probe: X access is validated by the x-api auth/env checks.

    Args:
        config: Resolved control-plane config (unused; kept for probe symmetry).

    Returns:
        Always None (no live preflight call is made).
    """
    return None


def probe_arxiv_api(config: LoopcraftConfig) -> str | None:
    """No-op API probe: arXiv is public/no-auth and handled at run time.

    Args:
        config: Resolved control-plane config (unused; kept for probe symmetry).

    Returns:
        Always None.
    """
    return None


#: Auth-bundle probes: bundle name -> callable returning a problem string or None.
#: Injectable so tests (and future runners) can substitute probes.
AUTH_PROBES: dict[str, Callable[[LoopcraftConfig], str | None]] = {
    "nv-tools": probe_nv_tools_auth,
    "x-api": probe_x_api_auth,
}

#: Declared-API probes: api name -> callable returning a problem string or None.
API_PROBES: dict[str, Callable[[LoopcraftConfig], str | None]] = {
    "slack": probe_slack_api,
    "x": probe_x_api,
    "arxiv": probe_arxiv_api,
}

#: Human guidance for satisfying each known auth bundle, shown by ``loopctl
#: auth`` when a bundle is missing so the operator knows the exact next step.
AUTH_GUIDANCE: dict[str, str] = {
    "nv-tools": "install the nv-tools CLI and run its login flow (e.g. `nv-tools auth login`)",
    "x-api": "set X_API_BEARER_TOKEN or X_API_OAUTH2_ACCESS_TOKEN in the host EnvironmentFile / .env",
}

#: Human guidance for satisfying each known declared API.
API_GUIDANCE: dict[str, str] = {
    "slack": "authorize the nv-tools Slack connector (`nv-tools slack list-channels`)",
    "x": "provide an X API token (see the x-api auth bundle)",
    "arxiv": "no credentials required (public API)",
}


#: Human-readable nouns for declared source assets in problem messages.
_ASSET_NOUNS = {
    "logic.skill": "skill",
    "logic.verify": "verify file",
    "content.config": "content config",
}


def _validate_arxiv_content(path: Path, config: LoopcraftConfig) -> None:
    """Validate the effective arXiv content config against its typed model."""
    # Imported lazily so the runtime-neutral probe module stays decoupled from
    # loop-specific content packages at import time.
    from loopcraft.research_intel.arxiv.config import ArxivIntelConfig

    ArxivIntelConfig.load(path)


def _validate_x_content(path: Path, config: LoopcraftConfig) -> None:
    """Validate the effective X content config and its referenced assets.

    Beyond the typed model, a configured ``sources.following_snapshot`` is a
    declared fetch source: it must exist as a contained regular file with the
    expected JSON shape, so a scheduled run can never silently drop it.
    """
    from loopcraft.research_intel.x.config import IntelConfig
    from loopcraft.research_intel.x.follow_discovery import load_following_snapshot_handles

    content = IntelConfig.load(path)
    snapshot = content.sources.following_snapshot
    if snapshot is not None:
        resolved = config.resolve_source_path(snapshot.as_posix())
        load_following_snapshot_handles(resolved)


def _x_content_assets(path: Path, config: LoopcraftConfig) -> list[str]:
    """Source-relative extra assets referenced by the effective X content config."""
    from loopcraft.research_intel.x.config import IntelConfig

    content = IntelConfig.load(path)
    snapshot = content.sources.following_snapshot
    return [snapshot.as_posix()] if snapshot else []


#: Content-config validators: loop id -> callable(public config path, config)
#: that loads the effective public/local file through the loop's typed model and
#: raises on any parse/schema/referenced-asset problem. Loops without a
#: registered validator get a plain YAML parse of the effective file.
#: Injectable like the probe registries.
CONTENT_VALIDATORS: dict[str, Callable[[Path, LoopcraftConfig], None]] = {
    "arxiv-intel": _validate_arxiv_content,
    "x-intel": _validate_x_content,
}

#: Content-asset resolvers: loop id -> callable returning extra source-relative
#: asset paths referenced by the effective content config, so staging can
#: materialize them into the run worktree alongside the config itself.
CONTENT_ASSET_RESOLVERS: dict[str, Callable[[Path, LoopcraftConfig], list[str]]] = {
    "x-intel": _x_content_assets,
}


def content_assets(loop: LoopManifest, config: LoopcraftConfig) -> list[str]:
    """Extra source-relative assets referenced by the loop's content config.

    Best-effort: an unreadable or invalid content config yields no assets here —
    preflight separately reports those as problems before staging matters.
    """
    declared = loop.content.config
    resolver = CONTENT_ASSET_RESOLVERS.get(loop.id)
    if not declared or resolver is None:
        return []
    try:
        path = config.resolve_source_path(declared)
        return resolver(path, config)
    except Exception:  # noqa: BLE001 — preflight owns reporting config problems
        return []


def _check_content_config_validity(config: LoopcraftConfig, loop: LoopManifest) -> list[str]:
    """Parse/validate the effective content config before agent startup.

    Existence alone does not establish that the loop can consume its declared
    config; a syntax or schema error found after launching a headless agent
    wastes the run budget. Runs only when the public file exists (missing or
    escaping configs are already reported by the existence check).

    Returns:
        A list of problem strings (empty when no config is declared or the
        effective public/local file parses and validates).
    """
    declared = loop.content.config
    if not declared:
        return []
    try:
        path = config.resolve_source_path(declared)
    except SourcePathError:
        return []
    if not path.is_file():
        return []
    # The effective file may be the gitignored local sibling; contain it before
    # any validator reads it — the same rule staging applies when copying it.
    local = local_sibling_path(path)
    if local.exists():
        try:
            assert_under(config.source_path, local, label="content config local override")
        except ValueError as exc:
            return [f"content.config: {exc}"]
    validator = CONTENT_VALIDATORS.get(loop.id)
    try:
        if validator is not None:
            validator(path, config)
        else:
            yaml.safe_load(local_override_path(path).read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — any config load fault is a preflight problem
        return [f"content config invalid: {declared}: {exc}"]
    return []


def _check_source_asset(config: LoopcraftConfig, declared: str, *, label: str, required: bool) -> list[str]:
    """Validate one declared source asset exists as a committed regular file.

    Per the public/private config split, the *public* file must exist in the
    source tree; a gitignored ``*.local.*`` sibling may shadow its values at
    staging time but never replaces the existence contract — otherwise a loop
    would pass on one host and fail after a clean checkout.

    Args:
        config: Resolved control-plane config used to resolve source paths.
        declared: The declared source-relative path (may be empty).
        label: Manifest field name for error messages (e.g. ``logic.skill``).
        required: Whether an empty ``declared`` is itself a problem.

    Returns:
        A list of problem strings (empty when the asset is present/valid).
    """
    if not declared:
        return [f"{label} is required"] if required else []
    try:
        path = config.resolve_source_path(declared)
    except SourcePathError as exc:
        return [f"{label}: {exc}"]
    noun = _ASSET_NOUNS.get(label, label)
    if not path.exists():
        return [f"{noun} not found: {declared}"]
    if not path.is_file():
        return [f"{noun} is not a regular file: {declared}"]
    return []


def check_declared_capabilities(loop: LoopManifest, config: LoopcraftConfig) -> list[str]:
    """Validate a loop's declared dependencies in a runtime-neutral way.

    Checks the skill, verify, and content-config assets, declared tools on PATH,
    required env vars, and the declared auth bundles/APIs against the shared
    probe registries. Unknown auth bundles or APIs are flagged so a missing
    setup is caught before a headless run rather than inside the agent.

    Args:
        loop: The loop manifest to check.
        config: Resolved control-plane config.

    Returns:
        A list of problem strings (empty when all declared capabilities pass).
    """
    problems: list[str] = []
    problems += _check_source_asset(config, loop.logic.skill or "", label="logic.skill", required=True)
    problems += _check_source_asset(config, loop.logic.verify or "", label="logic.verify", required=False)
    problems += _check_source_asset(config, loop.content.config or "", label="content.config", required=False)
    problems += _check_content_config_validity(config, loop)

    for tool in loop.depends_on.tools:
        binary = TOOL_BINARIES.get(tool, tool)
        if config.which(binary) is None:
            problems.append(f"declared tool '{tool}' not found on PATH ({binary})")

    for var in loop.depends_on.env:
        if not config.env_value(var):
            problems.append(f"required env var not set: {var}")

    for bundle in loop.depends_on.auth:
        probe = AUTH_PROBES.get(bundle)
        if probe is None:
            problems.append(f"no preflight probe for declared auth bundle '{bundle}'")
            continue
        problem = probe(config)
        if problem:
            problems.append(problem)

    for api in loop.depends_on.apis:
        probe = API_PROBES.get(api)
        if probe is None:
            problems.append(f"no preflight probe for declared api '{api}'")
            continue
        problem = probe(config)
        if problem:
            problems.append(problem)

    return problems
