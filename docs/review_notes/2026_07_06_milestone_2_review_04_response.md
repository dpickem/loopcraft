# Milestone 2 Review 04 — Response

Response to [`2026_07_06_milestone_2_review_04_report.md`](2026_07_06_milestone_2_review_04_report.md),
the follow-up review of `feat/m2-control-plane` after the Review 03 fix. Prior
history:
[01 report](2026_07_06_milestone_2_review_01_report.md) /
[01 response](2026_07_06_milestone_2_review_01_response.md),
[02 report](2026_07_06_milestone_2_review_02_report.md) /
[02 response](2026_07_06_milestone_2_review_02_response.md),
[03 report](2026_07_06_milestone_2_review_03_report.md) /
[03 response](2026_07_06_milestone_2_review_03_response.md).

The single finding is addressed, with regression tests.

## Commits

```text
98921d3 docs: add M2 review 04 report
d0e8ee0 fix: address M2 review 04 finding (live probes on scheduled PATH)
```

The fix is in `d0e8ee0`. Inspect with `git show d0e8ee0 -- <file>` using the
file list below.

## Status

Verification from the repo root:

```text
make test && make compile && make validate
```

- `make test`: **283 passed** (up from 281 at review time), fully offline.
- `make compile`: passes.
- `make validate`: passes, 3 manifests.

The report's focused reproduction now passes: a Slack loop with fake `codex`
and `nv-tools` on `scheduler.path` and an **empty operator PATH** clears
scheduled preflight, because the live probe executes the scheduled `nv-tools`
(exit 0) instead of a bare command on the operator PATH.

## Finding

### 1. Live capability probes now execute on the scheduled PATH — fixed

Review 03 routed binary *existence* checks through `config.which()` (scheduled
PATH), but the live Slack probe still ran `run_probe(["nv-tools", ...])` — a bare
command that `subprocess.run` resolves on the operator's process PATH. So the
probe could validate one PATH (`config.which("nv-tools")` on `scheduler.path`)
and execute another (bare `nv-tools` on the operator PATH), letting `apply`
disagree with the unit it renders.

The fix keeps the check and the execution on the same binary and environment:

- **`probe_slack_api` executes the resolved binary.** It now takes
  `nv_tools = config.which("nv-tools")` and calls
  `run_probe([nv_tools, "slack", ...], env=config.probe_env())`, so the probe
  runs the exact absolute executable it validated. `run_probe`'s allowlist checks
  `cmd[0]`'s basename, so an absolute path to an allowlisted binary is accepted.
- **`run_probe` accepts an `env`.** A new optional `env` argument is passed
  through to `subprocess.run`, so a probe can run in the scheduled service
  environment rather than the ambient one.
- **`config.probe_env()` builds that environment.** In direct mode it returns
  None (inherit the operator env); in scheduled mode it returns the operator env
  with `PATH` overridden to `scheduled_path` and the scheduled `EnvironmentFile`
  values overlaid — so any child processes `nv-tools` itself spawns also resolve
  on the scheduled PATH, and the probe sees the scheduled credentials (matching
  the Review 02/03 model). Direct `loopctl run` probes are unchanged.
- Because a probe failure is a preflight problem, a scheduled probe that cannot
  run (or fails) blocks a default `apply` before any unit is written (Review 01
  gate), reported at `apply` rather than at runtime.
- Files: `src/loopcraft/runners/capabilities.py` (`probe_slack_api`),
  `src/loopcraft/probes.py` (`run_probe` `env` param),
  `src/loopcraft/config.py` (`probe_env`).
- Tests:
  - `tests/test_capabilities.py::test_slack_probe_executes_resolved_binary_on_scheduled_path`
    (the probe runs the absolute which()-resolved `nv-tools` with the scheduled
    `PATH` in its env, not the bare name);
  - `tests/test_deploy.py::test_scheduled_slack_probe_runs_on_scheduled_path`
    (the report's scenario: fake `codex`/`nv-tools` on `scheduler.path`, empty
    operator PATH, Slack loop clears scheduled preflight end to end).

## Notes on resolved areas

Review 04 confirmed the Review 03 scheduled-PATH model is in place for
`CodexRunner.preflight` and declared-tool existence checks, and that rendered
units carry `Environment=PATH=<scheduled_path>`. With this change the live
probes execute against that same PATH/environment, so the "checks one PATH,
executes another" gap is closed. The M2 "passes `apply`, fails under systemd"
class — entrypoint (Review 01), credentials (Review 02), binary existence
(Review 03), and now live probe execution (Review 04) — is aligned on the
scheduled service environment. `event`-cadence deployment remains deferred to
M8, and live-VM install stays outside the offline suite.
