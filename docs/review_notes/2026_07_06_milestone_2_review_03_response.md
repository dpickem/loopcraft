# Milestone 2 Review 03 — Response

Response to [`2026_07_06_milestone_2_review_03_report.md`](2026_07_06_milestone_2_review_03_report.md),
the follow-up review of `feat/m2-control-plane` after the Review 02 fix. Prior
history:
[01 report](2026_07_06_milestone_2_review_01_report.md) /
[01 response](2026_07_06_milestone_2_review_01_response.md),
[02 report](2026_07_06_milestone_2_review_02_report.md) /
[02 response](2026_07_06_milestone_2_review_02_response.md).

The single finding is addressed, with regression tests.

## Commits

```text
0022d1c docs: add M2 review 03 report
5cbaa62 fix: address M2 review 03 finding (scheduled binary resolution)
```

The fix is in `5cbaa62`. Inspect with `git show 5cbaa62 -- <file>` using the
file list below.

## Status

Verification from the repo root:

```text
make test && make compile && make validate
```

- `make test`: **281 passed** (up from 272 at review time), fully offline.
- `make compile`: passes.
- `make validate`: passes, 3 manifests.

The report's focused probe now behaves correctly:

```text
tool visible only on the operator PATH, no scheduler.path
  -> scheduled apply preflight: problem "declared tool '...' not found on PATH"
     (the systemd service would not find it either)

scheduler.path includes the tool's dir
  -> scheduled apply preflight: no problem, and the rendered unit sets
     Environment=PATH to that same PATH
```

## Finding

### 1. Scheduled preflight now resolves runtime/tool binaries on the scheduled PATH — fixed

Review 01 made `ExecStart` absolute and Review 02 made credential resolution use
the scheduled `EnvironmentFile`, but binary lookup (`codex`, `nv-tools`, declared
tools) still used the operator's interactive PATH via `shutil.which(...)`. The
rendered service set no `PATH`, so a host could pass `apply` because the shell
found the binaries while the systemd service — with a smaller/default PATH, or a
different service user — could not.

The fix extends the scheduled-environment model (Review 02) to binaries, so
`apply` validates the exact PATH the unit runs with:

- **New `[scheduler].path`** — the PATH the scheduled service runs with.
  `LoopcraftConfig.scheduled_path` returns it (or systemd's default service
  PATH, `SYSTEMD_DEFAULT_PATH`, when unset).
- **`config.which(binary)`** mirrors `env_value`: in the default (direct) mode it
  resolves on the operator PATH; on a config marked for scheduled preflight
  (`for_scheduled_preflight()`) it resolves via `shutil.which(binary,
  path=self.scheduled_path)`. Because `apply` preflight already runs against the
  scheduled config (Review 02), routing binary lookups through `config.which`
  makes them respect the scheduled PATH with no change to probe signatures.
- **Runtime/tool lookups now go through `config.which`:** `CodexRunner.preflight`
  (`codex`), and the runtime-neutral `nv-tools` auth/slack probes and declared
  `depends_on.tools` checks in `capabilities`. Direct `loopctl run` keeps
  resolving on the process PATH.
- **The rendered service sets `Environment=PATH=<scheduled_path>`** — exactly the
  PATH preflight resolved against — so render and validation are aligned and the
  service does not fall back to systemd's ambient PATH. This closes the finding's
  "same PATH the service will get" requirement without leaving `build_command`'s
  bare `codex` invocation path-dependent: the unit's PATH is the validated one.
- A tool/runtime missing from the scheduled PATH is a preflight problem, so (via
  the Review 01 gate) default `apply` writes nothing and exits nonzero — reported
  at `apply`, not at runtime.
- Files: `src/loopcraft/config.py` (`scheduler.path`, `scheduled_path`,
  `which`), `src/loopcraft/scheduler.py` (rendered `PATH`),
  `src/loopcraft/runners/codex.py`, `src/loopcraft/runners/capabilities.py`
  (lookups via `config.which`), `loopcraft.toml` + `README.md` (docs).
- Tests:
  - `tests/test_config.py::test_which_scheduled_resolves_against_scheduled_path`
    (a tool on the operator PATH is invisible to scheduled `which` unless
    `scheduler.path` includes it), `::test_which_direct_uses_process_path`,
    `::test_scheduled_path_defaults_to_systemd_default`,
    `::test_scheduled_path_uses_configured_value`;
  - `tests/test_scheduler.py::test_service_renders_default_path`,
    `::test_service_renders_configured_path` (the unit carries the validated PATH);
  - `tests/test_deploy.py::test_scheduled_preflight_tool_missing_from_scheduled_path`
    (operator-PATH-only tool → problem),
    `::test_scheduled_preflight_tool_found_on_configured_path`;
  - `tests/test_cli_m2.py::test_apply_blocks_when_tool_missing_on_scheduled_path`
    (missing scheduled tool blocks apply before any unit is written).

## Notes on resolved areas

Review 03 confirmed the Review 02 credential fix is in place and the Review 01
fixes remain (absolute `ExecStart`, side-effect-free failed `apply`, user-scope
`default.target` path units, install rollback). `event`-cadence deployment stays
deferred to M8, and live-VM install remains outside the offline suite. With this
change the "passes `apply`, fails under systemd" class of gaps (entrypoint,
credentials, and now runtime/tool binaries) is closed for the scheduled model.
