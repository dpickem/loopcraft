# Milestone 1 Review 02 — Response

Response to [`2026_06_30_milestone_1_review_02_report.md`](2026_06_30_milestone_1_review_02_report.md)
(the follow-up re-review of the local M1 branch). Review 01 history:
[01 report](2026_06_30_milestone_1_review_01_report.md) /
[01 response](2026_06_30_milestone_1_review_01_response.md).

All three findings are addressed below. Each fix has a regression test that would have failed
before the change.

## Commit

The M1 code was reviewed before its first commit, so the review 01 and review 02 fixes are folded
into the M1 implementation commit on `main`:

```text
04ff46e feat: add M1 loopcraft control plane
```

Inspect a finding's fix with `git show 04ff46e -- <file>` using the file lists below.

## Status

Changes are in the local working tree of `/Users/dpickem/workspace/loopcraft` (M1 is not yet
committed). Verification from the repo root:

```text
make test && make compile && make validate
```

- `make test`: **70 passed** (up from 65 at review time), fully offline.
- `make compile`: passes.
- `make validate`: passes, 1 manifest.

## Findings

### 1. Repeated runs can no longer pass by reusing stale ledger outputs — fixed

`CodexRunner.run()` now snapshots each declared output's modification time **before** invoking
Codex and, on a clean exit, requires every output to have been created or rewritten during the run.

- Before the subprocess call, the runner records `st_mtime_ns` for each resolved output (or `None`
  when absent).
- After a `returncode == 0` exit, an output is **missing** if it does not exist and **stale** if it
  existed before and its mtime is unchanged. A run is `done` only when there are no missing and no
  stale outputs; otherwise it is `failed` with a `declared output not refreshed this run: <path>`
  problem. `RunResult.outputs` now lists only the outputs actually written this run, so the run
  record reflects reality (a stale `seen.json` left untouched is not reported as produced).
- A write updates mtime, so a legitimate byte-identical cursor rewrite still passes; only a genuine
  no-op leaves the file stale.
- Files: `src/loopcraft/runners/codex.py`.
- Tests: `tests/test_runner.py::test_run_flags_stale_unrefreshed_output` (both outputs pre-exist,
  the fake Codex returns 0 without touching them, result is `failed` and the path is not in
  `outputs`). The existing fresh-tree and missing-output tests still cover the produced/missing
  cases.

### 2. `logic.skill` paths are validated against the source boundary — fixed

Skill references are now treated as a first-class path boundary, mirroring the ledger-path guards.

- New `safe_source_relpath()` + `SourcePathError` and a `LoopcraftConfig.resolve_source_path()`
  helper in `src/loopcraft/config.py`: a source-relative path must be non-empty with no absolute
  root, drive/UNC or scheme head, and no `..` segment, and the resolved path must stay inside the
  source tree.
- It is enforced in all three places the report called out:
  - `LoopManifest.validate()` rejects an unsafe `logic.skill` (`src/loopcraft/manifest.py`).
  - `CodexRunner.preflight()` resolves the skill through `resolve_source_path()` and reports an
    unsafe value instead of blindly joining it (`src/loopcraft/runners/codex.py`).
  - `stage_loop_assets()` validates the skill, resolves it safely, and asserts every staged
    destination remains under the run workdir (`src/loopcraft/worktree.py`).
- Files: `src/loopcraft/config.py`, `src/loopcraft/manifest.py`,
  `src/loopcraft/runners/codex.py`, `src/loopcraft/worktree.py`.
- Tests: `tests/test_manifest.py::test_unsafe_skill_paths_are_reported` (absolute + traversing);
  `tests/test_worktree.py::test_stage_rejects_traversing_skill` and
  `::test_all_staged_paths_stay_under_workdir`.

### 3. Scheme-style outputs are now rejected by validation, matching run resolution — fixed

The Review 01 fix exempted scheme targets (e.g. `linear:project/Daily`) from path validation, which
let a manifest pass `make validate` and then fail when `_cmd_run()` resolved every output as a
ledger path. For M1 — which has no external-sink/artifact abstraction yet — validation now rejects
non-state inputs/outputs, so validation and run agree.

- `LoopManifest.validate()` reports any input/output that is not a ledger `state/...` path with
  `external sink '<target>' is not supported in M1 (only ledger 'state/...' paths)`
  (`src/loopcraft/manifest.py`).
- Because `_cmd_run()` validates the manifest before resolving outputs, a scheme output is now
  refused up front (exit 2) rather than crashing in `resolve_state_path()`.
- Files: `src/loopcraft/manifest.py`.
- Tests: `tests/test_manifest.py::test_scheme_outputs_are_rejected_in_m1` (replaces the old
  Review 01 exemption test) and `tests/test_cli.py::test_scheme_output_fails_validate_and_run`
  (validate returns 1 and `run` returns 2 for the same manifest).

## Notes on resolved/healthy areas

The report's healthy-area notes still hold: the Slack cursor contract is explicit in the manifest;
Codex preflight covers declared auth/API probes, tools, env vars, skill presence, and broad model
mismatch; runtime timeout returns a normalized `stalled` result with a timeout log; and ledger
state paths are guarded at validation and write time. Source-asset staging now has the same
boundary protection.

## Deferred / follow-ups

- External output sinks (Linear projects, artifact stores) are intentionally rejected in M1; they
  return when the artifact/sink abstraction lands (M5 Linear sync / artifact store), at which point
  `outputs` can carry both ledger and external targets with separate resolution.
- Promoting the per-run asset bundle to a real `git worktree` (so `--skip-git-repo-check` can be
  dropped) remains an M2 follow-up, unchanged from Review 01.
