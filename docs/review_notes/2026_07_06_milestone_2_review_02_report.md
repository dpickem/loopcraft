# Milestone 2 Review 02 — Report (response follow-up)

Review target: local branch `feat/m2-control-plane` in
`/Users/dpickem/workspace/loopcraft`.

Reviewed commits:

- `3b60299 feat: add M2 control plane (scheduler, auth, apply, fleet)`
- `929e685 fix: address M2 review 01 findings`
- `24a19ba docs: add M2 review 01 response`

Reference baseline:

- `docs/review_notes/2026_07_06_milestone_2_review_01_report.md`
- `docs/review_notes/2026_07_06_milestone_2_review_01_response.md`
- M2 in `docs/loopcraft-implementation-design.html`
- `CONTRIBUTING.md`

This pass re-reviewed the branch after the Review 01 response, focusing on whether the
systemd `ExecStart`, failed-apply side effects, scheduled environment validation, user-scope path
units, and install rollback fixes were actually reflected in the current implementation.

## Verification

Commands run from `/Users/dpickem/workspace/loopcraft`:

```text
make test && make compile && make validate
make check
```

Result:

- `make test`: passed, 265 tests.
- `make compile`: passed.
- `make validate`: passed, 3 manifests.
- `make check`: passed in this environment.

Focused reproduction for the remaining finding:

```text
env_problems= []
preflight_problems= ["demo: auth bundle 'x-api': X_API_BEARER_TOKEN or X_API_OAUTH2_ACCESS_TOKEN is required"]
ok= False
```

That reproduction used a temporary manifest with `depends_on.auth: [x-api]`, no token in the
process environment, and `scheduler.environment_file` containing `X_API_BEARER_TOKEN=dummy`.
`validate_environment()` accepted the scheduled env file, but adapter preflight still rejected the
loop because the token was not in `os.environ`.

## Findings

### 1. Scheduled `EnvironmentFile` credentials are still not used by adapter preflight

Relevant files:

- `src/loopcraft/deploy.py`
- `src/loopcraft/runners/capabilities.py`
- `src/loopcraft/cli.py`
- `loops/x-intel.yaml`
- `tests/test_cli_m2.py`
- `tests/test_deploy.py`
- `tests/test_capabilities.py`

Current state:

- Review 01 finding 3 added scheduled environment validation:
  `validate_environment()` treats `scheduler.environment_file` as the authority for scheduled
  service credentials and verifies declared env vars exist there.
- `loopctl auth` also counts a declared env var as satisfied when it exists in either the process
  environment or the configured environment file.
- Adapter preflight still calls `check_declared_capabilities()`.
- `check_declared_capabilities()` checks manifest `depends_on.env` via `config.env_value()`.
- `probe_x_api_auth()` checks X credentials via `config.env_value("X_API_BEARER_TOKEN")` and
  `config.env_value("X_API_OAUTH2_ACCESS_TOKEN")`.
- `config.env_value()` reads only the current process environment.

Why this matters:

The M2 design requires unmet dependencies to be reported at `apply`, using the same environment the
scheduled service will actually see. The current code has two inconsistent credential models:

- A token only in `scheduler.environment_file` is accepted by scheduled-env validation but rejected
  by adapter preflight, so `apply` fails even though the rendered unit would have the token.
- A token only in the operator's `.env` or shell can satisfy adapter preflight, while a systemd
  service without a configured `EnvironmentFile` will not inherit that token and can fail at
  runtime.

This is most visible for `x-intel`, because `loops/x-intel.yaml` declares `auth: [x-api]` and the
`x-api` probe owns the choice between `X_API_BEARER_TOKEN` and `X_API_OAUTH2_ACCESS_TOKEN`.

Recommended fix:

- Add a scheduled-environment accessor on `LoopcraftConfig` or a small deployment context, for
  example `config.scheduled_env_value(name)` or `config.env_values_for_scheduled_service()`, that
  merges/chooses values according to the M2 rules.
- Use that accessor in `check_declared_capabilities()` and `probe_x_api_auth()` when preflight is
  running for `apply`/scheduled deployment.
- Keep direct `loopctl run` behavior clear: it can continue to use process env + `.env`, but
  `apply` should validate what the systemd service will see.
- Add regression tests:
  - env file contains `X_API_BEARER_TOKEN`, process env does not, and `plan_deployment(...,
    run_preflight=True)` has no `x-api` preflight problem;
  - process env contains `X_API_BEARER_TOKEN`, no `scheduler.environment_file` is configured, and
    `apply` reports that scheduled credentials are not configured rather than passing only because
    the operator shell has a token;
  - a manifest with `depends_on.env` is satisfied by `scheduler.environment_file` during scheduled
    preflight.

## Notes On Resolved Areas

- Review 01's absolute `ExecStart` issue is materially fixed: rendered units now use a resolved
  absolute loopctl command.
- Default `apply` no longer writes staged units when the plan has unmet dependency problems.
- User-scope path units now render `WantedBy=default.target`.
- `install_units()` now rolls back files and enabled triggers on install failure.
- The remaining issue is confined to making the scheduled credential model consistent across
  `validate_environment()`, `auth`, and adapter preflight.
