"""``loopctl`` control-plane CLI.

Dispatches loop lifecycle commands (run/validate/apply/list/status/logs/deps)
against a resolved :class:`LoopcraftConfig`. Every command supports a consistent,
agent-friendly ``--json`` envelope (``{command, ok, exit_code, data}``) in
addition to human-readable text output.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loopcraft.config import LoopcraftConfig
from loopcraft.env import load_dotenv
from loopcraft.manifest import LoopManifest, load_all
from loopcraft.runners import RunContext, get_runner
from loopcraft.runners.base import STATUS_DONE
from loopcraft.store import RunRecord, Store
from loopcraft.worktree import stage_loop_assets

#: Scratch area (under the memory tree) for per-run worktrees.
_WORKTREES_SUBPATH = ("var", "worktrees")


def _emit(command: str, *, as_json: bool, ok: bool, rc: int, data: dict[str, Any], lines: list[str]) -> int:
    """Render a command result and return its exit code.

    In JSON mode a single ``{command, ok, exit_code, data}`` object is printed to
    stdout; otherwise the pre-formatted human ``lines`` are printed. Returning
    ``rc`` lets callers ``return _emit(...)`` directly.
    """
    if as_json:
        envelope = {"command": command, "ok": ok, "exit_code": rc, "data": data}
        print(json.dumps(envelope, indent=2, ensure_ascii=False, default=str))
    else:
        for line in lines:
            print(line)
    return rc


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
    """Return the manifest for ``loop_id`` from the loops directory, if present."""
    for ext in (".yaml", ".yml"):
        path = config.loops_dir / f"{loop_id}{ext}"
        if path.exists():
            return LoopManifest.load(path)
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
    manifest = _find_manifest(config, loop_id)
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
        return _record_preflight_failure(
            store, manifest, effective_vendor, preflight, run_id, started, start_perf, as_json=as_json
        )

    worktree = _worktree_dir(config, manifest.id, run_id)
    worktree.mkdir(parents=True, exist_ok=True)
    stage_loop_assets(config, manifest, worktree)
    ctx = RunContext(
        config=config,
        workdir=worktree,
        log_path=worktree / "run.log",
        resolved_outputs=resolved_outputs,
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


def _record_preflight_failure(
    store: Store,
    manifest: LoopManifest,
    effective_vendor: str,
    preflight,
    run_id: str,
    started: datetime,
    start_perf: float,
    *,
    as_json: bool,
) -> int:
    """Persist a failed-preflight run record and report the problems."""
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
        outputs=manifest.outputs,
        problems=preflight.problems,
    )
    path = store.record_run(record)
    data = {
        "loop": manifest.id,
        "status": "failed",
        "preflight": {"ok": False, "problems": preflight.problems},
        "run_record": str(path),
    }
    if not as_json:
        print("preflight failed; loop not run:", file=sys.stderr)
        for problem in preflight.problems:
            print(f"  - {problem}", file=sys.stderr)
        print(f"run record: {path}", file=sys.stderr)
    return _emit("run", as_json=as_json, ok=False, rc=1, data=data, lines=[])


def _worktree_dir(config: LoopcraftConfig, loop_id: str, run_id: str) -> Path:
    """Return the per-run worktree directory for a loop."""
    return config.memory_path.joinpath(*_WORKTREES_SUBPATH, loop_id, run_id)


def _prune_loop_worktrees(config: LoopcraftConfig, loop_id: str, *, keep_last: int) -> list[Path]:
    """Keep only the newest N per-run worktree directories for one loop.

    The worktree area is scratch/debug state under ``<memory>/var/worktrees``;
    durable run records and outputs live in ``ledger/``. Pruning therefore never
    removes canonical loop state. ``keep_last`` is clamped by config to 0..100.
    """
    loop_dir = config.memory_path.joinpath(*_WORKTREES_SUBPATH, loop_id)
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
    """List known loops with tier and effective vendor."""
    manifests, _ = load_all(config.loops_dir)
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
    return _emit(
        "list", as_json=as_json, ok=True, rc=0, data={"loops": loops}, lines=lines
    )


def _cmd_status(config: LoopcraftConfig, *, as_json: bool) -> int:
    """Show the last run per loop."""
    manifests, _ = load_all(config.loops_dir)
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
    return _emit("status", as_json=as_json, ok=True, rc=0, data={"loops": loops}, lines=lines)


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


def _cmd_deps_check(config: LoopcraftConfig, *, loop_id: str | None = None, as_json: bool = False) -> int:
    """Check external binaries on PATH and optionally a loop's adapter preflight."""
    dependencies: list[dict[str, Any]] = []
    missing: list[str] = []
    lines: list[str] = []
    for name, binary in config.dependencies.items():
        found = shutil.which(binary) if not binary.startswith("/") else binary
        dependencies.append({"name": name, "binary": binary, "found": bool(found)})
        lines.append(f"[{'ok ' if found else 'MISSING'}] {name}")
        if not found:
            missing.append(name)
    rc = 1 if missing else 0

    preflight_data: dict[str, Any] | None = None
    if loop_id:
        preflight_rc, preflight_data, preflight_lines = _preflight_loop(config, loop_id)
        rc = preflight_rc or rc
        lines.extend(preflight_lines)

    data = {"dependencies": dependencies, "missing": missing, "preflight": preflight_data}
    return _emit("deps.check", as_json=as_json, ok=rc == 0, rc=rc, data=data, lines=lines)


def _preflight_loop(config: LoopcraftConfig, loop_id: str) -> tuple[int, dict[str, Any], list[str]]:
    """Run a loop's adapter preflight; return (exit code, data, text lines)."""
    manifest = _find_manifest(config, loop_id)
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
