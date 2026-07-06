# Milestone 1 Review 06 — Report (branch implementation follow-up)

Review target: local branch `feat/m1-control-plane` in
`/Users/dpickem/workspace/loopcraft`.

Reviewed state:

- HEAD: `2f56929 docs: add M1 review 05 report and response`
- Review 05 implementation commit: `a4b2009 fix: address M1 review 05 findings`

Reference scope:

- M1 build plan and control-plane contracts in
  `docs/loopcraft-implementation-design.html`
- `CONTRIBUTING.md`
- Review 05 response in
  `docs/review_notes/2026_07_02_milestone_1_review_05_response.md`
- Prior Review 01–05 reports and responses, to avoid repeating acknowledged
  deferrals or already-fixed findings

This pass reviewed the branch as a whole after the Review 05 fixes. The four
Review 05 findings are addressed as described in its response. The findings
below are additional boundary, lifecycle, and contract issues not covered by
the earlier reports.

## Verification

Commands run from `/Users/dpickem/workspace/loopcraft`:

```text
make test && make compile && make validate && make check
```

Result:

- `make test`: passed, 149 tests.
- `make compile`: passed.
- `make validate`: passed, 3 manifests.
- `make check`: passed in this environment.

Focused diagnostics reproduced the following behaviors outside the current
suite:

- A source asset reached through an in-tree symlink resolved outside the source
  root, and `Store.write_state()` followed an in-ledger symlink and wrote a file
  outside the memory tree.
- `deps check --loop` returned `OK` for a manifest whose output was an
  unsupported M1 external sink; `manifest.validate()` rejected the same file.
- A staging failure left one worktree behind even with
  `LOOPCRAFT_WORKTREE_KEEP_LAST=0`.
- A valid-JSON run record with the matching loop id but an invalid/missing
  `RunRecord` schema raised an uncaught Pydantic `ValidationError` from
  `Store.runs_for()`.
- Both research content models accepted output paths that were absent from the
  corresponding manifest's declared outputs.

The live Slack exit-criteria run was not executed because it would access real
external data. The offline suite continues to use a stub adapter for the
end-to-end control-plane path.

## Findings

### 1. Lexical containment checks can be bypassed with symlinks

Relevant files:

- `src/loopcraft/paths.py`
- `src/loopcraft/config.py`
- `src/loopcraft/worktree.py`
- `src/loopcraft/cli.py`
- `tests/test_store.py`
- `tests/test_worktree.py`

Current state:

- `assert_under()` compares `os.path.normpath()` strings. It rejects `..` and
  absolute-path escapes but does not resolve symlinks in either the allowed root
  or the candidate path.
- `resolve_source_path()`, `resolve_state_path()`, staged-asset destinations,
  and worktree create/prune containment all rely on this lexical check.
- A path such as `skills/demo/SKILL.md` passes when `skills/demo` is a symlink to
  a directory outside the source tree; staging then follows the link and copies
  the outside file.
- Likewise, if `<memory>/ledger/demo` is a symlink to an outside directory,
  `Store.write_state("state/demo/out.txt", ...)` writes through it. The focused
  diagnostic confirmed the outside file was created.

Why this matters:

The Review 01/02 path fixes and current module docstrings promise that source
reads, ledger writes, and staging cannot escape their roots. Git preserves
symlinks, so a malicious or accidental source-tree symlink is enough to cross
the boundary without using a syntactically unsafe manifest path. A ledger or
worktree symlink can similarly redirect writes or pruning.

Recommended fix:

- Make containment checks compare real paths (`Path.resolve(strict=False)` or
  equivalent), resolving the root and every existing parent of the candidate.
- For write/delete paths, guard against symlink replacement between validation
  and use where practical; at minimum explicitly reject symlinked parents below
  the ledger/worktree roots.
- Decide whether source assets may themselves be symlinks. If allowed, require
  their resolved targets to remain under the resolved source root; otherwise
  reject them explicitly.
- Add regression tests for symlinked source assets, ledger parents, worktree
  parents, and prune targets.

### 2. The run lifecycle still loses records and retention guarantees on exceptional paths

Relevant files:

- `src/loopcraft/cli.py`
- `src/loopcraft/runners/base.py`
- `tests/test_cli.py`

Current state:

- `_cmd_run()` calls `runner.preflight()` without an exception boundary.
- `_run_execute()` calls `runner.run()` without an exception boundary. An
  unexpected adapter/build-command/subprocess/filesystem exception escapes as a
  traceback, writes no normalized failed `RunRecord`, and skips worktree
  pruning.
- The new structured staging-failure path creates the run worktree before
  staging, then returns through `_record_run_failure()` without pruning. A
  focused run with `worktree_keep_last=0` still left the failed worktree on
  disk.
- Because staging copies the skill directory before it reaches
  `content.config`, a partially staged failure directory can include resolved
  private assets and remain outside the configured retention policy.

Why this matters:

M1's core adapter/store contract is that headless execution returns a normalized
status and records the run. A single unanticipated adapter exception currently
makes the attempt disappear from durable history. Separately, the documented
worktree retention setting is not honored for the staging-failure path added by
the Review 05 fix.

Recommended fix:

- Put preflight, staging, execution, record writing, and pruning behind one
  explicit lifecycle/finalization boundary.
- Normalize ordinary adapter/runtime exceptions to a failed result/run record,
  preserving an error log or traceback for diagnosis; do not catch process
  control exceptions such as `KeyboardInterrupt`/`SystemExit`.
- Run retention cleanup in `finally` whenever a worktree was created, including
  staging and execution failures.
- Add regression tests for a preflight exception, a runner exception, and a
  staging failure with `keep_last=0`.

### 3. Research content configs can redirect writes away from the manifest I/O contract

Relevant files:

- `src/loopcraft/research_intel/arxiv/config.py`
- `src/loopcraft/research_intel/arxiv/store.py`
- `src/loopcraft/research_intel/x/config.py`
- `src/loopcraft/research_intel/x/store.py`
- `loops/arxiv-intel.yaml`
- `loops/x-intel.yaml`
- `tests/test_arxiv_intel.py`
- `tests/test_x_intel.py`

Current state:

- Both content models expose an `output` object whose paths directly control
  ledger writes made by the deterministic Python workflow.
- The manifests separately declare the authoritative outputs that the control
  plane resolves, grants as writable roots, checks for freshness, and records.
- No validation ties the two contracts together. A public or local content
  config can set, for example, arXiv `latest_markdown` to
  `state/research/arxiv/custom.md` or X `latest_json` to
  `state/research/x/custom.json`; both models accept those values even though
  neither path is declared by the corresponding manifest.
- Existing run-output tests use the default content model and therefore prove
  parity only while the duplicated defaults remain unchanged.

Why this matters:

The design makes the manifest's inputs/outputs the source of truth for
isolation, ordering, provenance, and dependency inference. Allowing a content
definition—especially a gitignored override—to redirect durable writes hides an
I/O dependency outside the manifest. The workflow can write undeclared files
and the runner then fails because the declared paths were not refreshed.

Recommended fix:

- Remove durable output locations from content config for control-plane runs;
  inject the manifest-resolved paths into the direct workflow instead.
- If standalone direct CLIs must retain configurable outputs, separate that
  mode explicitly and reject/ignore output overrides whenever
  `LOOPCRAFT_RUN_ID` indicates a control-plane run.
- Alternatively, add a preflight parity check that expands the direct
  workflow's complete output set and requires exact agreement with the
  manifest, including run-id templates.
- Add regression tests with customized public and local output settings proving
  that a control-plane run still writes only and exactly the manifest outputs.

### 4. `deps check --loop` can report OK for a semantically invalid manifest

Relevant files:

- `src/loopcraft/cli.py`
- `src/loopcraft/manifest.py`
- `tests/test_cli.py`

Current state:

- `_cmd_run()` calls `manifest.validate()` before preflight.
- `_preflight_loop()` catches parse/schema `ManifestError` from
  `_find_manifest()`, but it does not call `manifest.validate()` before running
  adapter preflight.
- A focused manifest using `linear:project/Daily` as an output produced
  `manifest_validate = [unsupported external sink ...]` while
  `_preflight_loop()` returned exit code 0 and `preflight demo (stub): OK`.
- Other custom validation failures—missing cron `at`, unsafe/unsupported I/O,
  invalid duration, or an invalid canonical id loaded through a future path—can
  be similarly omitted from the loop-scoped dependency result.

Why this matters:

`deps check --loop` is presented as the exact selected-loop preflight. Reporting
OK for a loop that `loopctl run` immediately rejects gives automation and the
operator contradictory readiness signals. It also falls short of the design's
requirement that a manifest be validated before execution/deployment checks.

Recommended fix:

- Share a single `load + filename/id check + manifest.validate()` helper between
  `run` and `_preflight_loop()`.
- Return a structured nonzero `invalid_manifest` result before invoking the
  adapter when custom validation fails.
- Add a JSON-envelope regression test using a Pydantic-valid but semantically
  invalid manifest.

### 5. A schema-invalid run-history file can crash `status` and `logs`

Relevant files:

- `src/loopcraft/store.py`
- `src/loopcraft/cli.py`
- `tests/test_store.py`
- `tests/test_cli.py`

Current state:

- `Store.runs_for()` skips malformed JSON and filesystem read errors, matching
  its docstring's promise to skip unreadable records.
- It does not catch Pydantic validation errors when valid JSON for the selected
  loop is missing required `RunRecord` fields or contains incompatible types.
- The focused record `{"loop": "demo", "status": 42}` raised an uncaught
  `ValidationError` instead of being skipped or surfaced as a degraded-history
  problem.
- `latest_run()`, `status`, and `logs` all use `runs_for()`, so one bad record
  can make all history access for that loop fail.

Why this matters:

The ledger is durable, human-legible state and will outlive schema revisions.
History readers need to tolerate partial writes, manual edits, and older record
shapes. A single bad JSON object should not take down the fleet status view.

Recommended fix:

- Catch Pydantic `ValidationError` (and relevant type/value errors) per record,
  just as JSON/read errors are caught today.
- Prefer atomic run-record writes (`temporary file + replace`) to reduce the
  chance of partial durable records.
- Surface skipped/corrupt record paths as structured problems where the CLI can
  do so without losing valid history; at minimum skip them consistently.
- Add store and CLI regression tests for valid JSON with missing fields, wrong
  field types, and a mix of valid and corrupt records.

### 6. Accepting a local-only `content.config` contradicts the documented public/private pattern

Relevant files:

- `CONTRIBUTING.md`
- `src/loopcraft/runners/capabilities.py`
- `src/loopcraft/worktree.py`
- `tests/test_capabilities.py`
- `tests/test_worktree.py`

Current state:

- `CONTRIBUTING.md` defines the prevailing pattern as a committed public file
  containing safe placeholders/documentation plus an optional gitignored local
  sibling containing real private values.
- The Review 05 implementation intentionally treats a local sibling as a valid
  `content.config` even when the declared public file is absent, and adds tests
  that lock in that behavior.
- Because `*.local.*` is ignored, such a loop can pass on the developer's host
  while the source branch contains no discoverable config shape/defaults and a
  clean host fails preflight after checkout.

Why this matters:

This weakens the explicit-dependency and trivial-setup goals: the manifest
points at a file that does not exist in source control, and the private file is
neither portable nor self-documenting. It also directly diverges from the
contribution rule the review was asked to enforce.

Recommended fix:

- Require the declared public `content.config` to exist as a regular committed
  source asset. A local sibling may shadow it but should not replace its
  existence contract.
- When every real value is private, commit a safe empty/placeholder config that
  documents the schema and setup instructions.
- Replace the local-only acceptance tests with tests asserting a clear
  preflight/staging failure when the public file is absent.

## Notes On Resolved/Healthy Areas

- All four Review 05 findings are substantively fixed: loop ids and filename
  identity are validated, missing configs now fail before execution, invalid
  manifests are structured/degraded CLI results, and produced outputs are
  separated from declared outputs in run records.
- The full offline test suite, compile check, manifest validation, and current
  environment dependency check pass.
- Previously acknowledged deferrals are not repeated as new findings: the
  staged asset bundle is not yet a real git worktree, and token/cost plus
  max-turn/max-token enforcement remain adapter follow-ups.
