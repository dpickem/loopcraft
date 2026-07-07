# Milestone 2 Review 06 — Response

Response to [`2026_07_06_milestone_2_review_06_report.md`](2026_07_06_milestone_2_review_06_report.md),
the post-path-audit review of `feat/m2-control-plane`. Prior history:
[01](2026_07_06_milestone_2_review_01_response.md),
[02](2026_07_06_milestone_2_review_02_response.md),
[03](2026_07_06_milestone_2_review_03_response.md),
[04](2026_07_06_milestone_2_review_04_response.md),
[05](2026_07_06_milestone_2_review_05_response.md) responses.

The single finding is addressed, with regression tests.

## Commits

```text
4da7084 docs: add M2 review 06 report
9dedab1 fix: address M2 review 06 finding (default uv loopctl for apply)
```

The fix is in `9dedab1`. Inspect with `git show 9dedab1 -- <file>`.

## Status

```text
make check && make test
```

- `make check`: **passes** — `apply ./loops --dry-run --skip-preflight` now plans
  the 3 shipped loops (arxiv-intel, slack-triage, x-intel) and exits 0.
- `make test`: **317 passed** (up from 313 at review time), fully offline.
- `make validate`: passes, 3 manifests.

Confirmed on a clean checkout's uv environment:

```text
planned 3 loop(s) from loops
  ...
ExecStart=/Users/.../loopcraft/.venv/bin/loopctl run arxiv-intel
```

## Finding

### 1. Default uv setup renders units in `make check` again — fixed

Review 05 finding 7 correctly stopped resolving a bare `scheduler.loopctl_bin`
on the operator PATH, but the default (`loopctl`) then could not be resolved on
the scheduled PATH for a clean `uv sync` checkout (where `loopctl` lives in
`.venv/bin`, not on systemd's default PATH), so `make check` planned zero loops
and exited nonzero.

The fix supplies a working default for the uv-managed project without weakening
deploy/install:

- **Config load defaults the scheduled command to the project's uv-managed
  loopctl.** In `LoopcraftConfig.load()`, when `[scheduler].loopctl_bin` is not
  set and `<source>/.venv/bin/loopctl` exists, the scheduled command defaults to
  that **absolute** path (via the new `_VENV_LOOPCTL_SUBPATH`). So a clean
  checkout renders units — `apply --dry-run --skip-preflight` and therefore
  `make check` pass — with no hidden local config, and the rendered `ExecStart`
  is an absolute executable (consistent with the Review 01 model).
- **Deploy/install stay strict.** An explicit `[scheduler].loopctl_bin` always
  wins, and a host without a project venv keeps the bare-name default that must
  be satisfied by an absolute command or a validated `[scheduler].path`. The
  Review 05 executable/scheduled-PATH checks are unchanged.
- Files: `src/loopcraft/config.py`, `loopcraft.toml` (documents the default).
- Tests:
  - `tests/test_config.py::test_config_defaults_loopctl_to_venv` (unset →
    defaults to `.venv/bin/loopctl`),
    `::test_config_respects_explicit_loopctl_bin` (explicit value wins),
    `::test_config_loopctl_default_stays_bare_without_venv` (no venv → strict
    bare name);
  - `tests/test_cli_m2.py::test_default_config_renders_shipped_loops` (the repo's
    default config + uv `.venv` renders the three shipped loops via
    `apply --dry-run --skip-preflight`; auto-skips if the project venv is absent).

## Notes

The individual Review 05 path-hardening changes are unchanged and remain correct;
this fix only restores the default-configuration/integration behavior the
stricter scheduled-PATH model had regressed. `event`-cadence deployment remains
deferred to M8, and live-VM install stays outside the offline suite.
