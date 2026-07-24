# Milestone 3.5 Review 03 — Response Verification

## 1. Scope and evidence

Review target: local branch `feat/m3.5-multi-model` in
`/Users/dpickem/workspace/loopcraft`.

Reviewed state:

- Review 02 baseline: `45e99b6`
- Review 02 fix: `c00ef10`
- Review 02 response: `ca63923`
- Live Cursor test / HEAD: `92005e9`
- Merge base: `2ac7ca0` (`main`)
- Working tree was clean before this report was created.

References:

- `docs/review_notes/2026_07_09_milestone_3_5_review_02_report.md`
- `docs/review_notes/2026_07_09_milestone_3_5_review_02_response.md`
- M3.5 and cross-cutting contracts in
  `docs/loopcraft-implementation-design.html`
- `CONTRIBUTING.md`
- Current implementation, tests, README, and commit messages.

Verification:

```text
make test && make compile && make validate && make check
git diff --check main...HEAD
git diff --check
```

Results:

- 410 offline tests passed; 1 live Cursor test skipped by default.
- Source/tests byte-compilation passed.
- All 3 checked-in manifests validated.
- Dependency check and apply dry-run passed.
- Diff whitespace checks and IDE diagnostics were clean.

Three independent review passes covered requirements/claims,
correctness/security, and tests/API standards. A separate synthesis pass
deduplicated and rechecked their findings against current workspace lines.

Limitations:

- The paid live Cursor test was not rerun during this review.
- No live Claude/Codex integration was invoked.
- A historical `taskToolCall` is asserted by the new test, but the repository
  does not preserve the live run evidence that produced commit `92005e9`.

## 2. Executive summary

The branch is substantially better. Review 02 fixes real defects in
inter-stage verdict gating, stop-on-failure, typed/durable stage records,
dry-run planning, final-component symlink checks, native agent schemas, and
basic handoff persistence.

The response nevertheless overstates closure. Current HEAD still has
blocker-level gaps in:

1. transaction-wide output publication and path-race resistance;
2. enforceable read-only review;
3. attributable intra-run reviewer execution/verdicts;
4. fleet output ownership and DAG consistency;
5. Cursor role provider/model validation;
6. sanctioned and atomic handoff persistence;
7. preflight-to-execution plan consistency;
8. the claimed hard aggregate runtime limit.

The live Cursor test is useful evidence that Cursor emitted a task call naming
the compiled reviewer and requested model. It does not prove successful
sub-agent completion, reviewer PASS, read-only behavior, or the complete
Loopcraft manifest/preflight/orchestration path. README/design wording that the
full M3.5 cross-provider criterion is thereby satisfied is too strong.

Overall verdict: **FAIL — not merge-ready as M3.5-complete.**

## 3. Claim/requirements status

### Review 02 findings

1. **Verdict missing/conflict/FAIL gating — Fixed for the original semantic
   failure.** Inter-stage and intra-run now reject missing, duplicate, or FAIL
   verdict text before promotion. Intra-run attribution remains a separate
   blocker (Finding 3).

2. **Reviewer failure stops later stages — Fixed.** The inter-stage pipeline
   breaks on every non-`DONE` stage.

3. **Intra-run verdict/read-only policy — Partial.** Claude readonly intra-run
   is rejected and Cursor/Codex compile native readonly metadata. The harness
   still supplies no per-role completion receipt, and shared stdout can satisfy
   an outputless reviewer.

4. **Promotion TOCTOU/atomicity — Partial.** Final-component symlinks, fixed temp
   names, and prevalidation of static unsafe outputs are addressed. Ancestor
   path races, destination-parent races, and multi-output partial publication
   remain.

5. **Intra-run provider/model compatibility — Partial.** Non-Cursor harnesses
   run model-shape checks and Cursor requires a model for a declared
   cross-provider role. Cursor remains permissive and never validates that the
   model family agrees with `role.vendor`.

6. **Unified output ownership — Partial.** ExecutionPlan now drives dry-run and
   execution. Fleet `manifest.effective_outputs()` still treats every
   outputless role as inheriting top-level outputs, including readonly
   reviewers that never inherit them.

7. **Scope/handoff reconciliation — Partial.** The milestone now consistently
   defers Git-diff execution and persists a structured handoff. The handoff
   bypasses `Store`, is non-atomic, and embeds maker stdout in the next prompt.

8. **Aggregate runtime budget — Partial.** Timing starts at pipeline entry, but
   `ceil()` can extend the subprocess allowance and no final deadline check
   covers verdict parsing, publication, handoff persistence, or logging.

9. **Durable typed stage records — Fixed.** `StageRunResult` reaches
   `RunResult`, `RunRecord`, and run-record serialization.

10. **Dry-run normalized plan — Fixed.** Role vendors, readonly policy, and
    effective outputs derive from the execution plan.

11. **Typing/Pydantic compliance — Regressed after being fixed.** Core plan and
    handoff structures are now typed Pydantic models, but
    `tests/test_cursor_live.py` introduced unparameterized `list[dict]`.

12. **Role tools — Partial/documented deferral.** The code now honestly calls
    this policy validation, recognizes reviewer orchestration/test tools, and no
    longer rejects all connectors. Runtime-native operation allowlists remain
    unenforced while the design still describes tools as what a role may touch.

13. **Direct execution fails closed on planning errors — Fixed for a caller that
    supplies no plan.** A separate preflight/execution TOCTOU remains because
    the CLI rebuilds and trusts a plan after preflight (Finding 7).

14. **Handoff prompt injection — Partial.** The stdout is labelled untrusted and
    fenced, but the static delimiter is attacker-reproducible and the text is
    still concatenated into the instruction channel.

15. **Cursor preflight documentation — Fixed.**

### M3.5 requirements

- Roles schema and agent compiler: **met**
- Inter-stage ordering across adapters: **met**
- Structured ledger handoff: **partial**
- Reviewer verdict gates inter-stage: **met**
- Reviewer cannot mutate protected state: **not met**
- Consistent output ownership / fleet DAG: **not met**
- Ledger-writing Cursor mechanism: **implemented and offline-tested**
- Cursor cross-provider sub-agent spawn: **partial evidence, not full
  Loopcraft-path proof**
- Hard aggregate runtime budget: **partial**
- Full Git-diff maker/checker: **explicitly deferred to L4**

## 4. Blockers

### 1. Output publication is still raceable and not transaction-wide

Relevant code:

- `src/loopcraft/outputs.py:104-126`
- `src/loopcraft/outputs.py:163-199`
- `src/loopcraft/orchestrator.py:562-594`
- `src/loopcraft/cli.py:489-503`

What is fixed:

- `lstat` rejects a static final-component symlink.
- `O_NOFOLLOW` plus `fstat` validates the opened final source inode.
- temp names are unique.
- static unsafe bindings are found before replacement.

Remaining mechanism:

- `O_NOFOLLOW` protects only the final component. A background process can swap
  an ancestor of `write_path` after the containment check and before `os.open`.
- Ledger containment is checked before parent creation, `mkstemp`, and
  `os.replace`; replaced ancestor directories are not opened through trusted
  directory descriptors.
- Sources are opened and destinations replaced sequentially. If output 1 is
  replaced and output 2 is swapped, unreadable, full-disk, or otherwise fails,
  canonical state is mixed old/new.
- Inter-stage output promotion happens before `_persist_handoff`. A handoff
  write failure bubbles into the CLI's execution-failure path, which records
  `outputs=[]` even though ledger outputs were already replaced.

Impact:

- A child process can redirect reads through a swapped ancestor.
- Concurrent runs or filesystem failure can leave partial canonical state.
- Run history can claim no output while the ledger was mutated.

Required fix:

- Open/traverse source and destination ancestors through trusted directory
  descriptors with no-follow semantics.
- Open and stage every source/destination before replacing any canonical path.
- Commit the set with rollback/version-pointer semantics, or explicitly abandon
  transaction-wide claims and make downstream visibility atomic.
- Persist the handoff before publication or include handoff + outputs in one
  sanctioned transaction.

Required tests:

- source-ancestor and destination-parent swaps;
- concurrent promotion to the same output;
- failure opening/copying/replacing output 2 after output 1 is valid;
- handoff-persistence failure after successful copies.

### 2. Read-only review is mutation detection, not an enforced boundary

Relevant code:

- `src/loopcraft/orchestrator.py:379-404`
- `src/loopcraft/orchestrator.py:531-545`
- `src/loopcraft/runners/claude.py:61-81`
- `docs/loopcraft-implementation-design.html:1512-1513`

The inter-stage check hashes only regular files that already existed under the
scratch worktree. It does not detect:

- newly created files consumed by later stages;
- metadata or symlink mutations;
- writes to the source tree or ledger outside the worktree;
- detached child-process writes after the snapshot check;
- remote connector mutations.

Claude inter-stage still runs in `acceptEdits` mode, and role tool declarations
are validation labels rather than runtime permissions. A checker can therefore
affect later work while passing the current hash comparison.

This is weaker than M3.5's exit criterion that the reviewer cannot mutate
protected state.

Required fix:

- run readonly roles in an OS/runtime-enforced sandbox;
- mount source, maker outputs, and ledger readonly, exposing only declared
  review outputs as writable;
- compare complete before/after inventories as defense in depth;
- enforce operation-level connector policy or reject unsupported combinations.

Required tests:

- create a new worktree source/config file;
- write source and ledger paths;
- mutate symlinks/metadata;
- leave a background child;
- attempt a mocked remote write.

### 3. Intra-run PASS is not attributable to the reviewer sub-agent

Relevant code:

- `src/loopcraft/orchestrator.py:358-371`
- `src/loopcraft/orchestrator.py:613-660`
- `tests/test_cursor_live.py:87-96`

For each readonly role, intra-run scans that role's bound output or falls back
to the shared harness log. It does not require evidence that the named reviewer
was spawned or completed. The parent harness can emit `Verdict: PASS`; multiple
outputless reviewers can all accept the same line.

The live test confirms only that a `taskToolCall` event requested the `reviewer`
name/model. It does not connect that event to Loopcraft's verdict, output, or
readonly checks.

Impact:

- The maker/parent can grade itself.
- A skipped or failed reviewer can be reported as PASS.
- Intra-run lacks the independent checker guarantee that motivates M3.5.

Required fix:

- require one typed runtime receipt per role containing role identity, effective
  model/provider, completion status, output reference, and verdict;
- reject missing, duplicate, or parent-authored receipts;
- preserve per-role intra-run stage records.

Required tests:

- parent-spoofed PASS;
- reviewer not spawned;
- reviewer task started but failed;
- two outputless reviewers;
- role/model mismatch between receipt and plan.

### 4. Fleet output ownership still disagrees with execution

Relevant code:

- `src/loopcraft/manifest.py:342-364`
- `src/loopcraft/orchestrator.py:149-157`

`manifest.effective_outputs()` includes top-level outputs whenever any role has
no explicit outputs. It cannot tell that a readonly reviewer with no outputs
inherits nothing.

Example:

- top-level output `A`;
- maker explicitly owns `B`;
- readonly reviewer has no output.

Execution produces only `B`; fleet analysis advertises `A` and `B`. This creates
phantom producer edges and false collisions.

Required fix:

- make readonly/ownership available to fleet validation without loading
  arbitrary runtime state, or run fleet analysis through the same resolved
  ownership planner;
- test explicit maker outputs plus an outputless readonly reviewer.

### 5. Cursor role vendor/model agreement is not validated

Relevant code:

- `src/loopcraft/orchestrator.py:690-721`
- `src/loopcraft/runners/cursor.py:31-49`
- `src/loopcraft/agent_compiler.py:82-100`

Cursor's adapter intentionally accepts every model string. Intra-run preflight
therefore allows `vendor: claude` with a GPT model (or the inverse). The
compiled Cursor agent carries only the model, so the declared role vendor has no
effect beyond the current "explicit model exists" check.

Impact:

- provider separation can silently differ from the manifest;
- tests and review records can claim an independent provider that was not used.

Required fix:

- validate known model families against `role.vendor`;
- require runtime receipts confirming the effective provider/model;
- reject or clearly mark model ids whose provider cannot be established.

### 6. Handoff persistence bypasses `Store` and remains injection-prone

Relevant code:

- `src/loopcraft/orchestrator.py:97-117`
- `src/loopcraft/orchestrator.py:309-323`
- `CONTRIBUTING.md:102-105`

`_persist_handoff` directly creates directories and calls `write_text` inside
the ledger. This violates the repository rule that state changes use
`loopcraft.store` and omits Store-level atomicity.

The persisted stdout is then concatenated into the next role's prompt behind a
static delimiter. A maker can include that delimiter and append instructions.
The "UNTRUSTED DATA" warning is useful, but it is advisory rather than a
separate data channel.

Required fix:

- add typed, atomic handoff methods to `Store`;
- pass handoff references/typed fields through a data channel instead of raw
  stdout in the instruction stream;
- test interrupted writes, reconstruction through Store, and delimiter
  injection.

### 7. Preflight and execution do not consume one immutable validated plan

Relevant code:

- `src/loopcraft/cli.py:460-474`
- `src/loopcraft/orchestrator.py:771-808`

CLI preflight validates a plan internally but does not return it. After staging,
execution rebuilds a new plan, discards its planning problems, and passes it to
`run_multi_model`, which trusts any supplied plan as "already preflighted."

An agent definition changed or removed between preflight and execution can
alter instructions, readonly policy, tools, model, or ownership without
revalidation.

Required fix:

- have preflight return the exact immutable plan execution consumes; or
- rebuild and fully revalidate immediately before invocation, refusing any new
  problem;
- test mutation/removal of an agent definition between phases.

### 8. The aggregate runtime limit is not hard

Relevant code:

- `src/loopcraft/orchestrator.py:424-441`
- `src/loopcraft/orchestrator.py:500-505`
- `src/loopcraft/orchestrator.py:562-601`

`math.ceil` can grant almost one extra second to a stage. After the subprocess
returns, no deadline check covers readonly hashing, verdict parsing, output
promotion, handoff persistence, or pipeline logging.

The implementation can therefore return `done` after exceeding the declared
aggregate runtime.

Required fix:

- carry an absolute monotonic deadline;
- support subsecond runner timeouts or fail before starting when the remaining
  whole-second allowance is insufficient;
- check the deadline before and after every publication/finalization phase;
- test fractional remainder and slow promotion/handoff.

### 9. New live-test code violates binding contribution rules

Relevant code:

- `tests/test_cursor_live.py:39-47`
- `tests/test_cursor_live.py:72-96`
- `CONTRIBUTING.md:17-20`
- `CONTRIBUTING.md:140-152`

The opt-in test is skipped by default, preserving offline unit tests. However:

- any nonempty value, including `LOOPCRAFT_LIVE_CURSOR=0`, enables it;
- it skips for a missing binary but not missing authentication;
- `_task_spawns` returns unparameterized `list[dict]`;
- it never asserts `completed.returncode`, task completion, reviewer output, or
  final PASS;
- "cross-provider" is asserted only by string inequality, so user overrides can
  select two models from the same provider;
- it invokes a paid runtime with `--force`.

`CONTRIBUTING.md` requires fully typed signatures and opt-in integration tests
that auto-skip when credentials/runtime are missing. Under the review policy,
these are blockers.

Required fix:

- require exact `LOOPCRAFT_LIVE_CURSOR=1`;
- add an authentication preflight/skip;
- type the parsed event structures;
- assert successful process/task completion and expected reviewer result;
- validate provider families, not only unequal strings;
- avoid `--force` unless justified by the test's isolated sandbox.

## 5. Suggestions and nits

### Response and live-claim wording

The Review 02 response predates `92005e9` and still says live Cursor spawn is
deferred/schema-tested only, while README/design now say "verified live." Update
the response/index or add a follow-up note so the branch's documentation does
not contradict itself.

The current smoke test provides useful spawn-request evidence but does not prove
the complete M3.5 path. Prefer wording such as:

> A live Cursor run emitted a structured task call for the compiled reviewer on
> the requested Opus model. Full Loopcraft orchestration, completion, verdict,
> and readonly guarantees remain covered separately/not yet live-demonstrated.

### Verdict parsing

`src/loopcraft/orchestrator.py:74-75` accepts quoted/list-prefixed verdicts and
optional separators such as `Verdict PASS`, although comments require one
structured `Verdict: PASS|FAIL` line. Use a typed result artifact or a strict
final-line grammar.

### Failed run-record contracts

`src/loopcraft/cli.py:557-594` records raw `manifest.outputs` for preflight and
early execution failures, while successful role runs record
`plan.effective_outputs`. Preserve the same effective declared contract on
every outcome.

### Durable observability

`StageRunResult.log_path` points into prunable worktrees. Either persist stage
logs as durable artifacts or avoid long-lived run records containing dangling
paths.

### Tool policy

`src/loopcraft/role_tools.py` now accurately documents policy validation rather
than runtime enforcement. The design still says `tools` are what a role may
touch. Reconcile the design or implement adapter-native/operation-level
allowlists.

### File organization

Some new modules place public models/functions before private helpers, contrary
to `CONTRIBUTING.md` section 14. This is lower severity than the behavioral
issues but should be corrected before claiming full standards compliance.

## 6. Merge gate and verdict

Before merge:

1. close the publication races/partial-commit paths;
2. enforce readonly at the runtime/OS boundary;
3. require attributable intra-run role receipts;
4. unify fleet and execution ownership;
5. validate Cursor role provider/model bindings;
6. route atomic handoffs through `Store` and remove raw stdout from the
   instruction channel;
7. execute the same validated plan that preflight approved;
8. enforce the runtime deadline through final publication;
9. make the live test typed, auth-aware, exact-opt-in, and completion-verifying.

Re-run all offline checks and the improved opt-in Cursor test, then record the
runtime/version and durable evidence.

**Verdict: FAIL.**
