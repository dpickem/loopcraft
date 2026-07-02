# Milestone 1 Review 04 — Response

Response to [`2026_07_02_milestone_1_review_04_report.md`](2026_07_02_milestone_1_review_04_report.md),
the whole-branch follow-up review of `feat/m1-control-plane`. Prior history:
[01 report](2026_06_30_milestone_1_review_01_report.md) /
[01 response](2026_06_30_milestone_1_review_01_response.md),
[02 report](2026_06_30_milestone_1_review_02_report.md) /
[02 response](2026_06_30_milestone_1_review_02_response.md),
[03 report](2026_06_30_milestone_1_review_03_report.md) /
[03 response](2026_06_30_milestone_1_review_03_response.md).

All five findings are addressed, each with a regression test.

## Commit

The fixes are in one commit on `feat/m1-control-plane`:

```text
09bd235 fix: address M1 review 04 findings
```

Inspect with `git show 09bd235 -- <file>` using the file lists below.

## Status

Verification from the repo root:

```text
make test && make compile && make validate
```

- `make test`: **114 passed** (up from 105 at review time), fully offline.
- `make compile`: passes.
- `make validate`: passes, 3 manifests.

## Findings

### 1. Research-loop skills can recursively invoke `loopctl run` — fixed

The `arxiv-intel` and `x-intel` skills previously told the agent to run
`make run LOOP=<self>`. Since the skill is loaded into that loop's own headless
run, that re-entered `loopctl run` for the same loop and could recurse.

- Both skills now instruct the in-loop agent to run the **direct CLI**
  (`python -m loopcraft.research_intel.<arxiv|x>.cli run --config ...`) and
  explicitly warn against re-entering the control plane for the same loop. The
  operator-facing `loopctl`/`make run` trigger is no longer shown inside the
  skill, so the in-loop prompt cannot recurse.
- Files: `skills/arxiv-intelligence-reporting/SKILL.md`,
  `skills/x-intelligence-reporting/SKILL.md`.
- Test (`tests/test_manifest.py`): `test_research_skills_do_not_self_invoke_loopctl`
  fails if a loop skill names `loopctl run <self>`, `make run LOOP=<self>`, or
  `loopcraft.cli run <self>`, and asserts each skill points at the direct CLI and
  carries an anti-recursion warning.

A fully deterministic (non-agent) adapter that invokes the Python CLI directly
remains a reasonable future option; for M1 the direct-CLI instruction plus the
run-id handoff below makes the agent path produce the declared outputs.

### 2. Direct arXiv/X CLIs now write the manifest's `{{run_id}}` history outputs — fixed

`loopctl run` resolves `history/{{run_id}}.md/json` using the control-plane
`Store.new_run_id()`, but the direct CLIs named their history archives with a
local timestamp, so `BaseRunner`'s declared-output check could mark a successful
run failed.

- The control plane now passes its run id to the loop's subprocess via the
  `LOOPCRAFT_RUN_ID` env var (`RunContext.env`), and both direct CLIs use
  `LoopcraftConfig.env_value(RUN_ID_ENV)` (falling back to a timestamp for
  standalone operator runs) as the history archive stamp.
- Files: `src/loopcraft/config.py` (`RUN_ID_ENV`), `src/loopcraft/cli.py`
  (sets `env={RUN_ID_ENV: run_id}`), `src/loopcraft/research_intel/arxiv/cli.py`,
  `src/loopcraft/research_intel/x/cli.py`.
- Test (`tests/test_arxiv_intel.py`):
  `test_arxiv_run_produces_manifest_outputs_for_control_plane_run_id` sets
  `LOOPCRAFT_RUN_ID`, runs the direct workflow with a mocked client, then resolves
  **every** declared `arxiv-intel` manifest output for that run id and asserts each
  file exists. The X equivalent is covered by the F3 test below.

### 3. Default `x-intel` always writes the declared `source-state.json` — fixed

With the shipped empty-source config, `remember_source_highwater()` never ran, so
`source-state.json` (a declared output) was absent and the run could fail the
declared-output check even with no API error.

- `IntelStore.persist_source_state()` now (re)writes `source-state.json` (`{}` on
  a fresh loop), and `XIntelRunner.run()` calls it on every non-dry run, so the
  file both exists and is refreshed regardless of sources.
- Files: `src/loopcraft/research_intel/x/store.py`,
  `src/loopcraft/research_intel/x/cli.py`.
- Tests (`tests/test_x_intel.py`):
  `test_x_run_writes_declared_outputs_with_run_id` runs the default-config
  workflow and asserts every declared X output — including `source-state.json` and
  the run-id-named history archives — exists;
  `test_x_persist_source_state_initializes_empty_file` covers the empty case.

### 4. `loopctl run` stages `content.config` and its private override — fixed

The public/private split was documented but `stage_loop_assets()` did not stage
`manifest.content.config`, and the direct CLI only picked up `*.local.yaml` via
Makefile wildcard logic.

- `stage_loop_assets()` now stages the loop's `content.config` (and any
  `*.local.*` sibling) into the run worktree; the existing local-shadowing pass
  overlays the private override onto the public file.
- The research config loaders (`IntelConfig.load` / `ArxivIntelConfig.load`) now
  prefer a gitignored `<name>.local.yaml` sibling via
  `loopcraft.settings.local_override_path()`, so the override applies whether the
  loop runs through `make`, `loopctl run`, or the direct CLI — not just the
  Makefile path.
- Files: `src/loopcraft/worktree.py`, `src/loopcraft/settings.py`,
  `src/loopcraft/research_intel/arxiv/config.py`,
  `src/loopcraft/research_intel/x/config.py`, plus `README.md` / `CONTRIBUTING.md`
  updated so the docs match the implementation.
- Tests: `tests/test_worktree.py::test_stage_loop_assets_stages_content_config_with_local_override`,
  `tests/test_x_intel.py::test_x_config_load_prefers_local_override`,
  `tests/test_arxiv_intel.py::test_arxiv_config_load_prefers_local_override`.

### 5. `make check` no longer fails on future runtime binaries — fixed

`[tool.loopcraft.dependencies]` previously listed `claude` and `cursor-agent`, so
`loopctl deps check` (and thus `make check`) could fail on a Codex-only M1 host.

- Dependencies are split into required M1 binaries
  (`[tool.loopcraft.dependencies]`: `python`, `git`, `codex`, `nv-tools`) and
  optional future runtimes (`[tool.loopcraft.optional-dependencies]`: `claude`,
  `cursor-agent`). `loopctl deps check` fails only on missing **required**
  binaries and reports optional ones without failing. `deps check --loop <id>`
  now scopes to just that loop's adapter preflight (its declared runtime and
  dependencies).
- Files: `pyproject.toml`, `src/loopcraft/config.py` (`optional_dependencies`),
  `src/loopcraft/cli.py` (`_cmd_deps_check`), plus `README.md`.
- Tests (`tests/test_cli.py`):
  `test_deps_check_optional_missing_does_not_fail` and
  `test_deps_check_required_missing_fails`.

## Notes on healthy areas

Review 04 confirmed strong offline coverage, the Pydantic control-plane models,
the consistent CLI JSON envelope, the centralized runtime-neutral capability
probes, and the per-source `XApiError` isolation in the X fetch path. Those areas
are unchanged here (the fetch-isolation change is now committed with a regression
test, `test_collect_raw_posts_isolates_failing_sources`).
