# Milestone 2 Review 03 — Report (response follow-up)

Review target: local branch `feat/m2-control-plane` in
`/Users/dpickem/workspace/loopcraft`.

Reviewed commits:

- `929e685 fix: address M2 review 01 findings`
- `646808a docs: add M2 review 02 report`
- `71850b0 fix: address M2 review 02 finding (scheduled credential model)`
- `1cef5ca docs: add M2 review 02 response`

Reference baseline:

- `docs/review_notes/2026_07_06_milestone_2_review_02_report.md`
- `docs/review_notes/2026_07_06_milestone_2_review_02_response.md`
- M2 in `docs/loopcraft-implementation-design.html`
- `CONTRIBUTING.md`

This pass re-reviewed the branch after the Review 02 response, focusing on whether the scheduled
credential model is now consistent across `auth`, `apply`, and adapter preflight, and whether any
other M2 "passes apply, fails under systemd" gaps remain.

## Verification

Commands run from `/Users/dpickem/workspace/loopcraft`:

```text
make test && make compile && make validate
make check
```

Result:

- `make test`: passed, 272 tests.
- `make compile`: passed.
- `make validate`: passed, 3 manifests.
- `make check`: passed in this environment.

Focused probe for the new finding:

```text
preflight_problems= []
ok= True
```

That probe used a temporary Codex loop declaring `tools: [nv-tools]`, with no systemd `PATH` or
absolute runtime/tool binary configuration. `plan_deployment(..., run_preflight=True)` passed
because the interactive test process could find `codex` and `nv-tools` on its own PATH.

## Findings

### 1. Scheduled preflight still validates runtime/tool binaries on the operator PATH

Relevant files:

- `src/loopcraft/runners/codex.py`
- `src/loopcraft/runners/capabilities.py`
- `src/loopcraft/scheduler.py`
- `src/loopcraft/deploy.py`
- `pyproject.toml`
- `loopcraft.toml`

Current state:

- Review 01 fixed the `loopctl` entrypoint by resolving the rendered service `ExecStart` to an
  absolute executable.
- Review 02 fixed credential resolution by running deployment preflight against a scheduled config
  whose `env_value()` reads `scheduler.environment_file`.
- The rendered service still sets only:
  - `LOOPCRAFT_SOURCE`
  - `LOOPCRAFT_MEMORY`
  - optional `EnvironmentFile=...`
- It does not set `PATH`.
- `CodexRunner.preflight()` checks the runtime with `shutil.which("codex")`.
- Runtime-neutral tool checks use `shutil.which(...)` for declared tools such as `nv-tools`.
- `CodexRunner.build_command()` still invokes `codex` by bare name, and the agent/tool subprocesses
  will also rely on the service environment.

Why this matters:

M2's core promise is that unmet dependencies are reported at `apply`, not at runtime. The current
implementation now uses the scheduled credential model, but still uses the operator's interactive
PATH to validate runtime/tool binaries. A host can pass `apply` because the shell running `apply`
finds `codex` and `nv-tools`, then fail at the scheduled service because systemd's default PATH is
smaller or because those binaries live in user-specific locations not visible to the service user.

This is the same class of failure as the earlier bare `loopctl` finding, just one level deeper:
`loopctl` itself is absolute, but the runner and declared tools are still path-dependent.

Recommended fix:

- Add a scheduled command/tool resolution model for runtimes and declared tools.
- Options:
  - Render a known-good `PATH=` into the service from scheduler config, and have `apply` preflight
    resolve runtime/tools against that PATH.
  - Resolve runtime and declared tool binaries to absolute paths at `apply` time and pass them into
    the run environment or runner config.
  - Add `[scheduler].path` or `[scheduler].environment_file` requirements for PATH, then validate
    that it contains `codex`, `nv-tools`, and any declared tool binaries for the service user.
- Ensure `CodexRunner.build_command()` uses the same resolved binary that preflight checked, or
  ensure the service environment contains a validated PATH.
- Add regression tests:
  - a binary visible only through the operator PATH does not satisfy scheduled `apply` unless the
    rendered service will also get that PATH;
  - rendered service units include the validated PATH or absolute runtime/tool configuration;
  - a missing scheduled `codex`/`nv-tools` path blocks `apply` before units are written.

## Notes On Resolved Areas

- The Review 02 scheduled credential finding is fixed: environment-file-only X credentials now
  satisfy deployment preflight, and shell-only credentials no longer do.
- Review 01 fixes remain in place: absolute `ExecStart` for `loopctl`, side-effect-free failed
  `apply`, user-scope path unit targets, and install rollback.
- The remaining issue is confined to aligning runtime/tool binary lookup with the scheduled service
  environment rather than the operator shell.
