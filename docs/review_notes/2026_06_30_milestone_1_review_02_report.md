# Milestone 1 Review 02 — Report (response follow-up)

Review target: local uncommitted changes in `/Users/dpickem/workspace/loopcraft`.

Reference baseline:

- `docs/review_notes/2026_06_30_milestone_1_review_01_report.md`
- `docs/review_notes/2026_06_30_milestone_1_review_01_response.md`
- M1 in `obsidian/dpickem_default/40-Resources/docs/loopcraft-implementation-design.html`
- `CONTRIBUTING.md`

This pass re-reviewed the local M1 branch after the Review 01 response, focusing on whether the
prior findings were actually fixed locally and whether the fixes introduced new contract gaps.

## Verification

Commands run from `/Users/dpickem/workspace/loopcraft`:

```text
make test && make compile && make validate
make check
PYTHONPATH=src python -m loopcraft.cli deps check --loop slack-triage
```

Result:

- `make test`: passed, 65 tests.
- `make compile`: passed.
- `make validate`: passed, 1 manifest.
- `make check`: passed.
- `loopctl deps check --loop slack-triage`: returned 1 with the expected loop-specific preflight
  failure, though in this environment the reported problem was `could not run 'nv-tools health'`
  rather than a more detailed nv-tools health result.

## Findings

### 1. Repeated runs can pass by reusing stale ledger outputs

Relevant files:

- `src/loopcraft/runners/codex.py`
- `src/loopcraft/cli.py`
- `loops/slack-triage.yaml`
- `tests/test_runner.py`
- `tests/test_cli.py`

Current state:

- Review 01 finding 1 moved `state/slack/seen.json` into the manifest as both an input and output.
- `CodexRunner.run()` still decides whether outputs were produced by checking `p.exists()` after
  Codex exits.
- On a repeated run, both `state/slack/triage-latest.md` and `state/slack/seen.json` may already
  exist from a prior successful run. If Codex exits 0 without rewriting either file, the runner
  marks the run `done` because no declared output is missing.
- Current tests only cover a fresh memory tree where the stub writes both files.

Why this matters:

The M1 exit criterion says `make run LOOP=slack-triage` writes the digest and a run record to the
memory tree. The Slack cursor fix also says `seen.json` must be updated. Existence-only checks do
not prove either write happened for the current run, and stale cursor state can cause the next
triage window to repeat old messages or miss new ones.

Recommended fix:

- Capture each resolved output's pre-run existence, size, and modification time before invoking
  the runner.
- After a successful subprocess exit, require every declared output to either be newly created or
  modified at or after the run start time.
- Consider treating cursor outputs specially if byte-identical rewrites are valid: require a fresh
  mtime, or have the agent write a small run timestamp/checkpoint field in `seen.json`.
- Add a regression test where both declared outputs already exist, the fake Codex process returns
  0 without touching them, and the result is not `done`.

### 2. `logic.skill` paths are staged without source-boundary validation

Relevant files:

- `src/loopcraft/worktree.py`
- `src/loopcraft/runners/codex.py`
- `src/loopcraft/manifest.py`
- `tests/test_worktree.py`
- `tests/test_manifest.py`

Current state:

- `stage_loop_assets()` turns `manifest.logic.skill` into `Path(skill)`, copies its parent
  directory from `config.source_path`, and writes it under the run workdir.
- Manifest validation checks that `logic.skill` exists only indirectly during Codex preflight; it
  does not reject absolute paths, `..` traversal, or scheme-style skill references.
- A malformed skill such as `../outside/SKILL.md` or an absolute path can escape the source tree
  during lookup, and can also escape the intended workdir layout when joined to `workdir`.

Why this matters:

The Review 01 response correctly stages the Slack skill directory so `channels.txt` is available,
but the staging path is now a second path boundary that needs the same kind of care as ledger
paths. `CONTRIBUTING.md` requires validating file paths before writing outside a worktree or the
memory tree, and M1 relies on source and memory staying separate.

Recommended fix:

- Add a `safe_source_relpath()` helper, or a skill-specific validator, that requires
  `logic.skill` to be a non-empty source-relative path with no absolute root, drive/UNC prefix,
  scheme head, or `..` segment.
- Use it in `LoopManifest.validate()`, `CodexRunner.preflight()`, and `stage_loop_assets()`.
- Add tests for absolute and traversing `logic.skill` values, plus a staging test that confirms
  all staged destinations remain under `workdir`.

### 3. Scheme-style outputs are accepted by validation but still crash at run resolution

Relevant files:

- `src/loopcraft/config.py`
- `src/loopcraft/manifest.py`
- `src/loopcraft/cli.py`
- `tests/test_manifest.py`

Current state:

- `LoopManifest.validate()` skips path validation for non-state scheme targets such as
  `linear:project/Daily`.
- `_cmd_run()` resolves every manifest output with `config.resolve_state_path(o)` before the
  runner starts.
- `resolve_state_path()` rejects `:` segments, so a manifest output that validation treats as
  allowed will fail at run time.

Why this matters:

This is not blocking the current `slack-triage` loop, whose outputs are ledger files. It is still
an internally inconsistent contract introduced by the path-validation fix: a manifest can pass
`make validate` and then fail before running because the CLI assumes every output is a ledger
output.

Recommended fix:

- For M1, either reject non-state outputs until an external sink/artifact abstraction exists, or
  keep separate `state_outputs` and external `outputs` so `_cmd_run()` resolves only ledger-owned
  paths.
- Add a CLI-level regression test for a scheme-style output so validation and run behavior agree.

## Notes On Resolved/Healthy Areas

- The Review 01 cursor contract is now explicit in `loops/slack-triage.yaml`.
- The Codex preflight path now covers declared auth/API probes, declared tools, env vars, skill
  presence, and broad model mismatch detection.
- Runtime timeout handling now returns a normalized `stalled` result and writes a timeout log.
- Ledger state paths are now guarded at validation and write time.
