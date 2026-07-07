# Milestone 2 Review 06 — Report (post path-audit response)

Review target: local branch `feat/m2-control-plane` in
`/Users/dpickem/workspace/loopcraft`.

Reviewed state:

- `3a464d7 fix: address M2 review 05 findings (path-boundary audit)`
- `59eeb3d docs: add M2 review 05 response`

Reference baseline:

- `docs/review_notes/2026_07_06_milestone_2_review_05_report.md`
- `docs/review_notes/2026_07_06_milestone_2_review_05_response.md`
- M2 in `docs/loopcraft-implementation-design.html`
- `CONTRIBUTING.md`

This pass re-reviewed the branch after the Review 05 response, focusing on whether the consolidated
path fixes preserve the documented M2 checks and default uv-managed setup.

## Verification

Commands run from `/Users/dpickem/workspace/loopcraft`:

```text
make check
uv run loopctl validate ./loops
uv run loopctl apply ./loops --dry-run --skip-preflight
```

Result:

- `uv run loopctl validate ./loops`: passed, 3 manifests.
- `uv run loopctl apply ./loops --dry-run --skip-preflight`: failed.
- `make check`: failed because it runs the same `apply` dry run after `deps check`.

Observed output:

```text
planned 0 loop(s) from loops
dry run: 0 unit(s) would be written (nothing written)
make: *** [check] Error 1
```

Debugging `plan_deployment()` shows the plan fails before rendering any units:

```text
plan loops 0 [
  "scheduler.loopctl_bin 'loopctl' cannot be resolved to an executable on the scheduled PATH
   (set an absolute path in [scheduler].loopctl_bin or add its directory to [scheduler].path)"
] loops
```

## Findings

### 1. Default uv setup now fails `make check`

Relevant files:

- `loopcraft.toml`
- `Makefile`
- `src/loopcraft/deploy.py`
- `src/loopcraft/config.py`
- `docs/review_notes/2026_07_06_milestone_2_review_05_response.md`

Current state:

- Review 05 finding 7 was fixed by resolving a bare `scheduler.loopctl_bin` against the scheduled
  PATH rather than the operator PATH.
- The default config still leaves `scheduler.loopctl_bin` commented/defaulted to `loopctl`.
- The default `scheduler.path` is systemd's default service PATH, not the project `.venv/bin`.
- The project's documented workflow and Makefile use `uv run loopctl`, not a globally installed
  `loopctl`.
- As a result, the default branch state cannot render units in `make check`: `apply --dry-run
  --skip-preflight` plans zero loops and exits nonzero.

Why this matters:

This makes the Review 05 response incomplete. It fixed the unsafe operator-PATH lookup, but did not
replace it with a working default for the uv-managed project. `CONTRIBUTING.md` requires running
`make check`, and M2's core path is `loopctl apply` rendering units. A clean checkout following
`uv sync` should not need hidden local scheduler config just to pass the standard check target.

Recommended fix:

- Make the default scheduled command/path match the documented uv setup. Options:
  - during config load, if `.venv/bin/loopctl` exists under `source_path`, use that as the default
    scheduled command for render checks; or
  - set `scheduler.loopctl_bin` in `loopcraft.toml` to the repo `.venv/bin/loopctl` via a
    source-root-aware placeholder; or
  - change `make check` to pass a check-only scheduler override that uses the current `uv run`
    environment while preserving strict deploy/install behavior.
- Keep deploy/install strict: a real host should still configure an absolute command or validated
  scheduled PATH.
- Add a regression test that the repository's default `loopcraft.toml` plus `uv sync` environment
  makes `loopctl apply ./loops --dry-run --skip-preflight` render the three shipped loops.

## Notes

- The individual Review 05 path hardening changes may be correct in isolation.
- The regression is at the integration/default-configuration layer: the stricter scheduled PATH
  model is now incompatible with the repo's default uv workflow and the standard `make check`
  target.
