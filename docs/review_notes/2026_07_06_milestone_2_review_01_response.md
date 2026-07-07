# Milestone 2 Review 01 — Response

Response to [`2026_07_06_milestone_2_review_01_report.md`](2026_07_06_milestone_2_review_01_report.md),
the first review of the M2 scheduler/auth/apply control plane on
`feat/m2-control-plane`.

All five findings are addressed, each with regression tests.

## Commits

The reviewed M2 implementation was first committed as a baseline, then the
findings were fixed on top:

```text
3b60299 feat: add M2 control plane (scheduler, auth, apply, fleet)
929e685 fix: address M2 review 01 findings
```

The fixes are in `929e685`. Inspect with `git show 929e685 -- <file>` using the
file lists below.

## Status

Verification from the repo root:

```text
make test && make compile && make validate
```

- `make test`: **265 passed** (up from 251 at review time), fully offline.
- `make compile`: passes.
- `make validate`: passes, 3 manifests.

Focused confirmations:

```text
absolute ExecStart:   apply renders ExecStart=<abs>/.venv/bin/loopctl run <loop>
                      (resolved, not the bare name); an unresolvable loopctl_bin
                      makes the plan non-renderable with a named problem.
unmet-dep apply:      default `apply` writes NOTHING and exits rc 1; the staging
                      dir is not even created, so `fleet` cannot report `staged`
                      for a rejected loop. `--render-invalid` renders diagnostics
                      but still exits rc 1.
env authority:        a var absent from the process env but present in
                      scheduler.environment_file is reported satisfied; a missing
                      var, a missing file, or a file inside either git tree fails
                      apply.
user-scope path unit: WantedBy=default.target (user) vs multi-user.target (system).
install rollback:     a failing enable disables already-enabled triggers and
                      restores/removes written units.
```

## Findings

### 1. Rendered systemd services are now reliably runnable — fixed

The rendered `ExecStart` used the bare `scheduler.loopctl_bin` (`loopctl`),
which a systemd unit cannot rely on because it does not inherit the operator's
interactive shell PATH.

- `deploy.resolve_loopctl_command()` resolves the configured command to an
  **absolute** executable: it `shlex`-splits the value (so a multi-word prefix
  like `uv run loopctl` is supported), and resolves the first token via
  `shutil.which()` when it is not already an absolute existing path. The rendered
  `ExecStart` is therefore always absolute (verified: it resolves to
  `.../.venv/bin/loopctl` in the documented uv setup, with no dependency on the
  invoking shell PATH at service run time).
- `plan_deployment()` resolves the command **once** up front. Because a bad
  command breaks every unit, an unresolvable `loopctl_bin` is a fleet-wide
  render problem: nothing is rendered and `apply` fails with a named message
  telling the operator to set an absolute path in `[scheduler].loopctl_bin`.
- `scheduler.render_loop_units(..., loopctl_command=...)` takes the resolved
  command; `_render_service()` writes it into `ExecStart`.
- Files: `src/loopcraft/deploy.py`, `src/loopcraft/scheduler.py`,
  `README.md` (documents the absolute-resolution behavior).
- Tests: `tests/test_deploy.py::test_resolve_loopctl_command_absolute`,
  `::test_resolve_loopctl_command_unresolvable_is_problem`,
  `::test_plan_unresolvable_command_blocks_render`;
  `tests/test_scheduler.py::test_render_service_uses_explicit_loopctl_command`.
  The deploy render/install tests pin an absolute `loopctl_bin` to a created
  dummy executable, so they prove correct `ExecStart` without relying on the
  runner's PATH.

### 2. `apply` no longer writes staged units when preflight fails — fixed

Default `apply` wrote units whenever the plan was `renderable`, even when
preflight problems made it not `ok` — which let `fleet` report `staged` for a
loop whose deployment was actually rejected.

- Default `apply` is now **side-effect-free unless the plan is fully clean**
  (`plan.ok`). A plan blocked only by unmet dependencies (preflight or the new
  environment checks) writes nothing and exits nonzero, with a message pointing
  at the problems.
- Diagnostic rendering is opt-in via a new `--render-invalid` flag; it renders
  the units (labeled as diagnostics) but still exits nonzero, so it can never be
  mistaken for a successful deploy.
- `DeploymentPlan.renderable` still means "the unit text can be produced"
  (manifest + render clean); the write gate is now `plan.ok`, keeping the
  `deploy.py` docstring's "validated before anything is written" contract true
  for the default path.
- `--install` with unmet deps therefore also writes nothing and never installs.
- Files: `src/loopcraft/cli.py`.
- Tests (updated to encode the safe default):
  `tests/test_cli_m2.py::test_apply_reports_unmet_dependency_and_writes_nothing`,
  `::test_apply_render_invalid_writes_diagnostics`,
  `::test_apply_install_refused_and_writes_nothing_with_unmet_dependency`.

### 3. `apply`/`auth` now validate the scheduled service environment — fixed

Both commands only checked the interactive process environment, so `apply`
could pass on the operator's shell/`.env` while the scheduled service later
failed with different credentials.

- When `scheduler.environment_file` is configured it is treated as the
  authority for scheduled credentials. `deploy.validate_environment()` requires
  the file to **exist**, live **outside both** the source and memory trees
  (secrets stay out of git), and **contain every env var** the deployed loops
  declare. These become plan problems that block deploy/install (like preflight)
  without blocking diagnostic rendering.
- When no `environment_file` is configured but loops declare env vars, that gap
  is reported too — a scheduled service would not inherit `.env` or the shell.
- `auth` is environment-file aware: a declared env var counts as satisfied when
  it is in the process env **or** the configured file, and the file's own health
  (existence/placement) is surfaced as an `env-file` item. The shared
  `deploy.environment_file_health()` (built on a new reusable
  `env.parse_env_file()`) keeps `apply` and `auth` in agreement about which keys
  a service would see.
- For `system` scope with a `User=` and a configured `environment_file`, `apply`
  emits an explicit reminder to ensure that user can read the file (portable
  cross-user read-permission verification is deferred rather than faked).
- Files: `src/loopcraft/deploy.py`, `src/loopcraft/cli.py`,
  `src/loopcraft/env.py`, `README.md`.
- Tests: `tests/test_deploy.py::test_validate_environment_flags_unconfigured_env_file`,
  `::test_validate_environment_flags_missing_var`,
  `::test_validate_environment_accepts_complete_env_file`,
  `::test_validate_environment_rejects_in_tree_env_file`;
  `tests/test_cli_m2.py::test_auth_env_var_satisfied_by_environment_file`.

### 4. User-scope `.path` units now use `default.target` — fixed

`on-artifact` path units always rendered `WantedBy=multi-user.target`, a system
target that will not enable under a user manager.

- `scheduler.install_target(scope)` returns `default.target` for user scope and
  `multi-user.target` for system scope; `_render_path()` uses it. Cron timers
  keep `timers.target`, which is valid in both scopes.
- Files: `src/loopcraft/scheduler.py`.
- Tests: `tests/test_scheduler.py::test_on_artifact_path_unit_target_follows_scope`
  (asserts both scopes render the right target).

### 5. `apply --install` is now transactional with rollback — fixed

A failed `daemon-reload` or `enable --now` previously left already-written units
and already-enabled triggers in place, so a failed install could leave a
half-deployed fleet while exiting nonzero.

- `install_units()` now backs up each unit it overwrites (recording prior
  content, or `None` when the file is new), then writes units, `daemon-reload`s,
  and enables triggers one by one. On **any** failure it calls
  `_rollback_install()`, which disables the triggers enabled in this invocation
  and restores replaced units (or removes newly written ones), then
  `daemon-reload`s again. `InstallResult.rolled_back` records that this happened.
- Rollback is best-effort per step so one failing cleanup command cannot strand
  the rest.
- Files: `src/loopcraft/deploy.py`.
- Tests: `tests/test_deploy.py::test_install_rolls_back_on_enable_failure`,
  `::test_install_rollback_disables_already_enabled_triggers` (two loops; the
  first enabled trigger is disabled when the second fails),
  `::test_install_restores_replaced_unit_on_failure`. The happy-path
  `::test_install_units_copies_and_enables` still passes.

## Notes on healthy areas

Review 01 confirmed the scheduler/deploy path has broad offline coverage, that
`init` is idempotent and source-safe, that `fleet` provides the terminal table,
and that required-vs-optional dependency checks and manifest-id canonicalization
are in place — all unchanged by these fixes. `event`-cadence deployment remains
deferred to M8, and live-VM install (running `systemctl` on a real host) stays
outside the offline test suite; `install_units` is exercised with a faked
`systemctl` in both success and rollback paths.
