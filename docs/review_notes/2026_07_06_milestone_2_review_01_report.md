# Milestone 2 Review 01 — Report (M2 control plane)

Review target: local branch `feat/m2-control-plane` in
`/Users/dpickem/workspace/loopcraft`.

Reviewed state:

- Branch HEAD: `bd35fb1 Merge pull request #1 from dpickem/feat/m1-control-plane`
- Uncommitted M2 implementation changes in the working tree, including:
  - `src/loopcraft/cli.py`
  - `src/loopcraft/config.py`
  - `src/loopcraft/deploy.py`
  - `src/loopcraft/scheduler.py`
  - `src/loopcraft/cli_output.py`
  - `src/loopcraft/runners/capabilities.py`
  - `Makefile`, `README.md`, `loopcraft.toml`, `CONTRIBUTING.md`
  - `tests/test_cli_m2.py`, `tests/test_deploy.py`, `tests/test_scheduler.py`

Reference scope:

- M2 in `docs/loopcraft-implementation-design.html`
- `CONTRIBUTING.md`

This pass focuses on the M2 scheduler/auth/apply scope: `loopctl init`, `loopctl auth`,
`loopctl apply` systemd rendering/install, pre-deploy dependency validation, and the terminal
`loopctl fleet` view.

## Verification

Commands run from `/Users/dpickem/workspace/loopcraft`:

```text
uv run pytest -q
uv run python -m compileall -q src tests
uv run loopctl validate ./loops
uv run loopctl deps check
uv run loopctl apply ./loops --dry-run --skip-preflight
uv run loopctl apply ./loops --skip-preflight --out /tmp/loopcraft-review-units
```

Result:

- `uv run pytest -q`: passed, 251 tests.
- `uv run python -m compileall -q src tests`: passed.
- `uv run loopctl validate ./loops`: passed, 3 manifests.
- `uv run loopctl deps check`: passed in this environment.
- `uv run loopctl apply ./loops --dry-run --skip-preflight`: passed and planned 6 units.
- `uv run loopctl apply ./loops --skip-preflight --out /tmp/loopcraft-review-units`: passed and
  rendered service/timer units for `arxiv-intel`, `slack-triage`, and `x-intel`.

The rendered services currently contain `ExecStart=loopctl run <loop>`.

## Findings

### 1. Rendered systemd services are not reliably runnable in the documented uv setup

Relevant files:

- `src/loopcraft/scheduler.py`
- `src/loopcraft/config.py`
- `Makefile`
- `README.md`
- `loopcraft.toml`

Current state:

- The documented development/setup path uses `uv sync` and `uv run ...`.
- The Makefile invokes `loopctl` through `uv run loopctl`.
- The default scheduler config is `loopctl_bin = "loopctl"`.
- Rendered services use `ExecStart=loopctl run <loop>`.
- A freshly prepared host that followed `uv sync` has a project `.venv`, but it may not have a
  globally installed `loopctl` binary on systemd's PATH.

Why this matters:

M2's purpose is unattended scheduled execution. A timer that starts successfully but whose service
fails before reaching `loopctl` does not satisfy "L1 + L2 fire on schedule with the laptop closed."
The design examples use `/usr/local/bin/loopctl`, while the repo now standardizes local commands on
`uv run`.

Recommended fix:

- Pick a hardened default for rendered services:
  - set `scheduler.loopctl_bin` to an absolute installed `loopctl` path during `loopctl init`, and
    validate it at `apply`; or
  - render `ExecStart=<absolute-uv> run loopctl run <loop>` with `WorkingDirectory=<source>`.
- Make `apply` fail if the configured `loopctl_bin` cannot be resolved for the systemd context.
- Add a scheduler test that renders from the default uv-managed setup and proves the resulting
  `ExecStart` is executable without relying on the interactive shell PATH.

### 2. `loopctl apply` writes staged units even when preflight reports unmet dependencies

Relevant files:

- `src/loopcraft/cli.py`
- `src/loopcraft/deploy.py`
- `tests/test_cli_m2.py`

Current state:

- `plan_deployment()` reports manifest, render, and preflight problems.
- `DeploymentPlan.renderable` ignores preflight problems so units can still be rendered.
- `_cmd_apply()` writes units whenever `plan.renderable` is true, even when `plan.ok` is false due
  to failed preflight.
- `tests/test_cli_m2.py::test_apply_reports_unmet_dependency_at_apply` asserts this behavior:
  units still render when a dependency is missing.

Why this matters:

The implementation design says validation happens at `apply` so a loop with an unmet dependency is
reported, not deployed. The module docstring in `deploy.py` also says the whole fleet is validated
"before anything is written." Writing staged unit files for a failed preflight makes `fleet` report
`staged` for a loop whose deployment was rejected, which can blur the distinction between "ready to
deploy" and "rendered diagnostics."

Recommended fix:

- Decide whether preflight failures should block all unit writes by default. That is the safest
  reading of the design and the current `deploy.py` docstring.
- If diagnostic rendering on failed preflight is intentional, require an explicit flag such as
  `--out` or `--render-invalid`, and keep default `apply` side-effect-free unless `plan.ok`.
- Update `test_apply_reports_unmet_dependency_at_apply` so it does not encode unsafe default
  behavior.

### 3. `apply`/`auth` validate the interactive environment, not the scheduled service environment

Relevant files:

- `src/loopcraft/cli.py`
- `src/loopcraft/config.py`
- `src/loopcraft/scheduler.py`
- `src/loopcraft/runners/capabilities.py`
- `loopcraft.toml`

Current state:

- `loopctl` loads `.env` in the interactive process.
- `auth` and `apply` probes call `config.env_value()`, which reads the current process
  environment.
- Rendered systemd services may run as another user and optionally source `scheduler.environment_file`.
- There is no validation that `scheduler.environment_file` exists, lives outside source/memory, is
  readable by the target service user/scope, or contains the env vars required by manifests.

Why this matters:

`apply` can pass because credentials are available in the operator's shell or repo-local `.env`,
while the scheduled service later fails because the unit's actual environment is different. M2's
exit criterion is specifically that unmet dependencies are reported at `apply`, not at runtime.

Recommended fix:

- Treat `scheduler.environment_file` as the authority for scheduled credentials when it is
  configured.
- Validate that the file exists, is outside the source and memory trees, and contains all declared
  env vars needed by scheduled loops.
- For `system` scope with `User=...`, validate readability by that user where practical, or emit
  explicit guidance when it cannot be verified.
- Add tests for env vars present only in `.env` versus only in `scheduler.environment_file`.

### 4. User-scope `.path` units render the system target `multi-user.target`

Relevant files:

- `src/loopcraft/scheduler.py`
- `tests/test_scheduler.py`

Current state:

- Cron timers render `[Install] WantedBy=timers.target`, which works for both system and user
  managers.
- `on-artifact` path units always render `[Install] WantedBy=multi-user.target`.
- For `scheduler.scope = "user"`, `install_units()` enables units with `systemctl --user`.

Why this matters:

`multi-user.target` is a system manager target. User-scope units are normally enabled under
`default.target` (or another user target). A user-scope artifact trigger can fail to enable or never
start, even though the rendered unit looked valid and tests passed.

Recommended fix:

- Render path unit install targets based on scheduler scope:
  - `multi-user.target` for system scope.
  - `default.target` for user scope.
- Add a user-scope `on-artifact` scheduler test that checks the rendered path unit and `install`
  target.

### 5. `apply --install` can leave a partially installed/enabled fleet on failure

Relevant files:

- `src/loopcraft/deploy.py`
- `tests/test_deploy.py`

Current state:

- `install_units()` writes all rendered unit files into the systemd unit directory.
- It then runs `daemon-reload`.
- It enables each timer/path trigger one by one.
- If `daemon-reload` or a later `enable --now` fails, previously written unit files and previously
  enabled triggers are left in place. The result reports problems but does not roll back.

Why this matters:

M2 deployment should be trustworthy and repeatable. A partial install can leave some loops live and
others not, while the command exits nonzero. That is especially risky for unattended automation
because the operator may assume a failed install changed nothing.

Recommended fix:

- Install to a temporary staging directory or backup current units before replacing them.
- If any enable step fails, disable triggers already enabled in this invocation and remove or
  restore the copied units.
- If rollback is intentionally deferred, make the CLI output explicit: list exactly which units were
  copied, which triggers were enabled, and which cleanup commands the operator should run.
- Add tests for failure after one trigger was enabled.

## Notes On Healthy Areas

- The new scheduler/deploy path has broad offline unit coverage and the full local suite passed.
- `loopctl init` is idempotent and creates memory-tree directories without writing source files.
- `loopctl fleet` gives the terminal fleet table called out by the updated design.
- Required versus optional runtime dependency checks are split, so missing future runtimes no
  longer fail a Codex-only setup.
- Manifest IDs are now canonicalized and validated, which protects worktree and unit filename
  generation from path traversal.
