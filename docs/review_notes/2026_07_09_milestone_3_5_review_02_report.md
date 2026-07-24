# Milestone 3.5 Review 02 — Response Verification

Review target: local branch `feat/m3.5-multi-model` in
`/Users/dpickem/workspace/loopcraft`.

Reviewed state:

- Base implementation: `5a78611 feat: add M3.5 multi-model loops (roles, agent compiler, orchestration)`
- Fix commit: `0a75645 fix: address M3.5 review 01 findings (multi-model safety + control-plane wiring)`
- Response commit / HEAD: `45e99b6 docs: add M3.5 review 01 response and index entry`
- Working tree was clean before this report was created.

References:

- `docs/review_notes/2026_07_09_milestone_3_5_review_01_report.md`
- `docs/review_notes/2026_07_09_milestone_3_5_review_01_response.md`
- `docs/loopcraft-implementation-design.html`
- `CONTRIBUTING.md`
- Current Cursor and Codex sub-agent format documentation linked by the
  compiler.

## Executive Summary

The response materially improves the branch. Shared multi-model preflight,
current Cursor/Codex file formats, role-name containment, failure-gated
promotion, inter-stage adapter preflight, and several CLI-level regression tests
are real fixes.

The claim that all 12 blockers and all 7 suggestions are addressed is not
accurate. Several fixes are partial, and some response claims are contradicted
by the current execution paths:

1. A reviewer with a required verify rubric can omit its verdict and the
   pipeline still returns `done`.
2. A failed reviewer or `Verdict: FAIL` does not stop later stages.
3. Intra-run execution does not evaluate reviewer verdicts and does not enforce
   the read-only/tool contract on all supported harnesses.
4. Role tools are classified at preflight but are not mapped into runtime
   allowlists, so they do not actually govern tool access.
5. Output promotion still has a source-symlink TOCTOU race despite claiming
   never to follow symlinks.
6. Output ownership remains mode-dependent when a maker declares role-specific
   outputs.
7. Intra-run preflight checks the harness binary but not harness/role model
   compatibility.
8. The design's M3.5 exit criterion still requires a clean diff handoff, while
   another design paragraph and README defer it; the implementation's
   `StageHandoff` is in-memory, not an artifact persisted through the ledger.
9. Only aggregate subprocess runtime is enforced. The design's hard
   turns/tokens/consecutive-failure caps remain unenforced.

Verdict: **not merge-ready as M3.5-complete**. The remaining blockers are
smaller and more localized than in Review 01, but they affect correctness,
security boundaries, and explicit design contracts.

## Verification

Commands run from `/Users/dpickem/workspace/loopcraft`:

```text
make test && make compile && make validate && make check
git diff --check main...HEAD
git diff --check 5a78611..HEAD
```

Results:

- 395 tests passed.
- Source and tests byte-compiled.
- All 3 checked-in manifests validated.
- Dependency check and apply dry-run passed.
- Diff whitespace checks passed.
- IDE diagnostics reported no errors in the reviewed source/test files.

No paid/live Cursor, Codex, or Claude sub-agent run was performed. The current
suite still does not demonstrate the Cursor cross-provider spawn exit criterion.

## Response Claim Verification

### Original blockers

1. **Shared deployment/dependency preflight — Fixed.**
   `deploy.resolve_preflight` is used by run, apply, and deps-check paths, and
   roles loops dispatch through `preflight_multi_model`.

2. **Mode-correct preflight — Partially fixed.**
   Inter-stage now preflights each role adapter and intra-run checks the harness
   binary. Intra-run still does not call the harness runner's model preflight or
   validate role models against the harness/provider contract (Finding 5).

3. **Read-only boundary — Partially fixed.**
   Inter-stage post-run hashing detects persistent changes to pre-existing
   regular files in the scratch worktree. It does not cover source/ledger paths
   outside that worktree, and intra-run has no equivalent attribution check;
   Claude intra-run remains prompt-only (Finding 2).

4. **Verify and tools govern runs — Not fully fixed.**
   Verify text is now included, but a missing required verdict does not fail the
   result and intra-run verdicts are not parsed. Tools are classified, not
   enforced as runtime allowlists (Findings 1-3).

5. **Failed outputs are not promoted — Partially fixed.**
   Process/status gating is fixed. Promotion's symlink check remains raceable,
   and semantic reviewer failure is evaluated only after promotion (Finding 4).

6. **Structured handoff / Git-diff scope — Partially scoped, internally inconsistent.**
   A structured in-memory handoff exists, but it is not stored as a ledger
   artifact. The milestone exit criterion still requires a clean diff handoff
   even though another paragraph now defers it (Finding 7).

7. **Normalized ownership plan — Not fixed for intra-run.**
   Inter-stage consumes per-stage ownership. Intra-run still binds all
   top-level outputs plus role-owned outputs, even when an explicit maker output
   replaces the top-level set under the documented rule (Finding 6).

8. **`--vendor` propagation — Execution fixed; dry-run remains wrong.**
   Preflight and execution honor the override. Dry-run resolves role vendors
   independently from the execution plan and displays inherited roles using the
   configured default (Finding 10).

9. **Current runtime agent schemas — Fixed at the file-schema level.**
   Cursor uses Markdown/YAML frontmatter and Codex uses
   `developer_instructions` plus `sandbox_mode`. Live runtime discovery remains
   untested.

10. **Safe output files / promotion — Partially fixed.**
    Symlinks and non-regular files are rejected in ordinary checks, and
    destination replacement is atomic. The source is checked and copied in
    separate pathname operations, leaving a TOCTOU path (Finding 4).

11. **Role name and stage-log containment — Fixed.**
    Role names use a safe vocabulary and generated stage-log paths are
    containment-checked.

12. **Aggregate hard budget — Partially fixed.**
    Remaining subprocess runtime is capped per stage and usage is summed.
    Turns, tokens, and consecutive failures are not enforced; orchestration
    overhead is outside the measured elapsed value (Finding 8).

### Original significant suggestions

13. **Per-role adapter preflight — Fixed for inter-stage; partial for intra-run.**
    Inter-stage uses each adapter's real preflight. Intra-run only
    compile-validates files and misses model/provider compatibility.

14. **Status and metrics — Partially fixed.**
    Aggregate precedence and in-memory stage records exist. Failure/missing
    verdict semantics are wrong, later stages can run after reviewer failure,
    and `RunResult.stages` is not copied into the durable run record
    (Findings 1 and 9).

15. **Role outputs in DAG/collision analysis — Partially fixed.**
    `effective_outputs()` includes role outputs, but also includes top-level
    outputs that no stage owns when a maker explicitly overrides its outputs.
    Fleet/DAG claims can therefore disagree with inter-stage execution
    (Finding 6).

16. **Compiled-name collisions — Fixed.**
    Manifest role keys are canonical compiled names and duplicate destinations
    are rejected.

17. **CLI-level negative tests — Partially fixed.**
    Useful run/apply/deps, mutation, and failed-maker tests were added. Missing
    cases include verdict omission/FAIL, later-stage continuation, intra-run
    semantics, role-specific maker outputs, TOCTOU-safe promotion, and
    model-incompatible intra-run plans.

18. **Typing compliance — Not fixed.**
    New production and test code still contains unparameterized `dict` shapes,
    untyped signatures suppressed with `# noqa: ANN001`, and dataclasses for
    structured execution/handoff models despite `CONTRIBUTING.md` preferring
    Pydantic (Finding 11).

19. **Cursor security documentation — Partially fixed.**
    The module docstring distinguishes the normal staged-output path from the
    exceptional coarse external-root grant. The `preflight` method docstring
    still describes direct ledger-output write grants.

## Blocking Findings

### 1. A missing required reviewer verdict still reports pipeline success

Relevant code:

- `src/loopcraft/orchestrator.py:451-461`
- `src/loopcraft/orchestrator.py:485-495`

When a read-only stage has a verify rubric but `_stage_verdict` returns `None`,
the orchestrator appends a problem:

```text
reviewer did not emit an explicit PASS/FAIL verdict
```

It does not change `stage_status`, set `reviewer_failed`, or otherwise affect
`_aggregate_status`. If the runner returned `done`, the aggregate result is
still `done`; `loopctl run` exits zero while printing a problem.

This directly contradicts the response's statement that a missing verdict is a
failure and the design's verifiable checker contract.

The parser also accepts the first matching verdict anywhere in free-form text.
If the reviewer quotes `Verdict: PASS` and later emits its real
`Verdict: FAIL`, the pipeline accepts PASS. Multiple/ambiguous verdicts are not
rejected.

Recommended fix:

- Require exactly one structured final verdict and treat missing, malformed, or
  conflicting verdicts as semantic failure before promotion/status aggregation.
- Add CLI tests for missing, malformed, duplicate/conflicting, and explicit
  FAIL verdicts.

### 2. Reviewer failures do not stop subsequent stages

Relevant code:

- `src/loopcraft/orchestrator.py:451-483`

The only early-stop condition is:

```text
if stage_status != DONE and not stage.readonly: break
```

A failed read-only stage therefore allows the next role to run. An explicit
`Verdict: FAIL` is even weaker: it sets only the pipeline-level
`reviewer_failed` flag, leaving the stage status `done`, so later mutating roles
continue.

The manifest supports two or more ordered roles, not exactly two. Running a
later maker after the checker has failed is unsafe and violates checker-gated
composition.

Recommended fix:

- Stop on every non-success stage unless the schema explicitly declares a
  continue-on-failure policy.
- Stop immediately on reviewer FAIL or missing required verdict.
- Add a three-stage regression test proving the last stage is not invoked.

### 3. Intra-run does not enforce reviewer verdict or tool/read-only policy consistently

Relevant code:

- `src/loopcraft/orchestrator.py:498-532`
- `src/loopcraft/agent_compiler.py:82-100`
- `src/loopcraft/role_tools.py:41-69`

The intra-run path compiles agents, invokes one harness, and accepts the harness
result based on process/output status. It never parses a review output for
PASS/FAIL. A reviewer can emit `Verdict: FAIL` while the harness exits zero and
the pipeline returns `done`.

Read-only enforcement is runtime-dependent:

- Cursor receives native `readonly`.
- Codex receives `sandbox_mode = "read-only"`.
- Claude receives only prompt prose; no native read-only policy is configured.
- The control plane cannot use the inter-stage hash check to distinguish maker
  changes from reviewer changes inside one harness.

Until the contract is enforceable, unsupported harness/role combinations
should fail preflight rather than be advertised as equivalent.

Recommended fix:

- Require a structured harness result containing per-role execution receipts
  and reviewer verdict, then validate it before promotion.
- Add an enforceable Claude policy or reject readonly intra-run roles under a
  Claude harness.
- Add intra-run FAIL, missing verdict, and mutation tests.

### 4. Output promotion still has a source-symlink TOCTOU vulnerability

Relevant code:

- `src/loopcraft/outputs.py:40-50`
- `src/loopcraft/outputs.py:121-140`
- `src/loopcraft/runners/base.py:297-316`

`is_safe_regular_file` claims to use `lstat`, but actually performs separate
`is_file()` and `is_symlink()` pathname checks. `promote_outputs` then resolves
and checks the path before calling `shutil.copyfile`, which follows source
symlinks by default.

A runtime can leave a background process that swaps the checked regular file
for a symlink between validation and copy. The higher-privilege control-plane
process then copies the link target into the ledger. The fixed
`<name>.loopcraft.tmp` destination also permits concurrent promotions of the
same ledger path to interfere. Contrary to the inline comment, `copyfile`
follows an existing destination symlink too, so a planted temporary-name
symlink can redirect the pre-replace write outside the ledger. The destination
path/parent is not re-asserted under the ledger immediately before writing, so
a swapped parent-directory symlink is another redirection path.

Promotion is only atomic per file, not transaction-wide as the response claims.
Bindings are validated and replaced sequentially; if a later output is unsafe
or its copy fails, earlier canonical outputs have already been replaced. A
multi-output run can therefore publish mixed old/new state.

Recommended fix:

- Open the source with no-follow semantics (`O_NOFOLLOW` where available), then
  `fstat` the opened descriptor and copy from that descriptor.
- Use a unique same-directory temporary file created with exclusive creation.
- Revalidate/open the destination directory without following a replaced
  symlink before creating the temporary file.
- Prevalidate every binding before replacing any destination, and provide
  rollback or document per-file rather than transactional semantics.
- Keep the atomic `os.replace`, and add adversarial swap, concurrency, and
  later-binding-failure tests.

### 5. Intra-run preflight does not validate model/provider compatibility

Relevant code:

- `src/loopcraft/orchestrator.py:613-638`
- `src/loopcraft/agent_compiler.py:103-132`

`_preflight_intra_run` checks only the harness adapter/binary and whether each
agent can be rendered. Rendering accepts any model string. It does not call the
harness runner's model preflight.

Examples that can pass:

- Codex harness with a role model `opus`.
- Claude harness with a role model `gpt-*`.
- Cursor harness with `vendor: claude` and no role model; the vendor binding is
  not represented in the compiled agent, so the role inherits the parent model.

Recommended fix:

- Add a harness-specific sub-agent model/provider validation API.
- Require an explicit compatible model when a role vendor differs from the
  Cursor harness/provider.
- Test wrong-provider models and provider-only bindings without models.

### 6. Output ownership still differs between execution modes and fleet metadata

Relevant code:

- `src/loopcraft/orchestrator.py:168-176`
- `src/loopcraft/orchestrator.py:514-520`
- `src/loopcraft/manifest.py:342-356`
- `src/loopcraft/cli.py:318-351`

The documented ownership rule says a maker's explicit role outputs replace its
inheritance of top-level outputs. Inter-stage follows that rule. Intra-run
unconditionally binds `manifest.outputs` and then adds role-owned outputs.
`effective_outputs()` and dry-run likewise always report top-level outputs.

For a manifest with top-level `A` and explicit maker output `B`:

- inter-stage requires/produces `B`;
- intra-run requires `A` and `B`;
- fleet/DAG metadata says the loop produces `A` and `B`;
- dry-run displays `A` as the main resolved contract.

This is the same mode drift Review 01 Finding 7 asked the normalized plan to
eliminate.

Recommended fix:

- Put one deduplicated `effective_outputs` list on `ExecutionPlan`, derived only
  from actual stage ownership.
- Use it for intra-run bindings, inter-stage provenance, dry-run, collision/DAG
  analysis, and run-record declared outputs.
- Add explicit-maker-output tests for both modes and dry-run.

### 7. M3.5 scope and handoff documentation remain contradictory

Relevant documentation:

- `docs/loopcraft-implementation-design.html:799`
- `docs/loopcraft-implementation-design.html:1507-1513`
- `README.md:155-160`
- `src/loopcraft/orchestrator.py:227-246`

The newly edited design paragraph says Git-diff code maker/checker execution is
deferred to L4. The M3.5 milestone exit criterion still says the maker/checker
"hands the diff between stages cleanly," and its risk still names lossless
context/diff passing.

The implementation's `StageHandoff` is also an in-memory dataclass rendered
directly into the next prompt. Outputs are promoted to ledger paths, but the
handoff artifact itself (status, digests, stdout) is not written through the
memory ledger as the design paragraph claims.

Recommended fix:

- Make one explicit scope decision and update all design/README milestone
  statements consistently.
- If structured handoff is the M3.5 deliverable, persist a typed handoff record
  under the run ledger and test reconstruction from it.
- Do not mark the clean-diff or Cursor live-spawn exit criteria met without an
  implementation/demonstration.

### 8. The design's hard budget contract remains only partially enforced

Relevant code/docs:

- `src/loopcraft/orchestrator.py:379-430`
- `README.md:155-160`
- `docs/loopcraft-implementation-design.html:735-739`
- `docs/loopcraft-implementation-design.html:774`

The fix tracks time spent inside each `runner.run` and passes the integer
remainder to the next stage. It does not count orchestration/promotion/hashing
overhead. Integer truncation can also report exhaustion with almost one second
remaining.

More importantly, `max_turns`, `max_tokens`, and
`max_consecutive_failures` remain unenforced despite the design calling all
budget fields hard caps. README documents only token/turn deferral; consecutive
failures are not discussed. The response therefore narrowed the implementation
without reconciling the design contract.

Recommended fix:

- Measure the aggregate deadline from pipeline start, including control-plane
  overhead, and derive precise remaining timeout at each invocation.
- Enforce available usage caps from adapter telemetry; reject unsupported hard
  caps or explicitly change the manifest/design semantics.
- Document and implement `max_consecutive_failures` at the scheduler/store
  boundary.

## Significant Findings

### 9. Per-stage records are not durable

Relevant code:

- `src/loopcraft/runners/base.py:71-87`
- `src/loopcraft/orchestrator.py:462-495`
- `src/loopcraft/cli.py:481-499`

The orchestrator places ad hoc dictionaries in `RunResult.stages`, but
`_run_execute` does not copy them into `RunRecord`. The aggregate pipeline log
references stage logs inside prunable worktrees. Stage metrics/statuses are
therefore not preserved in the authoritative run record, weakening the claim
that each stage is independently observable and costed.

Add a typed `StageRunResult` model and persist it in `RunRecord`.

### 10. Dry-run does not use the normalized execution plan

Relevant code:

- `src/loopcraft/cli.py:309-357`
- `src/loopcraft/orchestrator.py:91-165`

The response says one plan drives dry-run, preflight, and execution. Dry-run
does not build or consume that plan. It:

- ignores `--vendor` for inherited role rows;
- shows only top-level resolved outputs;
- does not show effective ownership/read-only policy.

This can display a different vendor and output contract from the run that would
execute.

### 11. New code still violates `CONTRIBUTING.md` typing/model rules

Examples:

- `src/loopcraft/orchestrator.py:63-84` uses dataclasses for execution-plan
  structures.
- `src/loopcraft/orchestrator.py:383`, `:553`, and `:559` use
  unparameterized dictionaries and an untyped argument suppressed by
  `# noqa: ANN001`.
- `src/loopcraft/runners/base.py:87` declares `stages: list[dict]`.
- `tests/test_cli_roles.py:52-58`, `:74`, `:176`, and `:204` suppress missing
  annotations on newly added signatures.

This directly contradicts response item 18. `CONTRIBUTING.md` requires fully
typed signatures, parameterized collections, and Pydantic for structured
cross-boundary shapes.

Use Pydantic models for execution/stage/handoff records and complete the new
test signatures instead of suppressing them.

### 12. Role tool declarations are validation labels, not runtime allowlists

Relevant code:

- `src/loopcraft/role_tools.py:41-69`
- `src/loopcraft/agent_compiler.py:62-100`
- `docs/loopcraft-implementation-design.html:787-797`

`role_tool_problems` classifies names and rejects a read-only role that declares
a known write tool. It does not translate the declared allowlist to actual
runtime permissions:

- Cursor and Codex compiled definitions omit tools.
- Claude receives vendor-neutral names such as `repo-read`, not demonstrated
  native tool identifiers.
- A role declaring only `repo-read` is not prevented from using other available
  read/network tools; a maker is not limited to its declared tools.

The classifier also treats every `nv-tools.*` connector as writing. That rejects
the design document's own read-only reviewer example
`tools: [nv-tools.gitlab, repo-read]`, even though GitLab read operations are a
core review use case. A connector name alone is insufficient to classify every
operation as read or write; operation-level gating is needed.

The branch should describe this as policy validation, not enforcement, until
each adapter maps and restricts native tools.

### 13. Direct orchestration can execute an invalid placeholder plan

Relevant code:

- `src/loopcraft/orchestrator.py:607-617`
- `src/loopcraft/orchestrator.py:663-686`

Preflight reports planning errors, but `run_multi_model()` rebuilds the plan and
discards its problems. CLI callers preflight first; direct callers can execute
placeholder agent definitions or another invalid plan. The public execution API
should fail closed when planning returns any problem.

### 14. Prior-stage stdout is inserted as trusted prompt instructions

Relevant code:

- `src/loopcraft/orchestrator.py:227-255`

`StageHandoff.render()` appends arbitrary prior-stage stdout directly after the
next role's instructions without a strong untrusted-data delimiter or structured
encoding. A compromised maker can inject instructions into the reviewer
handoff. Store stdout as data (for example, JSON or a referenced ledger
artifact), label it explicitly untrusted, and instruct the reviewer never to
follow directives contained within it.

### 15. Cursor's method-level security documentation remains stale

Relevant code:

- `src/loopcraft/runners/cursor.py:31-39`

The module docstring is corrected, but `CursorRunner.preflight` still says the
adapter grants write access to declared ledger outputs. Normal M3.5 execution
stages outputs in the worktree and promotes them in the control plane.

## Missing Regression Coverage

Add focused offline tests for:

1. Required reviewer verdict missing or malformed.
2. Explicit reviewer FAIL with a third stage that must not run.
3. Failed read-only stage with a later maker.
4. Intra-run PASS/FAIL semantics and read-only support per harness.
5. Codex/Claude intra-run with wrong-provider model ids.
6. Cursor provider binding with no explicit role model.
7. Explicit maker role outputs in both modes and dry-run.
8. Source-path swap during promotion and concurrent same-output promotion.
9. Multi-output failure after an earlier valid output was replaced.
10. Direct `run_multi_model` with planning errors.
11. Handoff prompt-injection resistance.
12. Durable serialization of per-stage results.
13. Aggregate budget including orchestration overhead.
14. Runtime-native tool allowlist mapping.
15. Opt-in live Cursor cross-provider discovery/spawn.

## Healthy Areas

- One shared preflight dispatcher now covers run, apply, and deps-check.
- Inter-stage model-shape preflight uses each role's real adapter.
- Current Cursor and Codex agent file schemas are represented correctly.
- Role names and generated stage-log paths are contained.
- Process failures no longer directly promote partial outputs.
- The inter-stage handoff includes promoted paths and content digests.
- Output collisions now participate in fleet analysis, even though effective
  ownership still needs correction.
- The branch adds useful CLI-level negative-path tests.
- All current offline checks pass.

## Recommended Merge Gate

Resolve Blocking Findings 1-8 and add their negative-path tests before calling
Review 01 fully addressed. Then reconcile the M3.5 milestone text with the
actual deferred scope. If live runtime smoke tests remain deferred, state that
the compiler/spawn path is schema-tested but the Cursor cross-provider exit
criterion is not yet demonstrated.
