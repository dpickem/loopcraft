# Milestone 3.5 Review 02 — Response

Response to [`2026_07_09_milestone_3_5_review_02_report.md`](2026_07_09_milestone_3_5_review_02_report.md),
the verification review on `feat/m3.5-multi-model`. Prior round:
[01 report](2026_07_09_milestone_3_5_review_01_report.md) /
[01 response](2026_07_09_milestone_3_5_review_01_response.md).

All 8 blocking findings (1–8) and all 7 significant findings (9–15) are
addressed.

## Commits

```text
c00ef10 fix: address M3.5 review 02 findings (verdict gating, TOCTOU, plan consistency)
```

Inspect with `git show c00ef10 -- <file>`.

## Status

```text
make test && make compile && make validate && make check
```

- `make test`: **410 passed** (up from 395), fully offline.
- `make compile` / `make validate` (3 manifests) / `make check`: pass.

New tests: `tests/test_outputs.py`; additions to `tests/test_orchestrator.py`
and `tests/test_cli_roles.py`.

## Blocking findings

### 1. Missing/conflicting reviewer verdict now fails — fixed

`_evaluate_verdict` requires exactly one structured `Verdict: PASS|FAIL` line:
zero matches is "missing", more than one is a "conflict". A read-only stage with
a `verify` rubric fails (`stage_status = FAILED`, problem recorded) on missing,
conflicting, or `FAIL` **before** promotion/aggregation, so `run` exits nonzero.
Tests: `test_cli_roles.py::test_run_roles_missing_verdict_fails`,
`::test_run_roles_reviewer_fail_stops_pipeline`.

### 2. Reviewer failure stops later stages — fixed

The pipeline now breaks on **any** non-`DONE` stage (`if stage_status != DONE:
break`), so a failed or FAIL-verdict reviewer halts the run and a later maker
never executes. Verified with a three-stage manifest whose `finisher` output is
asserted absent.

### 3. Intra-run verdict + read-only/tool policy — fixed

Intra-run now evaluates every read-only role's verdict from its own output after
the harness returns (a zero harness exit no longer implies checker PASS), and
preflight **rejects a read-only role under a Claude harness** (no native
read-only control); Cursor (`readonly`) and Codex (`sandbox_mode = "read-only"`)
are supported. Tests:
`test_orchestrator.py::test_preflight_intra_run_rejects_readonly_under_claude`.

### 4. Promotion TOCTOU/atomicity — fixed

`is_safe_regular_file` uses a single `os.lstat` + `S_ISREG`. `promote_outputs`
now (a) validates **all** bindings before replacing **any** destination, (b)
opens each source with `O_NOFOLLOW` and `fstat`s the descriptor (copying from
that fd, so a post-check swap cannot redirect the read), (c) copies through a
unique `mkstemp` temp in the destination directory then `os.replace`s, and (d)
re-asserts ledger containment. Tests: `test_outputs.py` (symlink source refused,
prevalidate-all leaves prior canonical value intact, skip-unproduced).

### 5. Intra-run model/provider compatibility — fixed

`_preflight_intra_run` runs the harness adapter's model-shape guard for each
role's model, and requires an explicit model when a role's vendor differs from a
Cursor harness. Test:
`test_orchestrator.py::test_preflight_intra_run_cross_provider_requires_model`.

### 6. Output ownership consistent across modes + fleet — fixed

`ExecutionPlan.effective_outputs` (union of actual per-stage ownership) now drives
intra-run bindings, inter-stage provenance, dry-run display, and the run
record's declared outputs. `manifest.effective_outputs()` excludes the top-level
set when every role declares its own outputs. Tests:
`test_cli_roles.py::test_run_roles_explicit_maker_output_replaces_top_level`,
`::test_dry_run_roles_uses_plan_with_override`.

### 7. Scope/handoff reconciled + persisted — fixed

`StageHandoff` is a Pydantic model written to `ledger/handoffs/<loop>/<run_id>/`
and reconstructed from disk for the next stage. The design M3.5 exit criterion
and README now describe the structured-artifact handoff and explicitly defer the
Git-diff maker/checker and a demonstrated live Cursor spawn to L4.

### 8. Aggregate budget from pipeline start — fixed

Remaining runtime is measured from a `time.monotonic` reading at pipeline start
(so control-plane overhead counts) with `math.ceil` to avoid truncation
false-exhaustion. The design/README name `max_turns`/`max_tokens` (need adapter
telemetry) and `max_consecutive_failures` (scheduler/store) as explicit
deferrals rather than silent gaps.

## Significant findings

- **9** — `StageRunResult` (typed) is persisted in `RunRecord.stages`; the CLI
  copies `result.stages` into the record. Test:
  `test_run_roles_records_durable_stage_results`.
- **10** — dry-run builds and consumes the normalized plan (per-role vendor
  honoring `--vendor`, effective outputs, read-only flag).
- **11** — `ExecutionPlan`/`RoleStage`/`StageHandoff`/`HandoffOutput`/
  `StageRunResult` are Pydantic; `RunResult.stages` is typed; new test
  signatures drop `# noqa: ANN001`.
- **12** — role tools are documented as **policy validation** (not runtime
  enforcement) with `READ`/`EXECUTE`/`WRITE` classes; a read-only reviewer may
  declare `agent-spawn`/`test-run`; connectors (`nv-tools.*`) are recognized and
  not auto-classified as writing (unblocking the `nv-tools.gitlab` reviewer).
- **13** — `run_multi_model` fails closed when planning returns problems. Test:
  `test_run_multi_model_fails_closed_on_planning_error`.
- **14** — prior-stage stdout is fenced and labelled UNTRUSTED DATA in the
  handoff prompt. Test: `test_handoff_render_marks_stdout_untrusted`.
- **15** — `CursorRunner.preflight` docstring updated to the staged-output model.

## Deferred (documented, not silently dropped)

- Git-worktree/diff code maker/checker and a live Cursor cross-provider
  spawn demonstration (with L4). The compiler/spawn path is schema-tested only.
- `max_turns`/`max_tokens` enforcement (needs adapter usage telemetry) and
  `max_consecutive_failures` (scheduler/store boundary).
- Runtime-native per-tool allowlist enforcement (tools are policy-validated
  today).
