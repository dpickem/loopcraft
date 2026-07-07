# Milestone 2 Review 02 — Response

Response to [`2026_07_06_milestone_2_review_02_report.md`](2026_07_06_milestone_2_review_02_report.md),
the follow-up review of `feat/m2-control-plane` after the Review 01 fixes. Prior
history:
[01 report](2026_07_06_milestone_2_review_01_report.md) /
[01 response](2026_07_06_milestone_2_review_01_response.md).

The single finding is addressed, with regression tests.

## Commits

```text
646808a docs: add M2 review 02 report
71850b0 fix: address M2 review 02 finding (scheduled credential model)
```

The fix is in `71850b0`. Inspect with `git show 71850b0 -- <file>` using the
file list below.

## Status

Verification from the repo root:

```text
make test && make compile && make validate
```

- `make test`: **272 passed** (up from 265 at review time), fully offline.
- `make compile`: passes.
- `make validate`: passes, 3 manifests.

The report's focused reproduction now behaves correctly:

```text
env file has X_API_BEARER_TOKEN, process env does not
  -> scheduled apply preflight: NO x-api problem (token read from the file)

process env has X_API_BEARER_TOKEN, no environment_file configured
  -> scheduled apply preflight: x-api problem reported (the service would not
     inherit the shell token)
```

## Finding

### 1. Scheduled `EnvironmentFile` credentials are now used by adapter preflight — fixed

Review 01's `validate_environment()` treated `scheduler.environment_file` as the
authority for scheduled credentials, but adapter preflight (and `auth`) still
resolved env/auth through `config.env_value()`, which read only the operator's
process environment. That left two inconsistent credential models: a token only
in the environment file was rejected by preflight, and a token only in the shell
could pass preflight even though the deployed service would never see it.

The fix makes the *scheduled* environment the single authority for the
deployment commands, without changing direct `loopctl run`:

- **New scheduled-environment resolution on `LoopcraftConfig`.**
  `scheduled_env_value(name)` returns the value a systemd service would see —
  it reads `scheduler.environment_file` (via the shared `env.parse_env_file`) and
  nothing else (a service inherits neither the operator's process env nor
  `.env`). `for_scheduled_preflight()` returns a frozen copy with
  `scheduled_env=True`; on that copy, `env_value()` delegates to
  `scheduled_env_value()`. The default config is unchanged and still reads the
  live process environment.
- **`apply` preflight uses the scheduled config.** `plan_deployment(...,
  run_preflight=True)` now runs each loop's adapter preflight against
  `config.for_scheduled_preflight()`. Because `CodexRunner.preflight` routes all
  credential checks through `check_declared_capabilities()` /
  `probe_x_api_auth()` (both of which call `config.env_value()`), no probe code
  had to change — the overlay covers `depends_on.env` vars and auth-bundle
  tokens (notably `x-api`'s `X_API_BEARER_TOKEN` / `X_API_OAUTH2_ACCESS_TOKEN`
  choice) uniformly. This also resolves the second inconsistency: a token only in
  `.env`/shell no longer satisfies `apply`, matching `validate_environment()`.
- **`auth` reports the same scheduled model.** `loopctl auth` now probes auth
  bundles/APIs and resolves declared env vars against
  `config.for_scheduled_preflight()`, so it agrees with `apply` about what the
  deployed fleet will have (rather than the earlier "process env OR file"
  superset). The environment-file health item is unchanged.
- **Direct `loopctl run` is deliberately untouched.** It executes in the current
  process (which has `.env` loaded at the CLI boundary), so its preflight keeps
  using the live environment. Chosen over threading a `scheduled=` flag through
  every probe because the config overlay keeps the probe/runner interfaces
  identical and makes the two modes a single, testable switch.
- Files: `src/loopcraft/config.py` (accessor + overlay),
  `src/loopcraft/deploy.py` (`plan_deployment` uses the scheduled config),
  `src/loopcraft/cli.py` (`auth` uses the scheduled config), `README.md`
  (documents the model).
- Tests:
  - `tests/test_deploy.py::test_scheduled_preflight_accepts_token_from_env_file`
    (token only in the file → no `x-api` preflight problem);
  - `::test_scheduled_preflight_ignores_token_only_in_process_env`
    (token only in the shell, no file → `x-api` problem reported);
  - `::test_scheduled_preflight_satisfies_declared_env_from_env_file`
    (`depends_on.env` satisfied by the file during scheduled preflight);
  - `tests/test_config.py::test_env_value_reads_process_env_by_default`,
    `::test_scheduled_env_value_reads_environment_file`,
    `::test_scheduled_env_value_none_when_unconfigured`,
    `::test_for_scheduled_preflight_switches_env_source` (the accessor and that
    the direct config is left reading the shell);
  - the Review 01 `auth` env-file test
    (`tests/test_cli_m2.py::test_auth_env_var_satisfied_by_environment_file`)
    still passes under the stricter model.

## Notes on resolved areas

Review 02 confirmed the Review 01 fixes are materially in place (absolute
`ExecStart`, no staged writes on failed apply, user-scope `default.target` path
units, install rollback). Those are unchanged here. `event`-cadence deployment
remains deferred to M8, and live-VM install stays outside the offline suite
(`install_units` is exercised with a faked `systemctl` in both the success and
rollback paths).
