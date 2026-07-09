# Milestone 3.5 Review 01 — Response

Response to [`2026_07_09_milestone_3_5_review_01_report.md`](2026_07_09_milestone_3_5_review_01_report.md),
the multi-model review on `feat/m3.5-multi-model`.

All 12 blocking findings (1–12) and all 7 significant suggestions (13–19) are
addressed.

## Commits

```text
0a75645 fix: address M3.5 review 01 findings (multi-model safety + control-plane wiring)
```

Inspect with `git show 0a75645 -- <file>`.

## Status

```text
make test && make compile && make validate && make check
```

- `make test`: **395 passed** (up from 387 at review time), fully offline.
- `make compile`: passes (src + tests).
- `make validate`: passes, 3 manifests.
- `make check`: passes (deps + dry-run apply).

New/renamed modules: `src/loopcraft/role_tools.py`; new tests
`tests/test_cli_roles.py` (CLI-level roles coverage). Core rewrites in
`src/loopcraft/orchestrator.py`, `agent_compiler.py`, `outputs.py`,
`manifest.py`, `deploy.py`, `cli.py`, `runners/base.py`.

## Blocking findings

### 1. Deployment/dependency preflight now use the multi-model path — fixed

Introduced one shared dispatcher, `deploy.resolve_preflight(config, manifest,
*, override_vendor)`, that routes a roles loop through `preflight_multi_model`
and a single-model loop through its one adapter. `run` (`cli._cmd_run`), `apply`
(`deploy.preflight_loop`), and `deps check --loop` (`cli._preflight_loop`) all
call it, so a missing role agent/adapter/binary is reported at every entry
point. Regression tests: `test_cli_roles.py::test_deps_check_roles_flags_missing_role_agent`
and `::test_apply_roles_reports_missing_binary`.

### 2. Intra-run preflights the harness; inter-stage preflights each role — fixed

Preflight now branches on execution mode (`orchestrator._preflight_intra_run` /
`_preflight_inter_stage`):

- **intra-run** checks the *harness* adapter + binary once and compile-validates
  every role for the harness format — a host with Cursor but no standalone
  Codex/Claude CLIs is no longer falsely rejected, and a host missing
  `cursor-agent` no longer falsely passes.
- **inter-stage** preflights each role's *own* adapter using its single-stage
  manifest (same object used to execute).

Tests: `test_preflight_flags_intra_run_cross_provider_without_cursor`,
`test_preflight_flags_missing_agent`.

### 3. `readonly: true` is now an enforced boundary — fixed

For a read-only stage, the orchestrator snapshots every pre-existing worktree
file (excluding the reviewer's own outputs and log) before the run and verifies
none were modified or deleted afterward (`_hash_tree` / `_protected_violations`).
A checker that touches a maker output or source-staged file fails the run and
its (rejected) outputs are **not** promoted. Intra-run additionally carries the
runtime-native read-only affordance (Cursor `readonly`, Codex `sandbox_mode =
"read-only"`). Regression test:
`test_cli_roles.py::test_run_roles_reviewer_mutation_is_rejected`.

### 4. Agent `verify` and `tools` now govern runs — fixed

- `verify` compiles into the native instructions (`AgentDefinition.prompt_body`)
  and is included in the inter-stage stage prompt, and a read-only reviewer's
  output/log is parsed for an explicit `Verdict: PASS|FAIL` (`_parse_verdict`);
  a `FAIL` fails the pipeline and a missing verdict (when `verify` is set) is a
  problem.
- `tools` are classified read/write (`role_tools.py`): an unmappable tool fails
  preflight, and a read-only role may not declare a writing tool.

### 5. Failed stages/runs no longer promote partial outputs — fixed

All three paths promote only after a full-success check: single-model
(`cli._run_execute`) promotes only on `RunStatus.DONE`; inter-stage promotes a
stage only when it is `DONE` and passed the read-only check; intra-run promotes
only when the harness is `DONE`. `promote_outputs` is transactional (temp file
+ `os.replace`). Test: `test_cli_roles.py::test_run_roles_failed_maker_not_promoted`.

### 6. Structured inter-stage handoff; Git-diff deferred — fixed/scoped

Handoff is now a structured `StageHandoff` (prior status, promoted ledger output
paths + sha256 digests, captured stdout) rendered into the next stage's prompt.
A full Git-worktree/diff code maker/checker is explicitly deferred with the L4
build loop and documented as such in the README and design doc, per the review's
offered option to narrow scope rather than overclaim.

### 7. One normalized ownership plan for both modes — fixed

`build_execution_plan` computes each role's owned outputs once (read-only role →
its own outputs; maker → its own or the loop's top-level) and the maker-output
set handed to the reviewer. Inter-stage and intra-run both consume this plan, so
the same manifest resolves identically in either mode; overlapping ownership is
rejected (`manifest._role_issues` + plan-level check).

### 8. `--vendor` override propagated into roles — fixed

`resolve_preflight`, `preflight_multi_model`, and `run_multi_model` take
`override_vendor`; an inherited role resolves to the override while an explicit
role vendor is untouched. The CLI no longer requires a top-level runner for a
roles loop. Test: `test_override_vendor_applies_to_inherited_role`.

### 9. Cursor/Codex agent files match current runtime schemas — fixed

Verified against the runtime docs and corrected:

- **Cursor** → `.cursor/agents/<name>.md` (Markdown + YAML frontmatter, native
  `readonly`/`model`), not YAML.
- **Codex** → `.codex/agents/<name>.toml` with `name`/`description`/
  `developer_instructions` and `sandbox_mode = "read-only"`, not
  `instructions`/`read_only`/`tools`.
- **Claude** → `.claude/agents/<name>.md` (unchanged).

README and design doc updated. Tests parse the generated TOML/frontmatter.

### 10. Output symlinks/non-regular files rejected; safe promotion — fixed

Output verification (`BaseRunner.run`) and promotion (`outputs.promote_outputs`)
use `lstat`-based `is_safe_regular_file`, never follow symlinks, re-assert
worktree containment before reading, and copy via temp + atomic replace. A
symlink/dir at a declared output path fails the run.

### 11. Role names validated; stage-log path contained — fixed

Role names must match `ROLE_NAME_RE` (lowercase alphanumeric + hyphens), so a
`../`-laden name is a validation error; the stage-log path is additionally
asserted under the worktree (`_safe_stage_log`).

### 12. Aggregate runtime budget enforced across stages — fixed

The inter-stage loop tracks elapsed wall time, passes each stage only its
remaining `max_runtime`, and aborts before launching a stage that cannot fit.
Runtime/tokens/cost are aggregated into the pipeline result. Token/turn caps are
**not** enforced per stage because headless CLI output does not expose usage
telemetry yet — documented as a known limitation rather than silently ignored.

## Significant suggestions

- **13** Per-role adapter preflight now runs each role's `preflight` on its
  single-stage manifest (same object used to execute).
- **14** Deterministic aggregate status (`_aggregate_status`: fail > stalled >
  needs-approval > done) and per-stage records preserved in `RunResult.stages`
  and the pipeline log (status/verdict/exit/tokens/cost).
- **15** Role outputs participate in producer-collision and DAG analysis via
  `LoopManifest.effective_outputs()` (top-level duplicate detection stays on raw
  outputs).
- **16** Compiled destination = manifest role key (unique); duplicate
  destinations are refused in `write_compiled_agents`.
- **17** New `tests/test_cli_roles.py` exercises `run`/`deps check`/`apply` with
  a real roles manifest, reviewer mutation, and failed-promotion.
- **18** New/updated test signatures are fully typed; `_CALLS` uses a `TypedDict`
  (`CallRecord`).
- **19** `runners/cursor.py` docstring updated: the worktree-local staging +
  promotion model is the normal path; `--sandbox disabled --force` is only a
  fallback for out-of-worktree targets.

## Deferred (documented, not silently dropped)

- Full Git-worktree/diff code maker/checker execution (with L4).
- Per-stage token/turn budget enforcement (needs adapter usage telemetry).
- An opt-in runtime smoke test that a live Cursor/Codex actually discovers and
  spawns the compiled sub-agents (offline unit tests remain the default per
  `CONTRIBUTING.md`).
