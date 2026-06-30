# Milestone 1 Review 01 — Report (Loopcraft control plane)

Review target: local uncommitted changes in `/Users/dpickem/workspace/loopcraft`.

Reference scope:

- M1 in `obsidian/dpickem_default/40-Resources/docs/loopcraft-implementation-design.html`
- `CONTRIBUTING.md`

This pass focuses on the M1 control-plane scope: manifest schema and validation, the Codex
runner (`preflight` + `run`), `loopctl run <loop>` in an isolated worktree, the
`loopcraft.store` ledger write path, Makefile wrappers, and the first observe-only
`slack-triage` loop.

## Verification

Commands run from `/Users/dpickem/workspace/loopcraft`:

```text
make test && make compile && make validate
```

Result:

- `make test`: passed, 38 tests.
- `make compile`: passed.
- `make validate`: passed, 1 manifest.

The checks cover the happy-path M1 exit criteria with a stub runner, basic manifest validation,
store writes, and Codex runner subprocess behavior. The findings below are contract gaps that
are not covered by the current tests.

## Findings

### 1. Slack cursor state is required by the skill but missing from the manifest contract

Relevant files:

- `loops/slack-triage.yaml`
- `skills/slack-triage/SKILL.md`
- `tests/test_cli.py`

Current state:

- The manifest declares only one output: `state/slack/triage-latest.md`.
- The skill instructs the agent to read and update `state/slack/seen.json` as the cursor for
  processing messages since the last run.
- The runner only resolves and verifies manifest-declared outputs, so `seen.json` is invisible to
  the run contract, dependency graph, and M1 exit test.

Why this matters:

`CONTRIBUTING.md` requires loop dependencies, inputs, outputs, and durable state to be explicit in
the manifest. `seen.json` is durable loop state and affects future runs. Leaving it only in the
skill hides a state dependency from validation and makes it easy for an implementation to pass M1
while never updating the cursor.

Recommended fix:

- Declare `state/slack/seen.json` in the manifest, likely as both an input cursor and an output
  cursor for the same loop.
- Keep `state/slack/triage-latest.md` as the user-facing exit-criteria output.
- Add a CLI or runner test that confirms both declared state files are produced or that the cursor
  write is explicitly deferred out of M1.

### 2. Codex preflight does not validate all declared dependencies

Relevant files:

- `src/loopcraft/runners/codex.py`
- `src/loopcraft/cli.py`
- `loops/slack-triage.yaml`
- `tests/test_runner.py`

Current state:

- `CodexRunner.preflight()` checks the `codex` binary, the skill path, declared tools, and
  declared environment variables.
- The Slack manifest declares `depends_on.apis: [slack]` and `depends_on.auth: [nv-tools]`, but
  those fields are ignored.
- Model availability is also not checked, even though the M1 runner contract in `CONTRIBUTING.md`
  calls out model support as a preflight concern.
- `loopctl deps check` probes generic binaries but does not validate the selected loop's declared
  API/auth/model requirements.

Why this matters:

M1 is supposed to catch setup problems before a headless run starts. Today a missing Slack/nv-tools
auth setup can pass preflight and fail late inside the Codex agent, which is exactly the failure
mode the M1 preflight path is meant to avoid.

Recommended fix:

- Add a small dependency-preflight layer that handles declared auth/API capabilities for known
  tools. For `auth: [nv-tools]` and `apis: [slack]`, prefer a bounded local/CLI probe that reports
  actionable setup errors without mutating remote state.
- Decide what model support can be verified locally for Codex and report unknown/unavailable
  models clearly.
- Add tests that a manifest with declared API/auth/model requirements produces preflight problems
  when the probe reports missing capability.

### 3. `loopctl run` uses an empty directory, not an isolated source worktree

Relevant files:

- `src/loopcraft/cli.py`
- `src/loopcraft/runners/codex.py`
- `skills/slack-triage/channels.txt`
- `tests/test_cli.py`
- `tests/test_runner.py`

Current state:

- `loopctl run` creates `<memory>/var/worktrees/<loop>/<run_id>` with `mkdir`.
- `CodexRunner` runs `codex exec --skip-git-repo-check` in that empty directory.
- The prompt embeds the main `SKILL.md`, but additional repo-local assets such as
  `skills/slack-triage/channels.txt` are not present relative to the working directory.

Why this matters:

M1 explicitly calls for headless execution in an isolated worktree. The current directory is
isolated, but it is not a source checkout/worktree, so loop assets and repo context are missing.
The Slack skill tells the agent to read `skills/slack-triage/channels.txt`; from the current run
directory that relative path does not exist.

Recommended fix:

- Create a real isolated source worktree or checkout for each run, or copy a minimal source bundle
  containing the manifest and skill assets into the run directory.
- Remove `--skip-git-repo-check` once the runner executes inside a real source worktree, unless
  there is a documented Codex-specific reason to keep it.
- Add a test that `RunContext.workdir` contains the expected loop asset path, especially
  `skills/slack-triage/channels.txt`.

### 4. Ledger path resolution can escape the memory tree

Relevant files:

- `src/loopcraft/config.py`
- `src/loopcraft/store.py`
- `src/loopcraft/manifest.py`
- `tests/test_store.py`
- `tests/test_manifest.py`

Current state:

- `LoopcraftConfig.resolve_state_path()` strips a leading `state/` prefix and joins the remainder
  under `<memory>/ledger`.
- Absolute paths and `..` segments are not rejected.
- `Store.write_state()` and `append_jsonl()` trust `resolve_state_path()` directly.

Why this matters:

The store is the sanctioned persistence boundary between source and memory. A malformed or
malicious manifest path like `/tmp/out.md` or `../../repo-file` can resolve outside the ledger and
possibly outside the memory tree, violating the source/memory separation and the path-validation
rule in `CONTRIBUTING.md`.

Recommended fix:

- Add a shared path validator for manifest-declared state paths.
- Reject absolute paths, empty `state`, `..` segments, and paths that resolve outside
  `config.ledger_dir`.
- Cover both manifest validation and `Store` methods with regression tests.

### 5. Runtime budgets are declared but not enforced

Relevant files:

- `loops/slack-triage.yaml`
- `src/loopcraft/runners/codex.py`
- `src/loopcraft/runners/base.py`
- `tests/test_runner.py`

Current state:

- `slack-triage` declares `budget.max_runtime: 10m`.
- `CodexRunner.run()` calls `subprocess.run()` without a timeout.
- There is no normalized result for timeout/stall behavior.

Why this matters:

An unattended headless M1 run can hang forever if the Codex invocation stalls. That undermines the
control-plane contract and makes the `max_runtime` manifest field misleading.

Recommended fix:

- Parse `budget.max_runtime` into seconds and pass it to `subprocess.run(timeout=...)`.
- Catch `subprocess.TimeoutExpired`, write the partial log if available, and return a normalized
  `failed` or `stalled` result.
- Add a runner test for timeout handling and run-record status.

## Notes On Healthy Areas

- The routine shell scripts were removed in favor of Makefile targets, which aligns with
  `CONTRIBUTING.md`.
- The new M1 code keeps durable writes under `loopcraft.store` for tests and control-plane
  records.
- Public structures are dataclasses with typed signatures, and tests remain offline by default.
- The README clearly states the M1 shipped scope and defers Claude/Cursor adapters, the scheduler,
  harvester, and UI to later milestones.
