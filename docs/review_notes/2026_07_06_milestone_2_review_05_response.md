# Milestone 2 Review 05 — Response

Response to [`2026_07_06_milestone_2_review_05_report.md`](2026_07_06_milestone_2_review_05_report.md),
the consolidated path-boundary audit of `feat/m2-control-plane`. Prior history:
[01](2026_07_06_milestone_2_review_01_response.md),
[02](2026_07_06_milestone_2_review_02_response.md),
[03](2026_07_06_milestone_2_review_03_response.md),
[04](2026_07_06_milestone_2_review_04_response.md) responses.

All 21 findings are addressed, each with regression tests.

## Commits

```text
1d059e3 docs: add M2 review 05 report
3a464d7 fix: address M2 review 05 findings (path-boundary audit)
```

The fixes are in `3a464d7`. Inspect with `git show 3a464d7 -- <file>`.

## Status

```text
make test && make compile && make validate
```

- `make test`: **313 passed** (up from 283 at review time), fully offline.
- `make compile`: passes.
- `make validate`: passes, 3 manifests.

## Findings

### 1. `loopctl_bin` must be an executable regular file — fixed

`resolve_loopctl_command()` now requires an absolute `loopctl_bin` to satisfy
`Path.is_file()` and `os.access(..., X_OK)` (and `shutil.which` already enforces
X_OK for a bare name), returning `scheduler.loopctl_bin '<path>' is not an
executable file` otherwise. Files: `src/loopcraft/deploy.py`. Tests:
`test_deploy.py::test_resolve_loopctl_command_rejects_non_executable`,
`::test_resolve_loopctl_command_rejects_directory`.

### 2. Invalid `environment_file` file types no longer crash `apply` — fixed

`environment_file_health()` guards `is_file()` before parsing and catches
`OSError`; `env.parse_env_file()` now returns `{}` for a non-file instead of
raising. A directory/unreadable path is a structured `env_problems` entry. Files:
`src/loopcraft/deploy.py`, `src/loopcraft/env.py`. Test:
`test_deploy.py::test_environment_file_directory_is_problem_not_crash`.

### 3. Relative `environment_file` rejected; expanded path rendered — fixed

`environment_file_health()` requires the (`~`-expanded) path to be absolute;
`config.rendered_environment_file` renders the expanded absolute path in the
unit. Files: `src/loopcraft/deploy.py`, `src/loopcraft/config.py`,
`src/loopcraft/scheduler.py`. Test:
`test_deploy.py::test_environment_file_relative_is_rejected`.

### 4. `scheduler.path` must be absolute components — fixed

`SchedulerConfig.problems()` rejects relative, empty (current-dir), and `..`
components (via `_abs_path_list_problems`), and `config.scheduled_path` expands
`~` per component for both `which()` and rendering. `plan_deployment` folds these
into `render_problems`. Files: `src/loopcraft/config.py`,
`src/loopcraft/deploy.py`. Tests:
`test_config.py::test_scheduler_problems_flag_relative_path_entry`,
`::test_scheduled_path_expands_user`.

### 5. `unit_prefix` cannot escape the unit directory — fixed

`SchedulerConfig.problems()` validates `unit_prefix` against
`^[A-Za-z0-9_.-]+$`; `write_units()`/`install_units()` additionally route every
filename through `_safe_unit_dest()` (must be a single path component). Files:
`src/loopcraft/config.py`, `src/loopcraft/deploy.py`. Tests:
`test_config.py::test_scheduler_problems_flag_bad_unit_prefix`,
`test_deploy.py::test_plan_flags_bad_unit_prefix`,
`::test_write_units_refuses_escaping_filename`.

### 6. X follow-candidate `--output-dir` stays in the ledger — fixed

`IntelStore.follow_candidates_dir()` requires a `state/...` path resolved through
`LoopcraftConfig.resolve_state_path()`; the CLI surfaces a violation as a
structured failure. Files: `src/loopcraft/research_intel/x/store.py`,
`src/loopcraft/research_intel/x/cli.py`. Test:
`test_x_intel.py::test_follow_candidates_dir_rejects_non_state_output`.

### 7. Bare `loopctl_bin` resolves on the scheduled PATH — fixed

`resolve_loopctl_command()` resolves a bare command with
`shutil.which(head, path=config.scheduled_path)`, so a binary only on the
operator PATH does not satisfy `apply`. File: `src/loopcraft/deploy.py`. Test:
`test_deploy.py::test_resolve_loopctl_bare_uses_scheduled_path`.

### 8. In-tree `environment_file` symlinks are rejected — fixed

`environment_file_health()` now checks **lexical** (pre-resolve) containment
under source/memory and rejects a symlink outright, so a tracked stub pointing
at an external secret is refused. Files: `src/loopcraft/deploy.py`. Test:
`test_deploy.py::test_environment_file_in_tree_symlink_rejected`.

### 9. User-scope unit dir honors `XDG_CONFIG_HOME` — fixed

`systemd_unit_dir()` returns `$XDG_CONFIG_HOME/systemd/user` when set, else
`~/.config/systemd/user`. File: `src/loopcraft/deploy.py`. Test:
`test_deploy.py::test_systemd_unit_dir_honors_xdg`.

### 10. Multi-word `loopctl_bin` is systemd-escaped — fixed

The command is carried as `list[str]`; `render_exec_start()` quotes each argv
token for systemd's parser (double-quote + escape when needed). Files:
`src/loopcraft/scheduler.py`, `src/loopcraft/deploy.py`. Tests:
`test_scheduler.py::test_exec_start_quotes_arguments_with_spaces`,
`::test_exec_start_preserves_multiword_command`.

### 11. `apply --out` refuses source-tree paths by default — fixed

`_cmd_apply()` rejects `--out` under the source tree (lexical + resolved) unless
`--allow-source-output` is given. Files: `src/loopcraft/cli.py`. Tests:
`test_cli_m2.py::test_apply_out_into_source_is_refused`,
`::test_apply_out_into_source_allowed_with_flag`.

### 12. Install/rollback refuse symlinked unit destinations — fixed

`install_units()` refuses a `dest.is_symlink()` (and non-plain filenames) and
rolls back; the symlink target is never written. File: `src/loopcraft/deploy.py`.
Test: `test_deploy.py::test_install_refuses_symlinked_unit`.

### 13. Scheduled probes use a minimal environment — fixed

`config.probe_env()` (scheduled) starts from a small allowlist
(`HOME`/`USER`/locale/...) plus the scheduled `PATH`,
`LOOPCRAFT_SOURCE`/`LOOPCRAFT_MEMORY`, and the `EnvironmentFile`, so an
operator-only variable (e.g. `HTTPS_PROXY`) cannot influence a probe. File:
`src/loopcraft/config.py`. Test:
`test_config.py::test_probe_env_excludes_operator_only_vars`.

### 14. `discover-follows --digest-json` is ledger-bound — fixed

The flag must be a `state/...` path resolved through `LoopcraftConfig`; an
absolute/traversing value is a structured failure. Files:
`src/loopcraft/research_intel/x/cli.py`. Test:
`test_x_intel.py::test_x_discover_follows_rejects_absolute_digest`.

### 15. `snapshot-following --output` is source-bound — fixed

`snapshot_following()` resolves `--output` through `resolve_source_path()` before
any fetch, rejecting absolute/traversing paths. File:
`src/loopcraft/research_intel/x/cli.py`. Test:
`test_x_intel.py::test_x_snapshot_following_rejects_absolute_output`.

### 16. Direct arXiv/X `--config` is source-bound — fixed

Both CLIs resolve `--config` through the new
`LoopcraftConfig.resolve_content_config()`, which rejects absolute/traversing
paths. Files: `src/loopcraft/research_intel/{arxiv,x}/cli.py`,
`src/loopcraft/config.py`. Tests:
`test_x_intel.py::test_x_run_rejects_absolute_config`,
`test_config.py::test_resolve_content_config_rejects_absolute`,
`::test_resolve_content_config_rejects_traversal`, and the arXiv CLI tests
updated to source-relative configs.

### 17. Direct config loaders contain `.local` overrides — fixed

`resolve_content_config()` resolves the effective public/`.local` file and
asserts it stays under the source tree (symlinks resolved), matching preflight/
staging. File: `src/loopcraft/config.py`. Tests:
`test_config.py::test_resolve_content_config_prefers_local`,
`::test_resolve_content_config_rejects_local_symlink_escape`.

### 18. X following snapshot resolves under source — fixed

`XIntelRunner._resolved_snapshot()` resolves the declared snapshot through
`resolve_source_path()`; both the fetch and follow-discovery paths use it, so
the file no longer depends on cwd. File: `src/loopcraft/research_intel/x/cli.py`.
Test: `test_x_intel.py::test_resolved_snapshot_uses_source_tree`.

### 19. `.env` loads from the source root — fixed

`loopctl main()` and `XIntelRunner.__init__`/`snapshot_following()` resolve the
config first, then `load_dotenv(config.source_path / ".env")`. Files:
`src/loopcraft/cli.py`, `src/loopcraft/research_intel/x/cli.py`. Test:
`test_cli.py::test_dotenv_loaded_from_source_root`.

### 20. `loopctl logs` constrains `log_path` — fixed

`_cmd_logs()` asserts the record's `log_path` resolves under the worktree log
root before reading, so a hand-edited record cannot make `logs` read arbitrary
files. File: `src/loopcraft/cli.py`. Test:
`test_cli.py::test_logs_refuses_external_log_path`.

### 21. `write_follow_candidates()` uses `contained_child()` — fixed

Date-stamped follow-candidate filenames now go through `contained_child()`,
matching the digest writers. File: `src/loopcraft/research_intel/x/store.py`.
Test: `test_x_intel.py::test_write_follow_candidates_contains_date_stamp`.

## Notes on non-issues

The report's confirmed non-issues (state-path guarding, source-relative
skill/verify/content assets, worktree symlink defense, `contained_child` around
run/date stamps, the Review 04 live-probe fix) are unchanged. `event`-cadence
deployment remains deferred to M8, and live-VM install stays outside the offline
suite.
