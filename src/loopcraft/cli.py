from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from .config import LoopcraftConfig
from .env import load_dotenv
from .manifest import LoopManifest, load_all
from .runners import RunContext, get_runner
from .runners.base import STATUS_DONE
from .store import RunRecord, Store
from .worktree import stage_loop_assets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="loopctl", description="Loopcraft control plane.")
    parser.add_argument("--source", help="Source tree root (default: auto-detect / LOOPCRAFT_SOURCE).")
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

    if args.command == "run":
        return _cmd_run(config, args.loop, vendor=args.vendor, dry_run=args.dry_run)
    if args.command == "validate":
        return _cmd_validate(config, args.loops_dir)
    if args.command == "apply":
        return _cmd_apply(config, args.loops_dir)
    if args.command == "list":
        return _cmd_list(config)
    if args.command == "status":
        return _cmd_status(config)
    if args.command == "logs":
        return _cmd_logs(config, args.loop)
    if args.command == "deps":
        return _cmd_deps_check(config, loop_id=args.loop)
    return 2


def _find_manifest(config: LoopcraftConfig, loop_id: str) -> LoopManifest | None:
    for ext in (".yaml", ".yml"):
        path = config.loops_dir / f"{loop_id}{ext}"
        if path.exists():
            return LoopManifest.load(path)
    return None


def _cmd_run(config: LoopcraftConfig, loop_id: str, *, vendor: str | None, dry_run: bool) -> int:
    manifest = _find_manifest(config, loop_id)
    if manifest is None:
        print(f"error: loop '{loop_id}' not found in {config.loops_dir}", file=sys.stderr)
        return 2

    problems = manifest.validate()
    if problems:
        for problem in problems:
            print(f"invalid manifest: {problem}", file=sys.stderr)
        return 2

    effective_vendor = vendor or manifest.effective_vendor(config.default_vendor)
    try:
        runner = get_runner(effective_vendor)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    preflight = runner.preflight(manifest, config)
    resolved_outputs = [config.resolve_state_path(o) for o in manifest.outputs]

    if dry_run:
        print(f"loop:    {manifest.id}")
        print(f"vendor:  {effective_vendor}  model: {manifest.runtime.model or '(default)'}")
        print(f"outputs: {[str(p) for p in resolved_outputs] or '(none)'}")
        print(f"preflight: {'OK' if preflight.ok else 'PROBLEMS'}")
        for problem in preflight.problems:
            print(f"  - {problem}")
        return 0 if preflight.ok else 1

    store = Store(config)
    run_id = store.new_run_id()
    started = datetime.now(UTC)
    start_perf = time.perf_counter()

    if not preflight.ok:
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
        print("preflight failed; loop not run:", file=sys.stderr)
        for problem in preflight.problems:
            print(f"  - {problem}", file=sys.stderr)
        print(f"run record: {path}", file=sys.stderr)
        return 1

    worktree = config.memory_path / "var" / "worktrees" / manifest.id / run_id
    worktree.mkdir(parents=True, exist_ok=True)
    stage_loop_assets(config, manifest, worktree)
    log_path = worktree / "run.log"
    ctx = RunContext(
        config=config,
        workdir=worktree,
        log_path=log_path,
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

    print(f"loop:   {manifest.id}")
    print(f"status: {result.status}")
    print(f"log:    {result.log_path}")
    print(f"run record: {record_path}")
    for produced in result.outputs:
        print(f"output: {produced}")
    for problem in result.problems:
        print(f"  ! {problem}", file=sys.stderr)
    return 0 if result.status == STATUS_DONE else 1


def _cmd_validate(config: LoopcraftConfig, loops_dir: str | None) -> int:
    target = Path(loops_dir) if loops_dir else config.loops_dir
    manifests, problems = load_all(target)
    print(f"validated {len(manifests)} manifest(s) in {target}")
    if problems:
        for problem in problems:
            print(f"  FAIL {problem}", file=sys.stderr)
        return 1
    print("all manifests valid")
    return 0


def _cmd_apply(config: LoopcraftConfig, loops_dir: str | None) -> int:
    rc = _cmd_validate(config, loops_dir)
    if rc != 0:
        return rc
    print("note: scheduler deployment (systemd units) lands in M2; M1 validates only.")
    return 0


def _cmd_list(config: LoopcraftConfig) -> int:
    manifests, _ = load_all(config.loops_dir)
    if not manifests:
        print(f"no loops found in {config.loops_dir}")
        return 0
    for manifest in manifests:
        vendor = manifest.effective_vendor(config.default_vendor)
        print(f"{manifest.id:24} tier={manifest.tier:8} vendor={vendor:7} {manifest.name}")
    return 0


def _cmd_status(config: LoopcraftConfig) -> int:
    manifests, _ = load_all(config.loops_dir)
    store = Store(config)
    if not manifests:
        print(f"no loops found in {config.loops_dir}")
        return 0
    for manifest in manifests:
        latest = store.latest_run(manifest.id)
        if latest:
            summary = f"{latest.status} @ {latest.started_at}"
        else:
            summary = "never run"
        print(f"{manifest.id:24} {summary}")
    return 0


def _cmd_logs(config: LoopcraftConfig, loop_id: str) -> int:
    store = Store(config)
    latest = store.latest_run(loop_id)
    if latest is None:
        print(f"no runs recorded for '{loop_id}'", file=sys.stderr)
        return 1
    if not latest.log_path or not Path(latest.log_path).exists():
        print(f"run {latest.run_id} has no log on disk", file=sys.stderr)
        return 1
    print(Path(latest.log_path).read_text(encoding="utf-8"))
    return 0


def _cmd_deps_check(config: LoopcraftConfig, *, loop_id: str | None = None) -> int:
    import shutil

    probes = {
        "codex": "codex",
        "claude": "claude",
        "cursor-agent": "cursor-agent",
        "nv-tools": "nv-tools",
        "git": "git",
        "python": sys.executable,
    }
    missing = []
    for name, binary in probes.items():
        found = shutil.which(binary) if not binary.startswith("/") else binary
        mark = "ok " if found else "MISSING"
        print(f"[{mark}] {name}")
        if not found:
            missing.append(name)
    rc = 1 if missing else 0

    if loop_id:
        rc = _preflight_loop(config, loop_id) or rc
    return rc


def _preflight_loop(config: LoopcraftConfig, loop_id: str) -> int:
    """Run the selected loop's adapter preflight and print declared-dep results."""
    manifest = _find_manifest(config, loop_id)
    if manifest is None:
        print(f"error: loop '{loop_id}' not found in {config.loops_dir}", file=sys.stderr)
        return 2
    vendor = manifest.effective_vendor(config.default_vendor)
    try:
        runner = get_runner(vendor)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    report = runner.preflight(manifest, config)
    print(f"\npreflight {loop_id} ({vendor}): {'OK' if report.ok else 'PROBLEMS'}")
    for problem in report.problems:
        print(f"  - {problem}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
