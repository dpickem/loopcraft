"""``loopctl`` control-plane CLI.

Dispatches loop lifecycle commands (run/validate/apply/list/status/logs/deps)
against a resolved :class:`LoopcraftConfig`. Every command supports a consistent,
agent-friendly ``--json`` envelope (``{command, ok, exit_code, data}``) in
addition to human-readable text output.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loopcraft.cli_output import emit as _emit
from loopcraft.config import RUN_ID_ENV, LoopcraftConfig, SourcePathError
from loopcraft.env import load_dotenv
from loopcraft.manifest import LoopManifest, ManifestError, load_all, loop_id_problem
from loopcraft.paths import assert_under
from loopcraft.runners import RunContext, get_runner
from loopcraft.runners.base import STATUS_DONE
from loopcraft.store import RunRecord, Store
from loopcraft.worktree import StagingError, stage_loop_assets

#: Scratch area (under the memory tree) for per-run worktrees.
_WORKTREES_SUBPATH = ("var", "worktrees")


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, load config, and dispatch to the selected command."""
    parser = argparse.ArgumentParser(prog="loopctl", description="Loopcraft control plane.")
    parser.add_argument("--source", help="Source tree root (default: auto-detect / LOOPCRAFT_SOURCE).")
    parser.add_argument("--json", action="store_true", help="Emit a structured JSON result envelope.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Run one loop now, headless.")
    p_run.add_argument("loop", help="Loop id (filename stem in loops/).")
    p_run.add_argument("--vendor", help="Override the runtime vendor for this run.")
    p_run.add_argument("--dry-run", action="store_true", help="Preflight + show the invocation; do not execute.")

    p_validate = sub.add_parser("validate", help="Validate all manifests in loops/.")
    p_validate.add_argument("loops_dir", nargs="?", help="Override the loops directory.")

    p_apply = sub.add_parser("apply", help="Validate manifests (deployment lands in M2).")
    p_apply.add_argument("loops_dir", nargs="?", help="Override the loops directory.")
    p_apply.add_argument("--dry-run", action="store_true", help="Validate only.")

    sub.add_parser("list", help="List known loops.")
    sub.add_parser("status", help="Show fleet status (last run per loop).")

    p_logs = sub.add_parser("logs", help="Print the last run log for a loop.")
    p_logs.add_argument("loop", help="Loop id.")

    p_deps = sub.add_parser("deps", help="Dependency helpers.")
    p_deps.add_argument("action", choices=["check"], help="check: probe runtimes/tools on PATH.")
    p_deps.add_argument(
        "--loop",
        help="Also run the selected loop's adapter preflight (auth/api/model/skill).",
    )

    args = parser.parse_args(argv)
    load_dotenv()
    config = LoopcraftConfig.load(args.source)
    as_json = args.json

    if args.command == "run":
        return _cmd_run(config, args.loop, vendor=args.vendor, dry_run=args.dry_run, as_json=as_json)
    if args.command == "validate":
        return _cmd_validate(config, args.loops_dir, as_json=as_json)
    if args.command == "apply":
        return _cmd_apply(config, args.loops_dir, as_json=as_json)
    if args.command == "list":
        return _cmd_list(config, as_json=as_json)
    if args.command == "status":
        return _cmd_status(config, as_json=as_json)
    if args.command == "logs":
        return _cmd_logs(config, args.loop, as_json=as_json)
    if args.command == "deps":
        return _cmd_deps_check(config, loop_id=args.loop, as_json=as_json)
    return 2


def _find_manifest(config: LoopcraftConfig, loop_id: str) -> LoopManifest | None:
    """Return the manifest for ``loop_id`` from the loops directory, if present.

    The loop selector is an id, not a file path: it must match the canonical
    loop-id vocabulary before any path is constructed, the resolved manifest must
    stay directly under ``loops/``, and its ``id`` field must equal the filename
    stem it was looked up by.

    Raises:
        ManifestError: If the selector is not a canonical loop id, the manifest
            cannot be parsed/validated, or its id differs from the filename stem.
    """
    problem = loop_id_problem(loop_id)
    if problem is not None:
        raise ManifestError(f"invalid loop id {loop_id!r}: {problem}")
    for ext in (".yaml", ".yml"):
        path = config.loops_dir / f"{loop_id}{ext}"
        try:
            assert_under(config.loops_dir, path, label="loop manifest path")
        except ValueError as exc:
            raise ManifestError(str(exc)) from exc
        if path.exists():
            manifest = LoopManifest.load(path)
            if manifest.id != loop_id:
                raise ManifestError(
                    f"{path.name}: manifest id {manifest.id!r} does not match filename stem {loop_id!r}"
                )
            return manifest
    return None


def _cmd_run(
    config: LoopcraftConfig,
    loop_id: str,
    *,
    vendor: str | None,
    dry_run: bool,
    as_json: bool,
) -> int:
    """Run one loop headless (or preflight it via ``--dry-run``)."""
    try:
        manifest = _find_manifest(config, loop_id)
    except ManifestError as exc:
        return _emit(
            "run",
            as_json=as_json,
            ok=False,
            rc=2,
            data={"loop": loop_id, "error": str(exc)},
            lines=[f"error: {exc}"],
        )
    if manifest is None:
        return _emit(
            "run",
            as_json=as_json,
            ok=False,
            rc=2,
            data={"loop": loop_id, "error": f"loop '{loop_id}' not found in {config.loops_dir}"},
            lines=[f"error: loop '{loop_id}' not found in {config.loops_dir}"],
        )

    problems = manifest.validate()
    if problems:
        return _emit(
            "run",
            as_json=as_json,
            ok=False,
            rc=2,
            data={"loop": loop_id, "invalid_manifest": problems},
            lines=[f"invalid manifest: {problem}" for problem in problems],
        )

    effective_vendor = vendor or manifest.effective_vendor(config.default_vendor)
    try:
        runner = get_runner(effective_vendor)
    except ValueError as exc:
        return _emit(
            "run",
            as_json=as_json,
            ok=False,
            rc=2,
            data={"loop": loop_id, "error": str(exc)},
            lines=[f"error: {exc}"],
        )

    preflight = runner.preflight(manifest, config)

    if dry_run:
        return _run_dry_run(config, manifest, effective_vendor, preflight, as_json=as_json)

    return _run_execute(config, manifest, runner, effective_vendor, preflight, as_json=as_json)


def _run_dry_run(
    config: LoopcraftConfig,
    manifest: LoopManifest,
    effective_vendor: str,
    preflight,
    *,
    as_json: bool,
) -> int:
    """Report the planned invocation and preflight result without executing."""
    resolved_outputs = [
        config.resolve_state_template(o, run_id="<run_id>", date="<date>")
        for o in manifest.outputs
    ]
    data = {
        "loop": manifest.id,
        "vendor": effective_vendor,
        "model": manifest.runtime.model,
        "resolved_outputs": [str(p) for p in resolved_outputs],
        "preflight": {"ok": preflight.ok, "problems": preflight.problems},
    }
    lines = [
        f"loop:    {manifest.id}",
        f"vendor:  {effective_vendor}  model: {manifest.runtime.model or '(default)'}",
        f"outputs: {[str(p) for p in resolved_outputs] or '(none)'}",
        f"preflight: {'OK' if preflight.ok else 'PROBLEMS'}",
        *[f"  - {problem}" for problem in preflight.problems],
    ]
    return _emit("run", as_json=as_json, ok=preflight.ok, rc=0 if preflight.ok else 1, data=data, lines=lines)


def _run_execute(
    config: LoopcraftConfig,
    manifest: LoopManifest,
    runner,
    effective_vendor: str,
    preflight,
    *,
    as_json: bool,
) -> int:
    """Stage assets, execute the loop, and record the run."""
    store = Store(config)
    run_id = store.new_run_id()
    started = datetime.now(UTC)
    start_perf = time.perf_counter()
    resolved_outputs = [
        config.resolve_state_template(o, run_id=run_id, date=started.date().isoformat())
        for o in manifest.outputs
    ]

    if not preflight.ok:
        return _record_run_failure(
            store,
            manifest,
            effective_vendor,
            preflight.problems,
            run_id,
            started,
            start_perf,
            phase="preflight",
            as_json=as_json,
        )

    worktree = _worktree_dir(config, manifest.id, run_id)
    worktree.mkdir(parents=True, exist_ok=True)
    try:
        stage_loop_assets(config, manifest, worktree)
    except (StagingError, SourcePathError) as exc:
        return _record_run_failure(
            store,
            manifest,
            effective_vendor,
            [str(exc)],
            run_id,
            started,
            start_perf,
            phase="staging",
            as_json=as_json,
        )
    ctx = RunContext(
        config=config,
        workdir=worktree,
        log_path=worktree / "run.log",
        resolved_outputs=resolved_outputs,
        # Hand the control-plane run id to any direct CLI the loop invokes so its
        # run-scoped history archives match the manifest's {{run_id}} outputs.
        env={RUN_ID_ENV: run_id},
    )

    result = runner.run(manifest, ctx)
    ended = datetime.now(UTC)

    record = RunRecord(
        run_id=run_id,
        loop=manifest.id,
        vendor=effective_vendor,
        model=manifest.runtime.model,
        status=result.status,
        started_at=started.isoformat(),
        ended_at=ended.isoformat(),
        duration_s=round(time.perf_counter() - start_perf, 3),
        exit_code=result.exit_code,
        tokens=result.tokens,
        cost_usd=result.cost_usd,
        iterations=result.iterations,
        inputs=manifest.inputs,
        outputs=result.outputs,
        declared_outputs=manifest.outputs,
        log_path=str(result.log_path) if result.log_path else None,
        problems=result.problems,
    )
    record_path = store.record_run(record)
    _prune_loop_worktrees(config, manifest.id, keep_last=config.worktree_keep_last)

    ok = result.status == STATUS_DONE
    data = {
        "loop": manifest.id,
        "status": result.status,
        "log": str(result.log_path) if result.log_path else None,
        "run_record": str(record_path),
        "outputs": result.outputs,
        "problems": result.problems,
    }
    lines = [
        f"loop:   {manifest.id}",
        f"status: {result.status}",
        f"log:    {result.log_path}",
        f"run record: {record_path}",
        *[f"output: {produced}" for produced in result.outputs],
    ]
    rc = _emit("run", as_json=as_json, ok=ok, rc=0 if ok else 1, data=data, lines=lines)
    if not as_json:
        for problem in result.problems:
            print(f"  ! {problem}", file=sys.stderr)
    return rc


def _record_run_failure(
    store: Store,
    manifest: LoopManifest,
    effective_vendor: str,
    problems: list[str],
    run_id: str,
    started: datetime,
    start_perf: float,
    *,
    phase: str,
    as_json: bool,
) -> int:
    """Persist a run record for a failure before execution and report it.

    Execution never started, so ``outputs`` (files actually produced) is empty;
    the manifest's declared contract is preserved separately in
    ``declared_outputs`` so a failed run never claims false provenance.

    Args:
        phase: Which pre-execution phase failed (``preflight`` or ``staging``).
    """
    record = RunRecord(
        run_id=run_id,
        loop=manifest.id,
        vendor=effective_vendor,
        model=manifest.runtime.model,
        status="failed",
        started_at=started.isoformat(),
        ended_at=datetime.now(UTC).isoformat(),
        duration_s=round(time.perf_counter() - start_perf, 3),
        inputs=manifest.inputs,
        outputs=[],
        declared_outputs=manifest.outputs,
        problems=problems,
    )
    path = store.record_run(record)
    data = {
        "loop": manifest.id,
        "status": "failed",
        "phase": phase,
        "problems": problems,
        "run_record": str(path),
    }
    if not as_json:
        print(f"{phase} failed; loop not run:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(f"run record: {path}", file=sys.stderr)
    return _emit("run", as_json=as_json, ok=False, rc=1, data=data, lines=[])


def _worktrees_root(config: LoopcraftConfig) -> Path:
    """Root of the per-run worktree scratch area in the memory tree."""
    return config.memory_path.joinpath(*_WORKTREES_SUBPATH)


def _worktree_dir(config: LoopcraftConfig, loop_id: str, run_id: str) -> Path:
    """Return the per-run worktree directory for a loop.

    The result is verified to remain under the worktree root, so manifest data
    can never choose an arbitrary staging directory (id validation upstream makes
    this unreachable; the guard is defense in depth).

    Raises:
        ValueError: If ``loop_id``/``run_id`` would escape the worktree root.
    """
    root = _worktrees_root(config)
    path = root.joinpath(loop_id, run_id)
    assert_under(root, path, label="run worktree")
    return path


def _prune_loop_worktrees(config: LoopcraftConfig, loop_id: str, *, keep_last: int) -> list[Path]:
    """Keep only the newest N per-run worktree directories for one loop.

    The worktree area is scratch/debug state under ``<memory>/var/worktrees``;
    durable run records and outputs live in ``ledger/``. Pruning therefore never
    removes canonical loop state. ``keep_last`` is clamped by config to 0..100.

    Raises:
        ValueError: If ``loop_id`` would make the prune target escape the
            worktree root (defense in depth; ids are validated upstream).
    """
    root = _worktrees_root(config)
    loop_dir = root / loop_id
    assert_under(root, loop_dir, label="worktree prune target")
    if not loop_dir.exists():
        return []

    children = [p for p in loop_dir.iterdir() if p.is_dir()]
    children.sort(key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)
    removed: list[Path] = []
    for path in children[keep_last:]:
        shutil.rmtree(path)
        removed.append(path)
    return removed


def _cmd_validate(config: LoopcraftConfig, loops_dir: str | None, *, as_json: bool) -> int:
    """Validate every manifest in the loops directory."""
    target = Path(loops_dir) if loops_dir else config.loops_dir
    manifests, problems = load_all(target)
    ok = not problems
    data = {
        "loops_dir": str(target),
        "validated": len(manifests),
        "ok": ok,
        "problems": problems,
    }
    lines = [f"validated {len(manifests)} manifest(s) in {target}"]
    if ok:
        lines.append("all manifests valid")
    rc = _emit("validate", as_json=as_json, ok=ok, rc=0 if ok else 1, data=data, lines=lines)
    if not as_json and problems:
        for problem in problems:
            print(f"  FAIL {problem}", file=sys.stderr)
    return rc


def _cmd_apply(config: LoopcraftConfig, loops_dir: str | None, *, as_json: bool) -> int:
    """Validate manifests; scheduler deployment lands in M2."""
    target = Path(loops_dir) if loops_dir else config.loops_dir
    manifests, problems = load_all(target)
    ok = not problems
    note = "scheduler deployment (systemd units) lands in M2; M1 validates only."
    data = {
        "loops_dir": str(target),
        "validated": len(manifests),
        "ok": ok,
        "problems": problems,
        "note": note,
    }
    lines = [f"validated {len(manifests)} manifest(s) in {target}"]
    if ok:
        lines.append("all manifests valid")
        lines.append(f"note: {note}")
    rc = _emit("apply", as_json=as_json, ok=ok, rc=0 if ok else 1, data=data, lines=lines)
    if not as_json and problems:
        for problem in problems:
            print(f"  FAIL {problem}", file=sys.stderr)
    return rc


def _cmd_list(config: LoopcraftConfig, *, as_json: bool) -> int:
    """List known loops with tier and effective vendor.

    Manifest problems (unparseable or invalid manifests) degrade the result:
    every loadable loop is still listed as partial data, but the problems are
    reported and the exit code is nonzero so a broken manifest can never
    silently vanish from the fleet view.
    """
    manifests, problems = load_all(config.loops_dir)
    ok = not problems
    loops = [
        {
            "id": m.id,
            "tier": str(m.tier),
            "vendor": m.effective_vendor(config.default_vendor),
            "name": m.name,
        }
        for m in manifests
    ]
    if not manifests:
        lines = [f"no loops found in {config.loops_dir}"]
    else:
        lines = [
            f"{m.id:24} tier={m.tier:8} vendor={m.effective_vendor(config.default_vendor):7} {m.name}"
            for m in manifests
        ]
    rc = _emit(
        "list",
        as_json=as_json,
        ok=ok,
        rc=0 if ok else 1,
        data={"loops": loops, "problems": problems},
        lines=lines,
    )
    if not as_json:
        for problem in problems:
            print(f"  ! {problem}", file=sys.stderr)
    return rc


def _cmd_status(config: LoopcraftConfig, *, as_json: bool) -> int:
    """Show the last run per loop.

    Like ``list``, manifest problems degrade the result instead of hiding it:
    loadable loops are still reported, problems are surfaced, and the exit code
    is nonzero — the fleet view must stay trustworthy exactly when configuration
    is broken.
    """
    manifests, problems = load_all(config.loops_dir)
    ok = not problems
    store = Store(config)
    loops: list[dict[str, Any]] = []
    lines: list[str] = []
    if not manifests:
        lines.append(f"no loops found in {config.loops_dir}")
    for manifest in manifests:
        latest = store.latest_run(manifest.id)
        loops.append(
            {
                "id": manifest.id,
                "last_status": latest.status if latest else None,
                "last_started_at": latest.started_at if latest else None,
            }
        )
        summary = f"{latest.status} @ {latest.started_at}" if latest else "never run"
        lines.append(f"{manifest.id:24} {summary}")
    rc = _emit(
        "status",
        as_json=as_json,
        ok=ok,
        rc=0 if ok else 1,
        data={"loops": loops, "problems": problems},
        lines=lines,
    )
    if not as_json:
        for problem in problems:
            print(f"  ! {problem}", file=sys.stderr)
    return rc


def _cmd_logs(config: LoopcraftConfig, loop_id: str, *, as_json: bool) -> int:
    """Print the last run log for a loop."""
    store = Store(config)
    latest = store.latest_run(loop_id)
    if latest is None:
        return _emit(
            "logs",
            as_json=as_json,
            ok=False,
            rc=1,
            data={"loop": loop_id, "error": "no runs recorded"},
            lines=[f"no runs recorded for '{loop_id}'"],
        )
    if not latest.log_path or not Path(latest.log_path).exists():
        return _emit(
            "logs",
            as_json=as_json,
            ok=False,
            rc=1,
            data={"loop": loop_id, "run_id": latest.run_id, "error": "no log on disk"},
            lines=[f"run {latest.run_id} has no log on disk"],
        )
    log_text = Path(latest.log_path).read_text(encoding="utf-8")
    return _emit(
        "logs",
        as_json=as_json,
        ok=True,
        rc=0,
        data={"loop": loop_id, "run_id": latest.run_id, "log_path": latest.log_path, "log": log_text},
        lines=[log_text],
    )


def _probe_dependency_table(table: dict[str, str]) -> tuple[list[dict[str, Any]], list[str]]:
    """Probe a name->binary table on PATH; return (per-dep records, missing names)."""
    records: list[dict[str, Any]] = []
    missing: list[str] = []
    for name, binary in table.items():
        found = binary if binary.startswith("/") else shutil.which(binary)
        records.append({"name": name, "binary": binary, "found": bool(found)})
        if not found:
            missing.append(name)
    return records, missing


def _cmd_deps_check(config: LoopcraftConfig, *, loop_id: str | None = None, as_json: bool = False) -> int:
    """Check dependencies, or (with ``--loop``) only that loop's preflight.

    Without ``--loop``: required M1 binaries are probed and a missing one fails
    the check, while optional/future-runtime binaries are reported but never fail
    it. With ``--loop <id>``: only the selected loop's adapter preflight runs, so
    the check reflects exactly that loop's declared runtime and dependencies.
    """
    if loop_id:
        rc, preflight_data, preflight_lines = _preflight_loop(config, loop_id)
        return _emit(
            "deps.check",
            as_json=as_json,
            ok=rc == 0,
            rc=rc,
            data={"loop": loop_id, "preflight": preflight_data},
            lines=[line.lstrip("\n") for line in preflight_lines],
        )

    required, missing = _probe_dependency_table(config.dependencies)
    optional, optional_missing = _probe_dependency_table(config.optional_dependencies)
    rc = 1 if missing else 0

    lines = [f"[{'ok ' if dep['found'] else 'MISSING'}] {dep['name']}" for dep in required]
    lines += [
        f"[{'ok ' if dep['found'] else 'optional'}] {dep['name']} (future runtime)"
        for dep in optional
    ]

    data = {
        "dependencies": required,
        "missing": missing,
        "optional_dependencies": optional,
        "optional_missing": optional_missing,
    }
    return _emit("deps.check", as_json=as_json, ok=rc == 0, rc=rc, data=data, lines=lines)


def _preflight_loop(config: LoopcraftConfig, loop_id: str) -> tuple[int, dict[str, Any], list[str]]:
    """Run a loop's adapter preflight; return (exit code, data, text lines)."""
    try:
        manifest = _find_manifest(config, loop_id)
    except ManifestError as exc:
        return 2, {"loop": loop_id, "error": str(exc)}, [f"error: {exc}"]
    if manifest is None:
        message = f"error: loop '{loop_id}' not found in {config.loops_dir}"
        return 2, {"loop": loop_id, "error": message}, [message]
    vendor = manifest.effective_vendor(config.default_vendor)
    try:
        runner = get_runner(vendor)
    except ValueError as exc:
        return 2, {"loop": loop_id, "error": str(exc)}, [f"error: {exc}"]
    report = runner.preflight(manifest, config)
    lines = [
        f"\npreflight {loop_id} ({vendor}): {'OK' if report.ok else 'PROBLEMS'}",
        *[f"  - {problem}" for problem in report.problems],
    ]
    data = {"loop": loop_id, "vendor": vendor, "ok": report.ok, "problems": report.problems}
    return (0 if report.ok else 1), data, lines


if __name__ == "__main__":
    raise SystemExit(main())
