# Milestone 1 Review 05 — Report (branch implementation follow-up)

Review target: local branch `feat/m1-control-plane` in
`/Users/dpickem/workspace/loopcraft`.

Reviewed state:

- HEAD: `9807633 docs: add M1 review 04 report and response`
- Included the design reference at
  `docs/loopcraft-implementation-design.html`.

Reference scope:

- M1 build plan and control-plane contracts in
  `docs/loopcraft-implementation-design.html`
- `CONTRIBUTING.md`
- Review 04 response in
  `docs/review_notes/2026_07_02_milestone_1_review_04_response.md`
- Prior Review 01–04 reports and responses, to avoid repeating acknowledged
  deferrals or already-fixed findings

This pass reviewed the branch as a whole after the Review 04 fixes. The five
Review 04 findings are addressed as described in its response. The findings
below are additional issues not covered by the earlier reports.

## Verification

Commands run from `/Users/dpickem/workspace/loopcraft`:

```text
make test && make compile && make validate
make check
```

Result:

- `make test`: passed, 114 tests.
- `make compile`: passed.
- `make validate`: passed, 3 manifests.
- `make check`: passed in this environment.

A focused diagnostic also constructed an otherwise-valid manifest with an
absolute `id` and a missing `content.config`. It produced:

```text
manifest_problems= []
preflight_asset_problems= []
worktree= /tmp/loopcraft-escaped/run-id
```

The live Slack exit-criteria run was not executed because it would access real
external data. The offline suite still uses a stub adapter for the end-to-end
control-plane path.

## Findings

### 1. Unvalidated loop identifiers can escape both the loops directory and the memory worktree root

Relevant files:

- `src/loopcraft/manifest.py`
- `src/loopcraft/cli.py`
- `tests/test_manifest.py`
- `tests/test_cli.py`

Current state:

- `LoopManifest.id` is an unrestricted `str`; manifest validation only checks
  that it is non-empty.
- `_find_manifest()` directly interpolates the command-line loop id into
  `config.loops_dir / f"{loop_id}.yaml"` without validating that it is one safe
  filename component. A value such as `../outside` or an absolute path can make
  `loopctl run` load YAML outside `loops/`.
- `_worktree_dir()` directly appends `manifest.id` to
  `<memory>/var/worktrees/`. An absolute manifest id discards that prefix, while
  `..` segments can traverse above it. The focused diagnostic resolved the run
  worktree to `/tmp/loopcraft-escaped/run-id` even though the configured memory
  root was `/tmp/loopcraft-review-mem`.
- The manifest filename and `manifest.id` are not required to match. As a
  result, `loopctl list` can advertise one id while `loopctl run <advertised-id>`
  looks for a different filename, and `loopctl run <filename-stem>` can execute
  under a different id.

Why this matters:

The design and contribution rules make the source tree and memory tree explicit
filesystem boundaries. A checked-in manifest should not be able to choose an
arbitrary execution/staging directory, and the CLI's loop selector should not be
an alternate file-path input. This also makes pruning operate on a path derived
from unchecked manifest data.

Recommended fix:

- Define and validate a canonical loop-id vocabulary, for example lowercase
  alphanumeric components separated by single hyphens.
- Validate the CLI loop argument before constructing a path and confirm the
  resolved manifest remains directly under `config.loops_dir`.
- Require each manifest id to equal its filename stem during `load_all()` and
  single-manifest lookup.
- Apply a containment check to the computed per-loop worktree root before
  creating or pruning it.
- Add regression tests for absolute ids, `..` ids/CLI arguments, separator
  characters, and filename/id mismatches.

### 2. A declared `content.config` can be missing while validation and preflight both pass

Relevant files:

- `src/loopcraft/manifest.py`
- `src/loopcraft/runners/capabilities.py`
- `src/loopcraft/worktree.py`
- `tests/test_capabilities.py`
- `tests/test_worktree.py`

Current state:

- Manifest validation checks only that `content.config` is a safe relative path.
- Runtime-neutral preflight checks `logic.skill` and `logic.verify`, but not
  `content.config`.
- `_stage_content_config()` silently skips a nonexistent public config and then
  continues unless a matching local sibling happens to exist.
- Consequently, an otherwise-valid research manifest naming
  `config/does-not-exist.yaml` has no validation or preflight problems. The
  headless agent starts and discovers the missing dependency only when the
  direct Python CLI attempts to load it.

Why this matters:

The design's explicit-dependency requirement says unmet dependencies should be
reported before a loop runs. Review 04 made `content.config` a staged runtime
asset; its existence now needs the same fail-fast treatment as the skill and
verify assets.

Recommended fix:

- Extend the shared source-asset preflight to require `content.config` to be a
  regular file when declared.
- Make staging raise a clear error when neither the declared public config nor
  an allowed effective config exists, rather than silently omitting it.
- Add validation/preflight and staging tests for a missing path and for a path
  that resolves to a directory.

### 3. Invalid manifests either crash `run` or are silently hidden by `list` and `status`

Relevant files:

- `src/loopcraft/cli.py`
- `src/loopcraft/manifest.py`
- `src/loopcraft/cli_output.py`
- `tests/test_cli.py`

Current state:

- `_find_manifest()` calls `LoopManifest.load()` without catching
  `ManifestError`. Invalid YAML or a Pydantic schema error therefore escapes the
  command handler as a traceback, including under `--json`.
- `load_all()` does turn parse/validation failures into problems, but
  `_cmd_list()` and `_cmd_status()` discard those problems and always emit a
  successful result. A broken manifest can therefore disappear from the fleet
  view without any warning.
- `validate` and `apply` correctly surface the same errors, so behavior differs
  by command.

Why this matters:

The design describes manifests as schema-checked on load, and `CONTRIBUTING.md`
requires consistent agent-friendly JSON output. A traceback is not a JSON
envelope, while silently omitting an invalid loop makes the operational views
unreliable precisely when configuration is broken.

Recommended fix:

- Catch `ManifestError` in single-manifest lookup/dispatch and emit a structured
  command failure in both text and JSON modes.
- Preserve `load_all()` problems in `list` and `status`; return a nonzero result
  (or an explicitly degraded result) while still including any valid loops as
  partial data.
- Add CLI tests covering malformed YAML and schema-invalid YAML for `run`,
  `list`, and `status`, including JSON-envelope assertions.

### 4. Failed preflight run records claim every declared output was produced

Relevant files:

- `src/loopcraft/cli.py`
- `src/loopcraft/store.py`
- `tests/test_cli.py`

Current state:

- Normal executed runs store `result.outputs`, which is the runner's list of
  files actually produced/refreshed.
- `_record_preflight_failure()` instead stores `manifest.outputs` even though
  execution never started and no output was touched.
- The existing failed-preflight test checks status and problems, but does not
  assert that `outputs` is empty.

Why this matters:

The design defines normalized run results and run-history records in terms of
I/O/artifacts touched, and later harvesting derives operational history from
those records. Recording declared paths as produced output on a preflight
failure creates false provenance and makes failed runs look like producers to
downstream readers.

Recommended fix:

- Record `outputs=[]` for a preflight failure. If the declared contract is also
  useful, add a separately named `declared_outputs` field rather than overloading
  the produced-output field.
- Likewise, consider whether `inputs` should mean declared inputs or inputs
  actually consumed and name the field explicitly so success and preflight
  failure records share one semantic contract.
- Extend `test_run_records_failure_on_preflight` to assert that no outputs (and,
  if appropriate, no consumed inputs) are recorded.

## Notes On Resolved/Healthy Areas

- All five Review 04 fixes are present, including anti-recursion skill wording,
  run-id propagation, X source-state creation, content-config staging/local
  shadowing, and required-vs-optional dependency probing.
- The full offline test suite, compile check, manifest validation, and current
  environment dependency check pass.
- Previously acknowledged deferrals are not repeated as new findings: the
  staged asset bundle is not yet a real git worktree, and token/cost plus
  max-turn/max-token enforcement remain adapter follow-ups.
