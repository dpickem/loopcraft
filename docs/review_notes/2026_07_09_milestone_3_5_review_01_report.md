# Milestone 3.5 Review 01 — Report

Review target: local branch `feat/m3.5-multi-model` in
`/Users/dpickem/workspace/loopcraft`.

Reviewed state:

- Base: `2ac7ca0 Merge pull request #3 from dpickem/feat/m3-adapters`
- HEAD: `5a78611 feat: add M3.5 multi-model loops (roles, agent compiler, orchestration)`
- Also included the current uncommitted edits to `agents/implementer.md` and
  `agents/reviewer.md`.

Reference scope:

- M3.5 and the manifest/runtime sections in
  `docs/loopcraft-implementation-design.html`
- `CONTRIBUTING.md`

## Executive Summary

The branch establishes useful foundations: typed role manifests, vendor-neutral
agent definitions, native-format compilation, per-run output staging, and
offline orchestration tests. The full project checks pass.

It is not ready to merge as the M3.5 implementation described by the design,
however. The main issue is not polish; several advertised safety and execution
contracts are not wired through the real control-plane paths:

1. `apply` and `deps check --loop` bypass multi-model preflight.
2. Cursor intra-run preflight checks the wrong binaries and can pass without
   `cursor-agent`.
3. `readonly: true` is an instruction, not an enforced capability boundary.
4. Agent `verify` and `tools` metadata do not govern execution.
5. Failed runs can promote partial output into the durable ledger.
6. The inter-stage path does not actually hand a repository diff between
   stages, and output handoff is wrong when a maker declares role-specific
   outputs.
7. Cursor and Codex agent files do not match the current runtime schemas.
8. Output symlinks and unsafe role names can cross intended filesystem
   boundaries.
9. Multi-stage execution does not enforce one aggregate hard budget.

These gaps directly intersect all three M3.5 exit criteria: cross-provider
maker/checker execution, Cursor sub-agent execution, and safe portable
ledger-writing.

## Verification

Commands run from `/Users/dpickem/workspace/loopcraft`:

```text
make test && make compile && make validate && make check
git diff --check main...HEAD
git diff --check
```

Results:

- 387 tests passed.
- Source/tests byte-compilation passed.
- All 3 checked-in manifests validated.
- Dependency check and apply dry-run passed.
- Committed and uncommitted diffs passed whitespace checks.

The passing `apply` check does not exercise a roles manifest; that omission is
material to Finding 1.

## Blocking Findings

### 1. Deployment and dependency preflight bypass the multi-model preflight

Relevant code:

- `src/loopcraft/deploy.py:115-135`
- `src/loopcraft/cli.py:1406-1434`
- `src/loopcraft/cli.py:306-312`
- `src/loopcraft/orchestrator.py:301-358`

`loopctl run` recognizes a roles loop and calls `preflight_multi_model`, but the
two other promised preflight entry points do not:

- `deploy.preflight_loop`, used by `loopctl apply`, resolves only the top-level
  vendor and calls that one runner's `preflight`.
- `_preflight_loop`, used by `loopctl deps check --loop`, does the same.

Consequently, deployment can report a roles loop ready while a role agent file,
role adapter, or role runtime binary is missing. This conflicts with both the
design's apply-time validation contract and README's statement that preflight
checks every role.

Recommended fix:

- Move the single-model/multi-model dispatch into one shared preflight helper
  used by `run`, `apply`, and `deps check --loop`.
- Add CLI/deployment tests with a real roles manifest and a missing second-role
  agent/binary.

### 2. Intra-run preflight validates role CLIs instead of the harness CLI

Relevant code:

- `src/loopcraft/orchestrator.py:322-357`
- `src/loopcraft/orchestrator.py:259-298`

For a Cursor intra-run manifest with Codex and Claude role bindings,
`preflight_multi_model` checks for `codex` and `claude`, but does not check for
`cursor-agent` unless one role itself says `vendor: cursor`. Execution does the
opposite: it invokes only the top-level Cursor harness.

This produces both false failures and false passes:

- A host with Cursor but without the standalone Codex/Claude CLIs is rejected,
  even though Cursor is supposed to broker both role models natively.
- A host with Codex/Claude but without `cursor-agent` can pass preflight and
  fail at execution.

That prevents the branch from reliably meeting the explicit exit criterion
"a Cursor loop spawns a cross-provider sub-agent."

Recommended fix:

- For `intra-run`, preflight the harness adapter/binary once, compile-validate
  every role for that harness, and validate role model bindings according to
  the harness contract.
- For `inter-stage`, preflight each role's actual adapter using its
  single-stage manifest.
- Add tests for both missing-harness and absent-standalone-provider cases.

### 3. `readonly: true` is not an enforced boundary

Relevant code:

- `src/loopcraft/orchestrator.py:98-119`
- `src/loopcraft/orchestrator.py:220-235`
- `src/loopcraft/agent_compiler.py:56-70`
- `src/loopcraft/runners/claude.py:61-81`
- `src/loopcraft/runners/codex.py:65-99`

The design says the reviewer's read-only policy travels with the role and
guarantees that the checker cannot mutate. In the inter-stage implementation,
the policy is only appended to the prompt. The reviewer runs in the same
worktree as the maker:

- Codex receives workspace-write permission over the worktree.
- Claude uses `--permission-mode acceptEdits`.
- The maker's staged files remain present and writable.

Narrowing `ctx.resolved_outputs` changes output verification; it does not
restrict filesystem writes. Intra-run similarly relies on generated metadata
and a prompt preamble without a control-plane check that the runtime actually
enforces the restriction.

Recommended fix:

- Give an inter-stage reviewer a separate read-only snapshot/worktree and only
  one isolated writable directory for its own review output, or enforce the
  equivalent runtime sandbox policy.
- After review, verify that protected files and maker outputs are unchanged
  before accepting/promoting the verdict.
- Treat runtimes without enforceable reviewer isolation as unsupported in
  preflight instead of presenting prompt text as a guarantee.
- Add a malicious-reviewer regression test that attempts to modify maker/source
  files.

### 4. Agent-definition `verify` and `tools` are parsed but do not govern runs

Relevant code:

- `src/loopcraft/agents.py:40-62`
- `src/loopcraft/agent_compiler.py:73-116`
- `src/loopcraft/orchestrator.py:73-95`
- `src/loopcraft/orchestrator.py:213-235`

`AgentDefinition.verify` is never emitted by any compiler and the inter-stage
stage manifest explicitly sets `logic.verify=None`. Therefore the reviewer
rubric, including its required explicit PASS/FAIL, is absent from the stop
condition and is never evaluated.

The `tools` list is likewise not a control-plane capability contract:

- Inter-stage execution loads the complete markdown file as a skill, so tools
  are merely YAML text in the prompt.
- Multi-model preflight checks top-level `depends_on`, not each role's tools.
- No mapping or allowlist enforces that a reviewer with `[repo-read]` cannot use
  mutating tools.

This breaks the claim that the agent definition is the single source of truth
for behavior and policy.

Recommended fix:

- Compile `verify` into the native agent instructions/stop contract and include
  it in inter-stage prompts.
- Validate a machine-readable verdict rather than equating CLI exit zero and
  output existence with reviewer PASS.
- Map role tools to runtime-native permissions and fail preflight when a tool
  cannot be mapped.

### 5. Failed stages and runs promote partial outputs to durable state

Relevant code:

- `src/loopcraft/cli.py:486-498`
- `src/loopcraft/orchestrator.py:235-247`
- `src/loopcraft/orchestrator.py:296-298`
- `src/loopcraft/outputs.py:82-95`

All three execution paths call `promote_outputs` regardless of `RunResult`
status:

- A single-model process can exit nonzero after writing a partial file; that
  file is copied over the durable ledger value.
- An inter-stage maker is promoted before its failure status is checked.
- An intra-run harness promotes any existing staged files even when the harness
  failed.

The run record then correctly says `failed`, but the source-of-truth ledger may
already contain failed/partial content. This undermines the state model and can
feed bad data to downstream loops.

Recommended fix:

- Promote only after the stage/run has met its complete success contract.
- If failed-attempt artifacts are useful, archive them under a run-scoped
  diagnostic path that cannot replace canonical outputs.
- Make promotion transactional (temporary destination plus atomic replace) and
  add failure/partial-write regression tests.

### 6. Inter-stage handoff does not satisfy the clean-diff contract

Relevant code:

- `src/loopcraft/orchestrator.py:43-70`
- `src/loopcraft/orchestrator.py:193-244`
- `src/loopcraft/worktree.py:268-340`

The design's M3.5 exit criterion requires the implementer/reviewer pair to hand
the diff between stages cleanly. The implementation hands over:

- up to 20,000 characters parsed from the previous process's stdout; and
- a list of top-level ledger output paths.

It does not create a Git worktree, capture a Git diff, record a content digest,
or pass a structured stage artifact. The current "worktree" is a scratch bundle
containing selected loop assets, not a repository checkout. A maker therefore
cannot implement a normal repository change inside this run directory, and a
reviewer has no authoritative diff to inspect.

Recommended fix:

- Define a structured handoff model containing the prior stage result,
  authoritative output paths/digests, and (for code work) the exact base/head
  diff or commit.
- Use a real isolated Git worktree for code-changing roles, or narrow M3.5's
  documented scope and defer code-diff maker/checker execution explicitly.
- Test a complete CLI-level maker/checker run, not only fake runner calls.

### 7. Role-specific maker outputs are handed off incorrectly and differ by mode

Relevant code:

- `src/loopcraft/orchestrator.py:161-170`
- `src/loopcraft/orchestrator.py:194-198`
- `src/loopcraft/orchestrator.py:220-238`
- `src/loopcraft/orchestrator.py:282-298`

The documented ownership rule says a maker with role outputs owns those outputs;
otherwise it inherits top-level outputs. Inter-stage execution follows that rule
for writes, but `maker_ledger` is always computed from `manifest.outputs`.
Therefore, when the maker declares role-specific outputs, the reviewer is told
to read the wrong files.

The modes also disagree:

- Inter-stage substitutes maker role outputs for top-level outputs.
- Intra-run always requires top-level outputs plus every role output.

The same manifest can therefore succeed in one mode and fail in the other, or
produce different durable state.

Recommended fix:

- Build one normalized role/output ownership plan before either execution path.
- Derive reviewer handoff, output verification, promotion, dry-run display, and
  run-record provenance from that plan.
- Reject ambiguous overlaps and test explicit maker outputs in both modes.

### 8. Run-time `--vendor` overrides are not propagated into roles execution

Relevant code:

- `src/loopcraft/cli.py:293-317`
- `src/loopcraft/cli.py:320-328`
- `src/loopcraft/cli.py:486-492`

`_cmd_run` calculates `effective_vendor` from `--vendor`, but multi-model
preflight and execution are called with `config.default_vendor`. A role that
inherits its vendor therefore ignores the one-off override displayed by the
CLI. For inter-stage loops, the CLI also insists that the top-level effective
vendor has a registered runner even when every stage has an explicit vendor and
no top-level harness is used.

Recommended fix:

- Pass the already-resolved override through preflight and orchestration.
- Resolve a normalized execution plan once and use it for display, preflight,
  and execution.
- Add multi-model `run --vendor` tests with inherited role vendors.

### 9. Generated Cursor and Codex agents do not match current runtime schemas

Relevant code:

- `src/loopcraft/agent_compiler.py:29-34`
- `src/loopcraft/agent_compiler.py:73-116`
- `src/loopcraft/agent_compiler.py:134-146`

The branch follows the design document's proposed `.cursor/agents/*.yaml`
layout, but current Cursor documentation requires Markdown files under
`.cursor/agents/` with YAML frontmatter and a Markdown prompt body. The
generated YAML files therefore will not be discovered as project sub-agents.

The Codex TOML also uses `instructions`, `read_only`, and `tools`. Current Codex
custom-agent files require `name`, `description`, and
`developer_instructions`; read-only enforcement is expressed with
`sandbox_mode = "read-only"` (or the corresponding current permission
profile). The generated file can therefore lose both its instructions and its
claimed read-only policy.

Runtime references checked during this review:

- Cursor: `https://cursor.com/docs/subagents.md`
- Codex: `https://developers.openai.com/codex/subagents`

This means the fake compiler tests prove only that Loopcraft can parse its own
output, not that either runtime discovers and executes it.

Recommended fix:

- Update the compiler to the current documented schemas.
- Correct the implementation design and README, which currently preserve the
  stale Cursor YAML assumption.
- Add schema fixtures and an opt-in discovery smoke test against installed
  runtimes.

### 10. Output symlinks can turn promotion into a file-disclosure path

Relevant code:

- `src/loopcraft/runners/base.py:229-235`
- `src/loopcraft/runners/base.py:291-300`
- `src/loopcraft/outputs.py:82-95`

Output verification and promotion use `exists()`, `stat()`, and `shutil.copy2`,
all of which follow symlinks in this use. An agent can create its declared
output path as a symlink to another file. The control-plane process then copies
the target's contents into the durable ledger, potentially using broader read
permissions than the sandboxed agent had. A directory also passes `exists()`
and fails only later in `copy2`.

Recommended fix:

- Use `lstat()` and reject symlinks and every non-regular-file output.
- Re-check source and destination containment immediately before promotion.
- Copy through a safely opened temporary regular file and atomically replace
  the destination.
- Add symlink, directory, and destination-race regression tests.

### 11. Unvalidated role names can escape the stage-log directory

Relevant code:

- `src/loopcraft/manifest.py:455-466`
- `src/loopcraft/orchestrator.py:205-229`

Role mapping keys are unrestricted and are interpolated directly into
`stage-{index}-{name}.log`. A name containing enough `../` components creates a
path outside `ctx.workdir`; unlike compiled-agent paths, the generated log path
has no containment assertion.

Recommended fix:

- Restrict role names to a documented safe vocabulary such as lowercase
  alphanumeric components separated by hyphens.
- Assert every generated stage-log path is contained by the worktree.
- Add traversal and absolute-looking role-name tests.

### 12. Multi-stage execution does not enforce the manifest's aggregate budget

Relevant code:

- `src/loopcraft/orchestrator.py:205-250`
- `src/loopcraft/runners/base.py:163-167`
- `src/loopcraft/runners/base.py:237-251`

Every inter-stage role receives the original full manifest budget. Only
`max_runtime` is technically enforced by `BaseRunner`; `max_turns` is prompt
text, while `max_tokens` and `max_consecutive_failures` are not enforced here.
An N-stage loop can therefore consume roughly N times the declared runtime cap
and unbounded turns/tokens. The orchestrator also discards stage usage, so it
cannot detect or report aggregate exhaustion.

The design defines these values as hard scheduler caps, not advisory per-stage
hints.

Recommended fix:

- Track one orchestration-level remaining budget.
- Pass each stage only its remaining allowance and abort before launching a
  stage that cannot fit.
- Enforce turns/tokens where runtime telemetry permits, fail closed when a hard
  cap cannot be measured, and aggregate runtime/tokens/cost/iterations into the
  pipeline result.
- Add multi-stage exhaustion tests, including a stage that times out after an
  earlier stage consumed part of the budget.

## Significant Suggestions

### 13. Per-role models never receive adapter-specific preflight

`preflight_multi_model` checks adapter registration and binary presence but does
not call each role runner's `preflight` on the generated stage manifest. A role
such as `vendor: codex, model: opus` can therefore pass apply/run preflight even
though `CodexRunner` already has a model-shape guard.

Use the same stage manifest for preflight and execution so the two paths cannot
drift.

### 14. Aggregate status and metrics lose stage information

Relevant code: `src/loopcraft/orchestrator.py:199-256`.

Any non-success stage becomes an aggregate `failed`, including `stalled` and
`needs_approval`. Exit codes, tokens, cost, and iteration counts from all stages
are dropped. This conflicts with the normalized status vocabulary and the
design's statement that inter-stage runs are independently observable and
costed.

Define deterministic aggregation rules and preserve stage results in the
durable run record (or a dedicated pipeline record).

### 15. Role outputs are absent from fleet collision and DAG validation

Relevant code:

- `src/loopcraft/manifest.py:617-645`
- `src/loopcraft/manifest.py:661-684`

Producer collision checks and input-derived DAG edges consider only
`manifest.outputs`. Role outputs can collide with another role or loop without
validation, and downstream inputs do not infer a dependency on a role-produced
file.

Include normalized effective role outputs in producer analysis, while avoiding
double-counting inherited top-level outputs.

### 16. Multiple roles can compile to the same native agent file

Relevant code:

- `src/loopcraft/manifest.py:455-466`
- `src/loopcraft/agent_compiler.py:119-146`
- `src/loopcraft/orchestrator.py:269-280`

The manifest role key and `AgentDefinition.name` are independent. Two roles can
reference definitions with the same `name`, causing the second compiled file to
overwrite the first. Role/agent names also have no explicit native-format-safe
vocabulary.

Validate uniqueness of compiled destinations and either require role name to
match definition name or make the manifest role key the canonical compiled
name.

### 17. The tests prove composition plumbing, not the advertised control-plane behavior

`tests/test_orchestrator.py` calls `run_multi_model` directly with a fake runner.
It does not exercise:

- `loopctl run`, `deps check --loop`, or `apply` with a roles manifest;
- failed-output promotion;
- runtime override inheritance;
- model mismatch preflight;
- reviewer mutation attempts;
- a real diff or structured handoff;
- Cursor harness binary selection.
- current Cursor/Codex schema and discovery;
- output symlink and role-name traversal rejection.
- aggregate multi-stage budget exhaustion.

Add focused regression tests for the findings above, then at least one opt-in
runtime smoke test for native sub-agent discovery. Offline unit tests should
remain the default, as required by `CONTRIBUTING.md`.

### 18. New test signatures do not follow the repository's typing rule

Examples:

- `tests/test_orchestrator.py:69`
- `tests/test_orchestrator.py:98`
- `tests/test_orchestrator.py:114`

`CONTRIBUTING.md` requires all function signatures to be fully typed and
parameterized collections. Several new fixture/helper signatures omit parameter
and return annotations, and `_CALLS` uses `list[dict]` instead of a parameterized
structured shape.

Add concrete annotations (and preferably a small typed model/TypedDict for call
records).

### 19. Cursor adapter documentation describes the legacy coarse-grant path as the M3.5 model

`src/loopcraft/runners/cursor.py:8-13` says the M3.5 writable-root grant disables
the sandbox for ledger outputs. The branch's normal M3.5 path now stages those
outputs in-worktree and README correctly says the coarse grant is only a
fallback for out-of-worktree targets.

Update the module docstring so security-sensitive behavior is described
consistently.

## M3.5 Requirements Matrix

### Build deliverables

- **`roles:` manifest block — Partial.**
  Typed parsing, ordering, source-path validation, and role outputs exist.
  Effective output collision/DAG validation and mode-consistent ownership do
  not.
- **Agent-definition compiler — Not runtime-compatible for Cursor/Codex.**
  Three proposed formats are rendered and path-contained, but current Cursor
  and Codex schemas differ from the generated files. In addition, `verify` is
  dropped, tools/read-only are not consistently enforceable, and compiled-name
  collisions are possible.
- **Inter-stage composition across adapters via memory ledger — Partial.**
  Ordered invocations and promotion exist, but handoff is stdout plus sometimes
  incorrect paths; there is no authoritative diff; failed output can be
  promoted; status/cost information is lost.
- **Intra-run sub-agents on Cursor — Partial/unproven.**
  Cursor-format files are written and one harness invocation occurs in a fake
  test. Preflight checks the wrong binaries, and there is no runtime smoke test
  proving Cursor discovers and spawns the agents.
- **Cursor writable-root parity — Mostly met for ordinary outputs.**
  Worktree-local staging removes the need for a normal external write grant
  across all adapters. Promotion-on-failure must be corrected before this is a
  safe durable-state path.
- **Aggregate hard budgets — Missing.**
  Runtime is enforced per invocation rather than across the pipeline; turns,
  tokens, and consecutive failures are not hard orchestration limits.

### Exit criteria

- **GPT implementer + Opus reviewer with clean stage handoff — Partial.**
  Per-role vendor/model selection is present, but the tested handoff is fake
  stdout and paths, not an authoritative diff or validated reviewer verdict.
- **Cursor loop spawns a cross-provider sub-agent — Unproven and preflight-broken.**
  Files are compiled and a fake Cursor runner is called; the real harness can be
  missing while preflight passes.
- **Ledger-writing loop runs unchanged under Cursor — Mechanism present, safety incomplete.**
  Output staging/promotion provides parity, but partial failed output is still
  eligible for promotion.

### Explicit M3.5 risk closures

- **Context/diff loss — Not closed.** Handoff is truncated stdout and ledger
  path hints; no structured diff contract.
- **Two-model cost — Not closed.** Stage metrics are discarded.
- **Hard budget enforcement — Not closed.** Multi-stage runs can exceed the
  declared aggregate caps.
- **Read-only reviewer cannot mutate — Not closed.** Prompt-level instruction is
  presented as enforcement.

## Healthy Areas

- The role manifest and agent-definition structures use Pydantic and forbid
  unknown fields.
- Source-relative role agent paths are validated and resolved through the
  existing containment boundary.
- Compiled agent destinations are checked against worktree escape.
- Output staging is a materially safer default than granting every runtime
  direct ledger write access.
- Single-model compatibility is preserved in the manifest validation path.
- The new tests are offline and deterministic.
- Documentation explains the intended two execution modes and role/output
  vocabulary clearly.
- The branch passes the repository's full current verification suite.

## Recommended Merge Gate

At minimum, resolve Findings 1-12 and add regression coverage for each before
calling the branch M3.5-complete. If the intended near-term scope is only the
schema/compiler/plumbing foundation, rename the shipped scope accordingly and
mark the Cursor spawn, clean diff handoff, enforced read-only reviewer, and
production multi-model preflight as deferred rather than claiming the current
M3.5 exit criteria.
