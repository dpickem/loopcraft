# Milestone 1 Review 06 — Response

Response to [`2026_07_02_milestone_1_review_06_report.md`](2026_07_02_milestone_1_review_06_report.md),
the whole-branch follow-up review of `feat/m1-control-plane` after the Review 05
fixes. Prior history:
[01 report](2026_06_30_milestone_1_review_01_report.md) /
[01 response](2026_06_30_milestone_1_review_01_response.md),
[02 report](2026_06_30_milestone_1_review_02_report.md) /
[02 response](2026_06_30_milestone_1_review_02_response.md),
[03 report](2026_06_30_milestone_1_review_03_report.md) /
[03 response](2026_06_30_milestone_1_review_03_response.md),
[04 report](2026_07_02_milestone_1_review_04_report.md) /
[04 response](2026_07_02_milestone_1_review_04_response.md),
[05 report](2026_07_02_milestone_1_review_05_report.md) /
[05 response](2026_07_02_milestone_1_review_05_response.md).

All six findings are addressed, each with regression tests.

## Commit

The fixes are in one commit on `feat/m1-control-plane`:

```text
ecf18fe fix: address M1 review 06 findings
```

Inspect with `git show ecf18fe -- <file>` using the file lists below.

## Status

Verification from the repo root:

```text
make test && make compile && make validate
```

- `make test`: **165 passed** (up from 149 at review time), fully offline.
- `make compile`: passes.
- `make validate`: passes, 3 manifests.
- `loopctl deps check` (`make check`): passes in this environment.

Every focused diagnostic from the report now fails safely:

```text
symlinked source asset:   REFUSED: source path escapes allowed root: .../src/skills/demo/SKILL.md
ledger symlink write:     REFUSED: state path escapes allowed root: .../mem/ledger/demo/out.txt
                          (outside file NOT created)
runs_for corrupt record:  skipped ([] instead of an uncaught ValidationError)
x output override:        REFUSED: content config must not override 'output' paths; durable
                          outputs are fixed by the loop manifest (loops/x-intel.yaml)
deps check --loop (sink): rc 2 with invalid_manifest (regression test)
staging fail, keep_last=0: worktree pruned, failed run still recorded (regression test)
```

## Findings

### 1. Symlink bypass of lexical containment — fixed

`assert_under()` compared `os.path.normpath()` strings, so an in-tree symlink
(preserved by git, or planted in the memory tree) could redirect source reads,
ledger writes, staging, or pruning outside the allowed root.

- `assert_under()` now fully resolves both the root and the candidate
  (`Path.resolve`), which follows symlinks in every existing component and
  normalizes the nonexistent tail. Every existing containment call site —
  `resolve_source_path()`, `resolve_state_path()` (and therefore all
  `Store.write_state`/`append_jsonl` writes), staged-asset destinations, the
  loop-manifest lookup, and worktree create/prune — inherits the real-path
  semantics through the one shared helper.
- Source assets *may* be symlinks, with the policy the report offered: the
  resolved target must itself remain under the resolved source root. The same
  rule applies inside the ledger and the worktree area, which also answers the
  "reject symlinked parents" minimum — a symlinked parent is followed and then
  either contained or refused. Full TOCTOU hardening (symlink replacement
  between check and use, e.g. `O_NOFOLLOW` opens) is out of M1 scope and
  noted as a deliberate limitation rather than attempted halfway.
- Files: `src/loopcraft/paths.py` (single change point; `config.py`,
  `worktree.py`, `cli.py`, and `store.py` flow through it).
- Tests: `tests/test_worktree.py::test_stage_rejects_symlinked_skill_dir_escaping_source`
  and `::test_stage_allows_symlink_resolving_inside_source` (both directions of
  the policy); `tests/test_store.py::test_store_write_rejects_symlinked_ledger_parent`
  (asserts the outside file is not created) and
  `::test_ledger_symlink_resolving_inside_memory_tree_is_allowed`;
  `tests/test_cli.py::test_worktree_dir_and_prune_reject_symlinked_loop_dir`
  (create and prune).

### 2. Run lifecycle loses records/retention on exceptional paths — fixed

Preflight and `runner.run()` had no exception boundary (an adapter fault
escaped as a traceback with no run record), and the Review 05 staging-failure
path skipped worktree pruning.

- `_cmd_run()` wraps `runner.preflight()`: an ordinary exception is normalized
  into a failing `PreflightReport` (`preflight raised <Type>: <msg>`), which
  then flows through the existing dry-run/failed-record paths.
- `_run_execute()` now has one explicit finalization boundary: worktree
  creation + staging failures (`StagingError`/`ValueError`/`OSError`, which
  also covers the new symlink-containment refusals) and unexpected
  `runner.run()` exceptions are recorded as normalized failed runs via
  `_record_run_failure` (phases `staging` / `execution`). For an execution
  fault the full traceback is written to the run log and the record's
  `log_path` points at it. Process-control exceptions
  (`KeyboardInterrupt`/`SystemExit`) are `BaseException` and deliberately not
  caught.
- Retention pruning moved into a `finally` that runs whenever a worktree was
  created — staging failures, execution faults, and successes alike — so
  `worktree_keep_last` (including `0`) is honored on every path; pruning itself
  is best-effort (an `OSError` becomes a stderr warning, never a lost record).
- Files: `src/loopcraft/cli.py` (`_cmd_run`, `_run_execute`,
  `_record_run_failure` with `phase`/`log_path`).
- Tests (`tests/test_cli.py`): `test_run_normalizes_preflight_exception`,
  `test_run_normalizes_runner_exception` (asserts the failed record and the
  traceback in the run log), `test_staging_failure_honors_worktree_retention`
  (`keep_last=0` leaves no worktree while the failed run record persists).

### 3. Content configs could redirect writes away from the manifest contract — fixed

Both research content models exposed an `output` object that directly
controlled ledger writes, untied to the manifests' declared outputs.

- Durable output locations are removed from the content-definition surface
  (the report's first recommended option). `OutputPaths` remains in code as the
  single fixed write set mirroring the manifest contract; `ArxivIntelConfig` and
  `IntelConfig` no longer carry an `output` field, and both direct CLIs
  construct `OutputPaths()` themselves. `from_dict()` rejects an `output` key
  with an explicit error naming the owning manifest — so a public *or*
  gitignored local override that tries to redirect writes fails fast on every
  path (control-plane staging, `make`, or standalone direct CLI) instead of
  silently writing undeclared files.
- The manifests now declare the workflow's *complete* write set: the dated
  digest files (`state/research/<loop>/digests/{{date}}.md/.json`) that
  `write_digest()` has always produced are declared outputs of both loops, so
  the control plane creates, freshness-checks, and records them like the rest.
  (`follow_candidates_dir` stays out of the `run` contract: it belongs to the
  separate operator-invoked `discover-follows` command, not the scheduled
  loop.)
- Parity between the fixed write set and the manifests is enforced by tests,
  so the duplicated defaults can no longer drift silently.
- Files: `src/loopcraft/research_intel/arxiv/config.py`,
  `src/loopcraft/research_intel/arxiv/cli.py`,
  `src/loopcraft/research_intel/x/config.py`,
  `src/loopcraft/research_intel/x/cli.py`, `loops/arxiv-intel.yaml`,
  `loops/x-intel.yaml`.
- Tests: `tests/test_x_intel.py::test_x_config_rejects_output_override`
  (public and local), `::test_x_fixed_outputs_match_manifest_contract`, and
  `test_x_run_writes_declared_outputs_with_run_id` extended to resolve **every**
  declared manifest output; `tests/test_arxiv_intel.py` mirrors all three
  (`test_arxiv_config_rejects_output_override`,
  `test_arxiv_fixed_outputs_match_manifest_contract`, and the control-plane
  run-id test now resolving the dated digests too).

### 4. `deps check --loop` reported OK for semantically invalid manifests — fixed

`_preflight_loop()` skipped `manifest.validate()`, so a loop that `run` would
immediately reject could still preflight OK.

- A single shared helper, `_lookup_loop()`, now owns the full
  `find + filename/id check + parse/schema + manifest.validate()` sequence and
  returns either the manifest or a structured `(rc, data, lines)` failure.
  `_cmd_run()` and `_preflight_loop()` (the `deps check --loop` path) both use
  it, so the two commands cannot disagree about readiness by construction.
- Custom validation failures surface as a structured nonzero
  `invalid_manifest` result (exit code 2) before the adapter is ever invoked,
  in both text and JSON modes.
- Files: `src/loopcraft/cli.py` (`_lookup_loop`, `_cmd_run`,
  `_preflight_loop`).
- Test (`tests/test_cli.py`):
  `test_deps_check_loop_reports_semantically_invalid_manifest` — a
  Pydantic-valid manifest with an unsupported external sink returns rc 2 with
  `invalid_manifest` in the JSON envelope instead of `OK`.

### 5. A schema-invalid run-history file crashed `status`/`logs` — fixed

`Store.runs_for()` skipped malformed JSON but let Pydantic `ValidationError`
escape for valid JSON with missing/mistyped `RunRecord` fields.

- `runs_for()` now skips schema-invalid records (`ValidationError`) exactly
  like unreadable ones, and also tolerates valid JSON that is not an object.
  The docstring states the durability rationale: the ledger outlives schema
  revisions, so one bad file must never take down history access for a loop.
- Run-record writes are atomic (`.tmp` + `Path.replace`), shrinking the window
  for partially written durable records; the `.tmp` name cannot match the
  `*.json` reader glob.
- Surfacing skipped-record paths as structured CLI problems was considered and
  deferred: it changes the `runs_for()` return contract for every caller, and
  with consistent skipping in place the fleet views stay correct. It can ride
  along with the M4 harvester work that reindexes these records anyway.
- Files: `src/loopcraft/store.py`.
- Tests: `tests/test_store.py::test_runs_for_skips_schema_invalid_records`
  (wrong types, missing fields, non-object JSON, mixed with a valid record) and
  `::test_record_run_write_is_atomic`;
  `tests/test_cli.py::test_status_survives_corrupt_run_record`.

### 6. Local-only `content.config` contradicted the public/private pattern — fixed

Review 05 accepted a gitignored `*.local.*` sibling as a valid effective config
with no committed public file, diverging from `CONTRIBUTING.md`'s pattern.

- The behavior is reversed to match the documented contract: the declared
  public `content.config` must exist as a committed regular source file, in
  both the shared runtime-neutral preflight and staging. A `*.local.*` sibling
  still shadows the public file's *values* at staging time, but never replaces
  its existence. The shipped loops already satisfy this (both public configs
  are committed with safe placeholder/documented values).
- The preflight check reuses the same `_check_source_asset` helper as
  skill/verify (one noun map), so all three declared source assets now share
  identical existence/regular-file semantics.
- The Review 05 local-only acceptance tests are replaced with tests asserting
  a clear preflight problem and a clear `StagingError` when the public file is
  absent.
- Files: `src/loopcraft/runners/capabilities.py`, `src/loopcraft/worktree.py`.
- Tests: `tests/test_capabilities.py::test_capabilities_reject_local_only_content_config`;
  `tests/test_worktree.py::test_stage_local_only_content_config_raises`. The
  positive shadowing path (public + local) remains covered by
  `test_stage_loop_assets_stages_content_config_with_local_override`.

## Notes on healthy areas

Review 06 confirmed all four Review 05 fixes are substantively in place and
that the offline suite, compile check, manifest validation, and environment
dependency check pass. Previously acknowledged deferrals remain unchanged and
are not re-argued here: the staged asset bundle is not yet a real git worktree,
token/cost plus max-turn/max-token enforcement remain adapter follow-ups, and
the live Slack exit-criteria run still requires a real-data environment. Full
TOCTOU symlink-race hardening (beyond resolve-at-check containment) is noted
under Finding 1 as an explicit M1 limitation.
