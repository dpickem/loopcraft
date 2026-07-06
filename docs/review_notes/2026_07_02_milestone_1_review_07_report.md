# Milestone 1 Review 07 — Report (branch implementation follow-up)

Review target: local branch `feat/m1-control-plane` in
`/Users/dpickem/workspace/loopcraft`.

Reviewed state:

- HEAD: `fda993c docs: add M1 review 06 report and response`
- Review 06 implementation commit: `ecf18fe fix: address M1 review 06 findings`

Reference scope:

- M1 build plan and control-plane contracts in
  `docs/loopcraft-implementation-design.html`
- `CONTRIBUTING.md`
- Review 06 response in
  `docs/review_notes/2026_07_02_milestone_1_review_06_response.md`
- Prior Review 01–06 reports and responses, to avoid repeating acknowledged
  deferrals or already-fixed findings

This pass verified the six Review 06 fixes and reviewed the branch again for
second-order gaps introduced or exposed by those changes. The findings below
are additional issues not covered by the earlier reports.

## Verification

Commands run from `/Users/dpickem/workspace/loopcraft`:

```text
make test compile validate check PYTHON=/Users/dpickem/miniconda3/bin/python
```

Result:

- `make test`: passed, 165 tests.
- `make compile`: passed.
- `make validate`: passed, 3 manifests.
- `make check`: dependency probing failed in this review shell because its
  current `PATH` does not expose commands named `python` or `nv-tools` (git and
  Codex were found). The tests/compile/validation used the explicit Python 3.12
  path above. This is an environment result, not evidence of a branch
  regression; the Review 06 response records `make check` passing in its
  environment.

Focused diagnostics reproduced the following behaviors outside the current
suite:

- A safe in-tree `SKILL.md` plus a sibling symlink to an outside file passed
  source containment; `copytree()` dereferenced the sibling and copied the
  outside contents into the run worktree.
- A registered runner whose `preflight()` raised was normalized by
  `loopctl run`, but the same exception escaped `_preflight_loop()` (the
  `deps check --loop` path).
- A malformed YAML `content.config` passed runtime-neutral preflight with no
  problems, then raised a YAML parser exception when the typed content loader
  opened it.
- Two otherwise-valid manifests declaring the same output path produced no
  `load_all()` validation problem.

The live Slack exit-criteria run was not executed because it would access real
external data. The offline suite continues to use a stub adapter for the
end-to-end control-plane path.

## Findings

### 1. Whole-directory staging can still dereference source symlinks outside the source tree

Relevant files:

- `src/loopcraft/worktree.py`
- `src/loopcraft/runners/capabilities.py`
- `src/loopcraft/manifest.py`
- `src/loopcraft/paths.py`
- `tests/test_worktree.py`

Current state:

- The Review 06 fix correctly real-resolves paths that actually flow through
  `assert_under()`.
- Skill staging validates only the declared `logic.skill` path, then copies its
  entire parent directory with `shutil.copytree(..., symlinks=False)` (the
  default). Sibling files and nested entries are never individually checked.
- If the declared `SKILL.md` is safe but another skill asset is a symlink to an
  outside file, `copytree()` follows that link and copies the outside contents
  into the worktree as a regular file. The focused diagnostic produced
  `staged_is_symlink=False` and read the outside secret from the staged file.
- `_stage_content_config()` similarly validates the public config through
  `resolve_source_path()`, but computes the optional local sibling directly and
  copies it without checking that a symlinked local override resolves under the
  source root.
- `load_all()` also globs and loads manifest entries without applying the
  source-root containment check used by `_find_manifest()`, so a manifest-file
  symlink is another bulk-read path that can leave the source tree.

Why this matters:

The Review 06 response states that an in-tree symlink cannot redirect source
reads outside the boundary. That is true for a single validated path but not
for the directory enumeration that follows it. Skills intentionally stage all
sibling assets, so one unchecked symlink can still disclose any readable file
to the headless run despite the new real-path helper.

Recommended fix:

- Traverse every source entry that will be staged and validate its resolved
  target under `config.source_path` before copying it. Do not rely on
  `copytree()`'s default symlink dereferencing.
- Either reject staged symlinks outright or preserve only symlinks whose
  resolved targets remain under source, with the same explicit policy used for
  `logic.skill`.
- Validate the local content-config sibling through the source resolver before
  copying it.
- Apply the same containment rule to every manifest path discovered by
  `load_all()`.
- Add regressions for a nested/sibling skill symlink, a symlinked local config,
  and a symlinked manifest discovered by fleet validation.

### 2. `deps check --loop` still lets adapter preflight exceptions escape

Relevant files:

- `src/loopcraft/cli.py`
- `tests/test_cli.py`

Current state:

- `_cmd_run()` wraps `runner.preflight()` and converts an ordinary exception
  into a failed `PreflightReport`, preserving the Review 06 lifecycle contract.
- `_preflight_loop()` shares manifest lookup/validation with `run`, but calls
  `runner.preflight()` directly without the same exception boundary.
- A focused raising stub produced `RuntimeError: boom` from
  `_preflight_loop()` rather than a structured failure. Through the public CLI,
  `loopctl --json deps check --loop <id>` can therefore still emit a traceback
  instead of the required JSON envelope.

Why this matters:

The two commands now share readiness validation but not readiness execution.
An adapter fault is normalized when running the loop and crashes when explicitly
checking that same loop's dependencies, which recreates the command-consistency
problem the Review 06 changes were intended to remove.

Recommended fix:

- Extract one shared safe-preflight helper used by both `_cmd_run()` and
  `_preflight_loop()`.
- Return exit code 1 with the same `preflight raised <Type>: <message>` problem
  shape in text and JSON modes.
- Add a public CLI regression test for a raising adapter under
  `--json deps check --loop`.

### 3. Newly declared `{{date}}` digest outputs can disagree across UTC midnight

Relevant files:

- `src/loopcraft/cli.py`
- `src/loopcraft/config.py`
- `src/loopcraft/research_intel/arxiv/cli.py`
- `src/loopcraft/research_intel/x/cli.py`
- `loops/arxiv-intel.yaml`
- `loops/x-intel.yaml`
- `tests/test_arxiv_intel.py`
- `tests/test_x_intel.py`

Current state:

- The Review 06 fix added
  `state/research/<loop>/digests/{{date}}.md/.json` to both manifests.
- The control plane resolves `{{date}}` once from `started.date()` before the
  headless run begins.
- The direct research workflows independently choose the dated filename from
  `datetime.now(UTC)` when their fetch/rank work completes. Only the run id is
  handed from the control plane to the child workflow; the resolved run date is
  not.
- Existing tests compute the expected date with `datetime.now(UTC)` immediately
  after the direct workflow, so they cannot exercise a start-before-midnight /
  write-after-midnight run.

Why this matters:

A legitimate run crossing 00:00 UTC writes the next day's digest while
`BaseRunner` checks the previous day's manifest-resolved output. The run is
reported failed for a missing declared output and has also written an
undeclared path. This is the date-template equivalent of the run-id mismatch
fixed in Review 04.

Recommended fix:

- Pass the control plane's resolved run date alongside `LOOPCRAFT_RUN_ID` (for
  example `LOOPCRAFT_RUN_DATE`) and require in-loop workflows to use it for
  every `{{date}}` output.
- Alternatively derive the UTC date deterministically from the control-plane
  run id, rather than sampling the clock again.
- Preserve the current-clock fallback only for standalone direct-CLI runs.
- Add boundary tests with a control-plane start on one UTC date and a mocked
  workflow clock on the next.

### 4. `content.config` is checked for existence but not validity before the agent starts

Relevant files:

- `src/loopcraft/runners/capabilities.py`
- `src/loopcraft/research_intel/arxiv/config.py`
- `src/loopcraft/research_intel/arxiv/cli.py`
- `src/loopcraft/research_intel/x/config.py`
- `src/loopcraft/research_intel/x/cli.py`
- `tests/test_capabilities.py`
- `tests/test_arxiv_intel.py`
- `tests/test_x_intel.py`

Current state:

- Runtime-neutral preflight now requires the public content config to be a
  regular, contained file, but it never parses the effective public/local YAML
  or validates it against the loop's Pydantic content model.
- A malformed effective config therefore passes preflight, starts a headless
  Codex run, and fails only when the skill invokes the direct Python workflow.
  The focused malformed YAML file produced `preflight_problems=[]` followed by
  a YAML `ParserError` from `IntelConfig.load()`.
- Both direct CLI entry points allow YAML, Pydantic, and file-read errors from
  config loading to escape before `emit()` is reached. Under `--json`, this is
  a traceback rather than the consistent JSON result envelope required by
  `CONTRIBUTING.md`.

Why this matters:

The content definition is a declared runtime dependency. Existence alone does
not establish that the loop can consume it, and discovering a syntax/schema
error after launching an agent wastes the run budget. The unhandled direct-CLI
error also regresses the branch's stated JSON-envelope parity.

Recommended fix:

- Give each content-bearing loop a declared or registered config validator and
  invoke it during preflight against the effective public/local file.
- At minimum parse YAML during shared preflight; for the shipped research loops,
  run the actual `ArxivIntelConfig` / `IntelConfig` Pydantic validation so unknown
  fields, invalid types, and forbidden `output` overrides fail before agent
  startup.
- Catch configuration read/parse/validation errors at both direct CLI
  boundaries and emit structured text/JSON failures with a nonzero exit code.
- Add tests for malformed YAML, schema-invalid YAML, and a malformed local
  override through both `loopctl run --dry-run` and each direct CLI's `--json`
  mode.

### 5. Fleet validation allows multiple loops to claim the same output

Relevant files:

- `src/loopcraft/manifest.py`
- `tests/test_manifest.py`

Current state:

- `load_all()` checks duplicate loop ids, unknown upstream loops, and cycles,
  but does not detect duplicate output declarations within one manifest or
  output collisions across manifests.
- `_detect_cycles()` builds `producers` with `setdefault()`, so when multiple
  loops claim the same normalized state path, the first sorted manifest silently
  becomes the producer used for inferred input dependencies.
- A focused two-manifest fleet in which both loops output
  `state/shared/out.md` returned no validation problems.

Why this matters:

The design says manifest I/O contracts drive scheduling and the inferred DAG.
With two producers, dependency inference is order-dependent and the loops can
race while overwriting the same durable ledger file. The catalog should either
forbid this ambiguity or model multi-producer semantics explicitly; silently
choosing the first producer is not a stable contract.

Recommended fix:

- During fleet validation, normalize every output and require one producing
  loop per path unless an explicit future multi-producer policy says otherwise.
- Report duplicate output entries within a single manifest as well.
- Replace `setdefault()` with construction that retains all producers and emits
  a clear validation issue naming the path and loops.
- Add tests for exact duplicates, normalized duplicates, and duplicate entries
  within one manifest.

## Notes On Resolved/Healthy Areas

- All six Review 06 findings are substantially addressed: direct containment
  checks resolve symlinks, run exceptions are recorded and worktrees finalized,
  research output locations are removed from content config, run/dependency
  lookup shares semantic validation, corrupt history is tolerated with atomic
  writes, and public content configs are required.
- The full offline test suite, compile check, and manifest validation pass.
- Previously acknowledged deferrals are not repeated as new findings: the
  staged asset bundle is not yet a real git worktree, token/cost plus
  max-turn/max-token enforcement remain adapter follow-ups, and full TOCTOU
  symlink-race hardening remains outside M1.
