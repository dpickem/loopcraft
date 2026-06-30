# Milestone 1 Review 01 — Response

Response to [`2026_06_30_milestone_1_review_01_report.md`](2026_06_30_milestone_1_review_01_report.md)
(the Loopcraft M1 control-plane review).

All five findings are addressed below. Each fix has at least one regression test that
would have failed before the change.

## Commit

The M1 code was reviewed before its first commit, so all five fixes are folded into the M1
implementation commit on `main`:

```text
9a0f311 feat: add M1 loopcraft control plane
```

The related shell-script cleanup noted under healthy areas is:

```text
86c6815 chore: replace daily-intel shell scripts with Makefile targets
```

Inspect a finding's fix with `git show 9a0f311 -- <file>` using the file lists below.

## Status

Changes are in the local working tree of `/Users/dpickem/workspace/loopcraft` (M1 is not yet
committed). Verification from the repo root:

```text
make test && make compile && make validate
```

- `make test`: **65 passed** (up from 38 at review time), fully offline.
- `make compile`: passes.
- `make validate`: passes, 1 manifest.

New behavior was also exercised manually:

```text
loopctl deps check --loop slack-triage
# ... preflight slack-triage (codex): PROBLEMS
#   - auth bundle 'nv-tools': 'nv-tools health' reported a problem (run it for details)
```

This is the intended outcome: the declared `auth: [nv-tools]` dependency is now probed and a
broken setup is reported before any headless run starts.

## Findings

### 1. Slack cursor state now in the manifest contract — fixed

`state/slack/seen.json` is the durable cursor the skill reads at the start of a run and rewrites
at the end, so it is now an explicit part of the I/O contract rather than skill-only lore.

- `loops/slack-triage.yaml` declares `state/slack/seen.json` as both an `inputs` cursor and an
  `outputs` cursor, and `state/slack/triage-latest.md` remains the user-facing exit-criteria
  output. The `logic.verify` line now also names the cursor update.
- Cycle detection already treats a producer that is its own consumer as a self-edge, not a cycle,
  so declaring the same path as input and output is safe.
- Files: `loops/slack-triage.yaml`.
- Tests: `tests/test_manifest.py::test_slack_triage_declares_seen_cursor`,
  `::test_self_cursor_is_not_a_cycle`; `tests/test_cli.py::test_run_end_to_end_with_stub_runner`
  now asserts both `triage-latest.md` and `seen.json` are produced and that the cursor appears in
  the run record's `outputs`.

### 2. Codex preflight validates declared auth / APIs / model — fixed

`CodexRunner.preflight()` previously ignored `depends_on.apis`, `depends_on.auth`, and the model.
It now consults all three through an injectable probe layer, and `loopctl deps check` can target a
specific loop.

- New probe tables in `src/loopcraft/runners/codex.py`: `AUTH_PROBES` and `API_PROBES`
  (name → callable returning a problem string or `None`). They are module-level and injectable so
  tests and later milestones can substitute them.
  - `auth: [nv-tools]` runs a bounded, read-only `nv-tools health` probe (offline-safe, no remote
    mutation) and reports an actionable error if the CLI is missing or unhealthy.
  - `apis: [slack]` requires the nv-tools connector that provides Slack.
  - A declared auth bundle or API with no registered probe is reported rather than silently
    accepted.
- Model support: `_probe_codex_model()` flags a model id that does not look like a Codex/OpenAI
  model (prefix allowlist), so an obviously wrong binding such as `opus` on the Codex adapter is
  caught locally. The check is deliberately broad to avoid false negatives on new model names.
- `loopctl deps check --loop <id>` now runs the selected loop's adapter preflight and prints the
  per-dependency result (`src/loopcraft/cli.py`, `_preflight_loop`).
- Files: `src/loopcraft/runners/codex.py`, `src/loopcraft/cli.py`.
- Tests (`tests/test_runner.py`): `test_preflight_unknown_auth_bundle_reported`,
  `test_preflight_auth_probe_failure_reported`, `test_preflight_api_probe_failure_reported`,
  `test_preflight_passes_when_probes_ok`, `test_preflight_flags_unrecognized_model`;
  `tests/test_cli.py::test_deps_check_loop_runs_preflight`.

### 3. Run worktree now contains the loop's source assets — fixed

`loopctl run` still executes in an isolated per-run directory, but that directory is now seeded
with a minimal source bundle so the loop's relative asset references resolve.

- New `src/loopcraft/worktree.py::stage_loop_assets()` copies the skill directory (preserving its
  source-relative path, e.g. `skills/slack-triage/`) and the manifest into the run directory before
  the adapter runs. The Slack skill's `skills/slack-triage/channels.txt` is therefore present
  relative to the working directory.
- `_cmd_run` calls `stage_loop_assets(...)` right after creating the worktree
  (`src/loopcraft/cli.py`).
- `--skip-git-repo-check` is kept intentionally: the run directory is an asset bundle, not a git
  checkout. The rationale is now documented in `_build_command` in
  `src/loopcraft/runners/codex.py`. (Promoting the bundle to a real `git worktree` is a reasonable
  M2 follow-up once deployment exists.)
- Files: `src/loopcraft/worktree.py`, `src/loopcraft/cli.py`,
  `src/loopcraft/runners/codex.py`.
- Tests: `tests/test_worktree.py::test_stage_loop_assets_copies_skill_dir_and_manifest`;
  `tests/test_cli.py::test_run_end_to_end_with_stub_runner` asserts `channels.txt` is staged into
  the run worktree.

### 4. Ledger path resolution can no longer escape the memory tree — fixed

A shared validator now guards every manifest-declared state path, at both validation and write
time.

- New `safe_state_relpath()` and `StatePathError` in `src/loopcraft/config.py`: strips an optional
  `state/` or `ledger/` prefix and rejects empty paths, absolute paths, drive/UNC heads, and any
  `..` traversal. `is_state_path()` exempts scheme targets such as `linear:project/Daily` from
  path validation.
- `LoopcraftConfig.resolve_state_path()` routes through the validator and additionally confirms the
  normalized result stays inside `ledger_dir` (defense in depth). `Store.write_state()`,
  `read_state()`, `state_exists()`, and `append_jsonl()` inherit this because they call
  `resolve_state_path()`.
- `LoopManifest.validate()` now reports unsafe `inputs`/`outputs`, so a bad path fails
  `make validate` rather than at write time.
- Files: `src/loopcraft/config.py`, `src/loopcraft/manifest.py` (and `store.py` via the shared
  resolver).
- Tests: `tests/test_store.py::test_resolve_state_path_rejects_escapes`,
  `::test_store_write_rejects_escaping_path`, `::test_safe_state_relpath_strips_prefixes`;
  `tests/test_manifest.py::test_unsafe_state_paths_are_reported`,
  `::test_scheme_targets_are_not_validated_as_paths`.

### 5. Runtime budget is now enforced — fixed

`budget.max_runtime` is parsed and applied as a hard subprocess timeout, and a timeout maps to a
normalized result.

- New `parse_duration()` in `src/loopcraft/manifest.py` parses `30s`/`10m`/`1h` to seconds, with a
  `Budget.max_runtime_s` property. Malformed durations are reported by `LoopManifest.validate()`.
- `CodexRunner.run()` passes `timeout=loop.budget.max_runtime_s` to `subprocess.run()`, catches
  `subprocess.TimeoutExpired`, writes the partial log, and returns a `stalled` `RunResult`
  (`exit_code=None`) with an explanatory problem. The CLI records that status in the run record.
- Files: `src/loopcraft/manifest.py`, `src/loopcraft/runners/codex.py`.
- Tests: `tests/test_runner.py::test_run_timeout_returns_stalled`,
  plus `tests/test_manifest.py` duration coverage (`test_parse_duration_valid`,
  `test_parse_duration_invalid_raises`, `test_budget_max_runtime_seconds`,
  `test_invalid_max_runtime_reported`).

## Notes on resolved/healthy areas

The report's healthy-area notes still hold: routine shell scripts remain replaced by Makefile
targets; all durable writes go through `loopcraft.store`; public structures are typed dataclasses;
and the test suite is offline by default (the new `nv-tools health` auth probe is only invoked by
adapter preflight, which the offline tests reach exclusively through injected probes).

## Deferred / follow-ups

- Promoting the per-run asset bundle to a real `git worktree` (so `--skip-git-repo-check` can be
  dropped) is left for M2, where host bootstrap and deployment land.
- Token/cost capture and `budget.max_tokens` / `max_turns` enforcement remain adapter follow-ups;
  M1 enforces wall-clock runtime only.
