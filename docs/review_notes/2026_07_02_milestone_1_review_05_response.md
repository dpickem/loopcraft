# Milestone 1 Review 05 — Response

Response to [`2026_07_02_milestone_1_review_05_report.md`](2026_07_02_milestone_1_review_05_report.md),
the whole-branch follow-up review of `feat/m1-control-plane` after the Review 04
fixes. Prior history:
[01 report](2026_06_30_milestone_1_review_01_report.md) /
[01 response](2026_06_30_milestone_1_review_01_response.md),
[02 report](2026_06_30_milestone_1_review_02_report.md) /
[02 response](2026_06_30_milestone_1_review_02_response.md),
[03 report](2026_06_30_milestone_1_review_03_report.md) /
[03 response](2026_06_30_milestone_1_review_03_response.md),
[04 report](2026_07_02_milestone_1_review_04_report.md) /
[04 response](2026_07_02_milestone_1_review_04_response.md).

All four findings are addressed, each with regression tests.

## Commit

The fixes are in one commit on `feat/m1-control-plane`:

```text
a4b2009 fix: address M1 review 05 findings
```

Inspect with `git show a4b2009 -- <file>` using the file lists below.

## Status

Verification from the repo root:

```text
make test && make compile && make validate
```

- `make test`: **149 passed** (up from 114 at review time), fully offline.
- `make compile`: passes.
- `make validate`: passes, 3 manifests.
- `loopctl deps check` / `apply --dry-run` (`make check`): passes in this
  environment.

The review's focused diagnostic (an otherwise-valid manifest with an absolute
`id` and a missing `content.config`) now fails at every layer instead of
resolving a worktree to `/tmp/loopcraft-escaped/run-id`:

```text
manifest_problems= ["id: must be lowercase alphanumeric components separated by
                     single hyphens (e.g. 'slack-triage'): '/tmp/loopcraft-escaped'"]
preflight_asset_problems= [..., 'content config not found: config/does-not-exist.yaml
                                 (no public file or *.local.* override)']
worktree= REFUSED: run worktree escapes allowed root: /tmp/loopcraft-escaped/run-id
```

## Findings

### 1. Loop identifiers can escape the loops directory and worktree root — fixed

Loop ids were unrestricted strings interpolated into paths by `_find_manifest()`
and `_worktree_dir()`, and the manifest id was never required to match its
filename.

- A canonical loop-id vocabulary is now defined and enforced:
  `LOOP_ID_RE = ^[a-z0-9]+(?:-[a-z0-9]+)*$` (lowercase alphanumeric components
  separated by single hyphens), with `loop_id_problem()` as the shared
  validator. `LoopManifest.validation_report()` rejects any non-canonical id.
- `_find_manifest()` validates the CLI loop selector against the same
  vocabulary **before** constructing any path, confirms the candidate path
  stays under `config.loops_dir` (defense in depth via `assert_under`), and
  raises `ManifestError` when the loaded manifest's `id` differs from the
  filename stem it was looked up by — so `list` can never advertise one id
  while `run` executes another.
- `load_all()` reports an `id != filename stem` mismatch as a validation
  problem, so `validate`/`apply` catch it fleet-wide.
- `_worktree_dir()` and `_prune_loop_worktrees()` now verify the computed
  per-loop path remains under `<memory>/var/worktrees` before creating or
  pruning anything, so unchecked manifest data can never choose (or delete) an
  arbitrary directory even if a future code path skips id validation.
- Files: `src/loopcraft/manifest.py` (`LOOP_ID_RE`, `loop_id_problem`,
  validation + `load_all` stem check), `src/loopcraft/cli.py`
  (`_find_manifest`, `_worktrees_root`, `_worktree_dir`,
  `_prune_loop_worktrees`).
- Tests: `tests/test_manifest.py::test_non_canonical_loop_ids_are_rejected`
  (absolute ids, `..` ids, separators, uppercase, double/leading/trailing
  hyphen, underscore), `::test_canonical_loop_ids_are_accepted`,
  `::test_load_all_reports_filename_id_mismatch`;
  `tests/test_cli.py::test_run_rejects_non_canonical_loop_ids` (absolute and
  traversing CLI arguments), `::test_run_rejects_filename_id_mismatch`,
  `::test_worktree_dir_and_prune_reject_escaping_ids` (escaping loop ids and an
  absolute run id against both create and prune paths).

### 2. A missing declared `content.config` now fails preflight and staging — fixed

Validation only checked path safety, runtime-neutral preflight ignored
`content.config`, and `_stage_content_config()` silently skipped a nonexistent
file, so the headless agent discovered the missing dependency mid-run.

- The shared runtime-neutral preflight (`check_declared_capabilities`) now
  requires a declared `content.config` to exist as a regular file. Matching the
  staging semantics of the public/private split, either the declared public
  file or its gitignored `*.local.*` override satisfies the dependency; a path
  resolving to a directory is reported as its own problem. The skill/verify
  asset check also gained the regular-file requirement.
- Staging raises a new `StagingError` ("content.config not found: ... (no
  public file or *.local.* override)") instead of silently omitting the config,
  and refuses a config that is not a regular file. A local-only override still
  stages correctly (the shadowing pass materializes it under the public name).
- `loopctl run` catches `StagingError`/`SourcePathError` from staging and
  records a structured failed run (phase `staging`) rather than letting the
  exception escape — a backstop for adapters whose preflight does not use the
  shared check.
- The sibling-path computation is now shared: `local_sibling_path()` in
  `loopcraft.settings` is used by preflight, staging, and `local_override_path`,
  so all three agree on what an override is.
- Files: `src/loopcraft/runners/capabilities.py` (`_check_content_config`,
  `_check_source_asset`), `src/loopcraft/worktree.py` (`StagingError`,
  `_stage_content_config`), `src/loopcraft/settings.py`
  (`local_sibling_path`), `src/loopcraft/cli.py` (`_run_execute` staging
  guard).
- Tests: `tests/test_capabilities.py::test_capabilities_flag_missing_content_config`,
  `::test_capabilities_flag_directory_content_config`,
  `::test_capabilities_accept_existing_content_config`,
  `::test_capabilities_accept_local_only_content_config`,
  `::test_capabilities_flag_escaping_content_config`;
  `tests/test_worktree.py::test_stage_missing_content_config_raises`,
  `::test_stage_directory_content_config_raises`,
  `::test_stage_local_only_content_config_stages_override`;
  `tests/test_cli.py::test_run_fails_structured_when_content_config_missing`
  (end-to-end: passing preflight, failing staging, structured envelope, failed
  run record with no outputs).

### 3. Invalid manifests are structured failures in `run` and degrade `list`/`status` — fixed

`_find_manifest()` let `ManifestError` escape as a traceback (including under
`--json`), while `list` and `status` discarded `load_all()` problems and always
reported success.

- `_cmd_run` and `_preflight_loop` (`deps check --loop`) catch `ManifestError`
  from lookup — malformed YAML, Pydantic schema errors, invalid loop ids, and
  filename/id mismatches all emit the standard `{command, ok, exit_code, data}`
  envelope (or text error line) with exit code 2.
- `_cmd_list` and `_cmd_status` now preserve `load_all()` problems: every
  loadable loop is still reported as partial data, `data.problems` carries the
  issues, `ok` is false, and the exit code is 1, so a broken manifest degrades
  the fleet view loudly instead of vanishing from it. Text mode prints the
  problems to stderr, mirroring `run`'s problem output.
- `validate`/`apply` behavior is unchanged; all four commands now agree.
- Files: `src/loopcraft/cli.py` (`_cmd_run`, `_preflight_loop`, `_cmd_list`,
  `_cmd_status`), `src/loopcraft/manifest.py` (removed the stale
  `pragma: no cover` on the YAML-error path, now exercised by tests).
- Tests (`tests/test_cli.py`):
  `test_run_reports_malformed_yaml_as_structured_error`,
  `test_run_reports_schema_invalid_manifest_as_structured_error` (both assert a
  parseable JSON envelope), and `test_list_and_status_surface_broken_manifests`
  (rc 1, problems surfaced, valid loop still listed for both commands).

### 4. Failed pre-execution runs no longer claim declared outputs as produced — fixed

`_record_preflight_failure()` stored `manifest.outputs` in the produced-output
field even though execution never started.

- The failure recorder (now `_record_run_failure`, shared by the preflight and
  staging failure paths) records `outputs=[]` and preserves the contract in a
  new, separately named `RunRecord.declared_outputs` field. Successful runs
  populate the same field, so success and failure records share one semantic
  contract: `inputs`/`declared_outputs` are the manifest's declared I/O
  contract at run time, `outputs` is provenance — only files the run actually
  produced or refreshed. The `RunRecord` docstring spells this out explicitly,
  answering the report's `inputs` naming question by documenting it as the
  declared contract in both record shapes.
- The failure envelope now names the failed phase (`preflight` or `staging`)
  alongside the problems.
- Files: `src/loopcraft/store.py` (`RunRecord.declared_outputs`, field
  semantics), `src/loopcraft/cli.py` (`_record_run_failure`, success-record
  `declared_outputs`).
- Tests (`tests/test_cli.py`): `test_run_records_failure_on_preflight` extended
  to assert `outputs == []` while `declared_outputs` retains the contract;
  `test_run_end_to_end_with_stub_runner` asserts the produced/declared split on
  a successful record;
  `test_run_fails_structured_when_content_config_missing` asserts the same for
  a staging failure.

## Notes on healthy areas

Review 05 confirmed all five Review 04 fixes are present and that the offline
suite, compile check, manifest validation, and environment dependency check
pass. Previously acknowledged deferrals remain unchanged and are not re-argued
here: the staged asset bundle is not yet a real git worktree, token/cost plus
max-turn/max-token enforcement remain adapter follow-ups, and the live Slack
exit-criteria run still requires a real-data environment.
