# Milestone 1 Review 07 — Response

Response to [`2026_07_02_milestone_1_review_07_report.md`](2026_07_02_milestone_1_review_07_report.md),
the whole-branch follow-up review of `feat/m1-control-plane` after the Review 06
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
[05 response](2026_07_02_milestone_1_review_05_response.md),
[06 report](2026_07_02_milestone_1_review_06_report.md) /
[06 response](2026_07_02_milestone_1_review_06_response.md).

All five findings are addressed, each with regression tests.

## Commit

The fixes are in one commit on `feat/m1-control-plane`:

```text
30d5223 fix: address M1 review 07 findings
```

Inspect with `git show 30d5223 -- <file>` using the file lists below.

## Status

Verification from the repo root:

```text
make test && make compile && make validate
```

- `make test`: **182 passed** (up from 165 at review time), fully offline.
- `make compile`: passes.
- `make validate`: passes, 3 manifests.
- `loopctl deps check` (`make check`): passes in this environment (all four
  required binaries found).

Every focused diagnostic from the report now fails safely:

```text
sibling symlink staging:      REFUSED: source asset escapes allowed root:
                              .../src/skills/demo/leak.txt (nothing copied)
deps-path preflight raising:  rc 1 with problems=['preflight raised RuntimeError: boom']
                              (same shape as `run`)
malformed content.config:     preflight problem 'content config invalid: config/demo.yaml: ...'
duplicate declared output:    "outputs: output 'shared/out.md' is declared by
                              multiple loops: loop-a, loop-b"
```

## Findings

### 1. Whole-directory staging could dereference symlinks outside the source tree — fixed

Skill staging validated only the declared `logic.skill` path and then bulk-copied
its parent directory with `shutil.copytree()`'s default symlink dereferencing;
the local config sibling and `load_all()`'s manifest glob were similar
enumeration paths with no per-entry check.

- Skill-directory staging now goes through `_copytree_contained()`: every
  directory and file below the skill dir — sibling files, nested directories,
  and symlinked entries — must individually resolve under the source root
  before it is copied (with a symlink-cycle guard on the walk). The policy is
  the one the report offered and `logic.skill` already used: an
  in-source-resolving symlink is dereference-copied like any other file; one
  that escapes aborts staging with `SourcePathError` (which `loopctl run`
  already normalizes to a structured staging failure).
- `_stage_content_config()` validates the gitignored `*.local.*` sibling
  through the same source-containment guard before copying it, and the staged
  manifest file (`manifest.source_path`) gets the identical check.
- `load_all()` applies the same containment rule as `_find_manifest()` to
  every manifest path its glob discovers, so a symlinked `loops/` entry that
  resolves elsewhere becomes a validation problem instead of a bulk read
  outside the tree.
- Files: `src/loopcraft/worktree.py` (`_copytree_contained`,
  `_assert_source_contained`, `_stage_content_config`, manifest staging),
  `src/loopcraft/manifest.py` (`load_all` containment).
- Tests: `tests/test_worktree.py::test_stage_rejects_sibling_symlink_escaping_source`
  (asserts nothing leaked into the worktree),
  `::test_stage_rejects_nested_symlinked_dir_escaping_source`,
  `::test_stage_copies_in_source_sibling_symlink_content` (the allowed
  direction), `::test_stage_rejects_symlinked_local_config_escaping_source`;
  `tests/test_manifest.py::test_load_all_rejects_symlinked_manifest_escaping_loops_dir`.

### 2. `deps check --loop` let adapter preflight exceptions escape — fixed

`_cmd_run()` normalized a raising `preflight()` but `_preflight_loop()` called
the adapter directly, so the same fault crashed the dependency check.

- The Review 06 boundary is extracted into one shared `_safe_preflight()`
  helper used by both `_cmd_run()` and `_preflight_loop()`. An ordinary adapter
  exception becomes a failing `PreflightReport` with the identical
  `preflight raised <Type>: <message>` problem string; exit code 1 and the
  standard text/JSON shapes flow through the existing report handling.
  Readiness validation *and* readiness execution are now shared by
  construction.
- Files: `src/loopcraft/cli.py` (`_safe_preflight`, `_cmd_run`,
  `_preflight_loop`).
- Test (`tests/test_cli.py`):
  `test_deps_check_loop_normalizes_preflight_exception` — a raising adapter
  under the public `--json deps check --loop` CLI produces the JSON envelope
  with rc 1 and the normalized problem, not a traceback.

### 3. `{{date}}` digest outputs could disagree across UTC midnight — fixed

The control plane resolved `{{date}}` once at run start while the workflows
independently sampled the clock when writing, so a run crossing 00:00 UTC wrote
the next day's file and failed the declared-output check.

- The control plane now hands its resolved run date to the loop subprocess via
  `LOOPCRAFT_RUN_DATE` (`RUN_DATE_ENV`), set from the same `started.date()`
  that resolves the manifest `{{date}}` outputs — the exact mechanism Review 04
  established for `{{run_id}}`.
- Both research workflows stamp their dated digest filenames from
  `RUN_DATE_ENV` when present; the current clock remains only the fallback for
  standalone direct-CLI runs, as recommended.
- Files: `src/loopcraft/config.py` (`RUN_DATE_ENV`), `src/loopcraft/cli.py`
  (`RunContext.env`), `src/loopcraft/research_intel/arxiv/cli.py`,
  `src/loopcraft/research_intel/x/cli.py`.
- Tests: `test_arxiv_run_produces_manifest_outputs_for_control_plane_run_id`
  and `test_x_run_writes_declared_outputs_with_run_id` now set a handed-down
  run date (`2026-01-01`) that deliberately differs from the machine's real
  clock date — the midnight-boundary disagreement — and assert every declared
  output, including the dated digests, resolves for the *handed-down* date.

### 4. `content.config` was checked for existence but not validity — fixed

Preflight never parsed the effective public/local YAML, so a malformed config
started a headless agent; both direct CLIs also let config-load errors escape
as tracebacks under `--json`.

- The shared runtime-neutral preflight now validates the effective config via
  a registered, injectable validator table (`CONTENT_VALIDATORS`): the shipped
  research loops run their real typed models (`ArxivIntelConfig.load` /
  `IntelConfig.load`, lazily imported), so malformed YAML, unknown fields,
  invalid types, and the forbidden `output` override all fail before agent
  startup; loops without a registered validator get a plain YAML parse of the
  effective file. Both branches honor the `*.local.*` override, so a malformed
  local file is caught even when the public file is fine.
- Both direct CLI boundaries now convert config read/parse/validation errors
  into structured failures (exit code 2) through a shared `cli_output.fail()`
  helper (the X CLI's private `_fail` was promoted to `cli_output` and the
  arXiv CLI uses it too; `XIntelRunner` construction is wrapped for both `run`
  and `discover-follows`).
- Files: `src/loopcraft/runners/capabilities.py`, `src/loopcraft/cli_output.py`,
  `src/loopcraft/research_intel/arxiv/cli.py`,
  `src/loopcraft/research_intel/x/cli.py`.
- Tests: `tests/test_capabilities.py::test_capabilities_flag_malformed_yaml_content_config`,
  `::test_capabilities_run_registered_content_validator` (schema violation and
  `output` override through the typed model),
  `::test_capabilities_flag_malformed_local_override`,
  `::test_capabilities_valid_content_config_passes`;
  `tests/test_cli.py::test_dry_run_surfaces_invalid_content_config`
  (`loopctl run --dry-run` path);
  `tests/test_arxiv_intel.py::test_arxiv_cli_reports_invalid_config_as_json_envelope`
  and `tests/test_x_intel.py::test_x_cli_reports_invalid_config_as_json_envelope`
  (each direct CLI's `--json` mode).

### 5. Fleet validation allowed multiple loops to claim the same output — fixed

`load_all()` never checked output collisions, and `_detect_cycles()` silently
made the first sorted manifest the producer.

- Fleet validation now normalizes every declared output and requires exactly
  one producing loop per path, emitting a problem that names the path and all
  claiming loops (`output 'shared/out.md' is declared by multiple loops:
  loop-a, loop-b`). Duplicate entries within a single manifest are reported
  separately (`output declared more than once in this manifest`). No explicit
  multi-producer policy exists in M1, so ambiguity is forbidden rather than
  modeled.
- `_detect_cycles()` replaces the first-wins `setdefault()` with a
  producers-per-path list and adds dependency edges from *every* producer, so
  cycle detection cannot miss a cycle through a non-first producer while the
  validation error above is being fixed.
- Files: `src/loopcraft/manifest.py` (`load_all`, `_detect_cycles`).
- Tests (`tests/test_manifest.py`):
  `test_load_all_reports_output_claimed_by_multiple_loops` (exact duplicates),
  `test_load_all_reports_normalized_duplicate_outputs` (different spellings of
  one ledger path), `test_load_all_reports_duplicate_output_within_one_manifest`,
  and `test_distinct_outputs_produce_no_duplicate_problems` (no false
  positives).

## Notes on healthy areas

Review 07 confirmed all six Review 06 fixes are substantially in place and that
the offline suite, compile check, and manifest validation pass. The `make
check` failure in the review shell was environmental (no `python`/`nv-tools` on
that PATH) and did not reproduce here. Previously acknowledged deferrals remain
unchanged and are not re-argued: the staged asset bundle is not yet a real git
worktree, token/cost plus max-turn/max-token enforcement remain adapter
follow-ups, and full TOCTOU symlink-race hardening stays outside M1.
