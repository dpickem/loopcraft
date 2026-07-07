# Milestone 3 Review 02 — Response

Response to [`2026_07_07_milestone_3_review_02_report.md`](2026_07_07_milestone_3_review_02_report.md),
the adapter-contract follow-up on `feat/m3-adapters`. Prior history:
[01 report](2026_07_07_milestone_3_review_01_report.md) /
[01 response](2026_07_07_milestone_3_review_01_response.md).

Both findings are addressed.

## Commits

```text
b55803b fix: address M3 review 02 findings (cursor adapter contract)
```

Inspect with `git show b55803b -- <file>`.

## Status

```text
make test && make compile && make validate
```

- `make test`: **360 passed** (up from 359 at review time), fully offline.
- `make compile`: passes.
- `make validate`: passes, 3 manifests.

## Findings

### 1. Cursor no longer silently accepts loops it can't produce outputs for — fixed

The Cursor adapter has no equivalent of Codex/Claude's `--add-dir`, so it cannot
grant write access to a loop's declared ledger outputs (which resolve into the
memory tree, outside the per-run worktree). Rather than let a Cursor run start
and then fail to write those files, `CursorRunner.preflight` now reports any loop
that declares `state/...` (ledger) outputs as **unsupported for Cursor in M3**:

- `is_state_path` identifies the declared ledger outputs; when present, preflight
  adds `cursor adapter (M3) cannot grant write access to ledger outputs outside
  the run worktree: [...]; use codex/claude for output-producing loops`.
- So `apply` / `deps check --loop` fail closed for a Cursor loop with ledger
  outputs (honest, at deploy time) instead of failing at the scheduled run.
- A Cursor loop with no declared outputs (or non-ledger targets) still passes.

I chose "fail preflight + document" over inventing a `cursor-agent` writable-root
flag, since the CLI's file-access model here is workspace/trust-based and a
speculative flag could be wrong. When a correct Cursor access model exists it can
grant `self.writable_roots(ctx)` like the other adapters and this preflight guard
is lifted.

- Files: `src/loopcraft/runners/cursor.py`.
- Tests: `tests/test_runner.py::test_cursor_flags_ledger_outputs` (a `state/...`
  output loop is rejected), and `::test_cursor_accepts_any_model` updated to a
  no-output manifest so it isolates the model-permissiveness behavior.

### 2. Shipped M3 scope is now stated, not overclaimed — fixed

The M3 design exit criterion mentions a Cursor loop spawning a cross-provider
sub-agent; the shipped adapters run a single headless invocation with no
sub-agent/role machinery. Instead of leaving the docs broader than the code, the
README now states the shipped scope explicitly:

- Codex and Claude are **full** adapters (grant declared ledger-output dirs via
  `--add-dir`, so a loop runs unchanged on either).
- The Cursor adapter is **limited** (no writable-root grant → ledger-output loops
  unsupported, per finding 1).
- Cross-provider **sub-agents** and per-role multi-model **compilation** are
  **deferred to M3.5**; today each loop is one headless invocation.

- Files: `README.md` (shipped-adapter-scope note in *Runtime portability*).

## Notes on healthy areas

Review 02 confirmed the Review 01 fixes are in place (invalid-default `vendor
get/list` returns nonzero; model checks documented as shape guards) and that the
new modules follow the repo conventions and share the prompt path — all unchanged
here. The full offline suite passes.
