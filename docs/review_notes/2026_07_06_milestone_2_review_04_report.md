# Milestone 2 Review 04 — Report (response follow-up)

Review target: local branch `feat/m2-control-plane` in
`/Users/dpickem/workspace/loopcraft`.

Reviewed commits:

- `5cbaa62 fix: address M2 review 03 finding (scheduled binary resolution)`
- `40f96b3 docs: add M2 review 03 response`

Reference baseline:

- `docs/review_notes/2026_07_06_milestone_2_review_03_report.md`
- `docs/review_notes/2026_07_06_milestone_2_review_03_response.md`
- M2 in `docs/loopcraft-implementation-design.html`
- `CONTRIBUTING.md`

This pass re-reviewed the branch after the Review 03 response, focusing on whether scheduled
runtime/tool binary resolution now uses the exact PATH rendered into the systemd service.

## Verification

Commands run from `/Users/dpickem/workspace/loopcraft`:

```text
make test && make compile && make validate
make check
```

Result:

- `make test`: passed, 281 tests.
- `make compile`: passed.
- `make validate`: passed, 3 manifests.
- `make check`: passed in this environment.

Focused reproduction for the finding:

```text
preflight_problems= ["demo: api 'slack': could not run the Slack read probe (nv-tools slack list-channels)"]
ok= False
```

That reproduction used a temporary Slack loop with `depends_on.tools: [nv-tools]`,
`depends_on.auth: [nv-tools]`, and `depends_on.apis: [slack]`. The fake `codex` and `nv-tools`
binaries were present in `scheduler.path`, and the operator process `PATH` was empty. Scheduled
binary resolution found the binaries, but the live Slack probe still failed because it invoked
`nv-tools` through the operator environment.

## Findings

### 1. Live capability probes still execute on the operator PATH

Relevant files:

- `src/loopcraft/runners/capabilities.py`
- `src/loopcraft/probes.py`
- `src/loopcraft/config.py`
- `tests/test_deploy.py`

Current state:

- Review 03 added `LoopcraftConfig.which()`, `scheduler.path`, and `scheduled_path`.
- `CodexRunner.preflight()` uses `config.which("codex")`.
- Declared tool checks use `config.which(...)`.
- `probe_nv_tools_auth()` uses `config.which("nv-tools")`.
- `probe_slack_api()` first uses `config.which("nv-tools")`, but then calls:
  `run_probe(["nv-tools", "slack", "list-channels", "--limit", "1", "--format", "json"], ...)`.
- `run_probe()` calls `subprocess.run()` without an `env` or resolved executable path, so it uses
  the current operator process `PATH`.

Why this matters:

Review 03 fixed binary existence checks against the scheduled service PATH, but the Slack API probe
still executes through the operator environment. This means `apply` can still disagree with the
unit it renders:

- A valid `scheduler.path` containing `nv-tools` can fail `apply` when the operator shell PATH does
  not contain `nv-tools`.
- Conversely, if future probes follow this pattern, they could pass using an operator-only binary
  even though the scheduled service would not find the same command.

M2's design promise is that `apply` validates the environment the scheduled service will use. Live
capability probes need to execute with that same binary path/environment, not just check it before
falling back to `subprocess.run()` defaults.

Recommended fix:

- Have probes execute the resolved binary from `config.which(...)`, for example:
  `nv_tools = config.which("nv-tools")`; then call `run_probe([nv_tools, "slack", ...], ...)`.
- Or extend `run_probe()` to accept an `env` / `path` and call it with the scheduled config's PATH
  when `config.scheduled_env` is true.
- Keep the check and execution path together so a probe cannot validate one PATH and execute
  another.
- Add a regression test where:
  - `scheduler.path` contains fake `codex` and `nv-tools`;
  - the operator `PATH` is empty or lacks those binaries;
  - `plan_deployment(..., run_preflight=True)` succeeds for a Slack loop whose fake `nv-tools`
    probe exits 0.

## Notes On Resolved Areas

- Review 03's main scheduled PATH model is materially in place for `CodexRunner.preflight()` and
  declared tool existence checks.
- Rendered service units now include `Environment=PATH=<scheduled_path>`.
- The remaining issue is that live subprocess probes must use the same scheduled PATH/resolved
  command they validate.
