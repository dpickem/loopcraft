# Milestone 2 Review 05 — Report (consolidated path audit)

Review target: local branch `feat/m2-control-plane` in
`/Users/dpickem/workspace/loopcraft`.

Reviewed state:

- `d0e8ee0 fix: address M2 review 04 finding (live probes on scheduled PATH)`
- `9aa04b4 docs: add M2 review 04 response`

Reference baseline:

- `docs/review_notes/2026_07_06_milestone_2_review_04_report.md`
- `docs/review_notes/2026_07_06_milestone_2_review_04_response.md`
- M2 in `docs/loopcraft-implementation-design.html`
- `CONTRIBUTING.md`

This pass deliberately broadened the review from a single PATH symptom to a full path-boundary
audit across scheduler/deploy, systemd unit rendering, scheduled PATH/EnvironmentFile handling,
unit filename generation, worktree/source staging, and research-loop memory writes. It focuses on
places where `apply` can accept an unusable path, validate a different path than systemd will use,
or raise instead of returning a structured validation problem.

## Verification

Commands run from `/Users/dpickem/workspace/loopcraft`:

```text
make test && make compile && make validate && make check
```

Result:

- `make test`: passed, 283 tests.
- `make compile`: passed.
- `make validate`: passed, 3 manifests.
- `make check`: passed in this environment.

Focused probes:

```text
absolute loopctl_bin exists but is not executable
  -> resolve_loopctl_command returns it with no problem
  -> plan_deployment renderable=True

scheduler.environment_file points at a directory
  -> environment_file_health raises IsADirectoryError
  -> plan_deployment raises IsADirectoryError

scheduler.environment_file is relative and exists in apply cwd
  -> environment_file_health passes
  -> rendered unit would contain EnvironmentFile=secrets.env

scheduler.path contains a relative entry ("tools")
  -> apply preflight resolves it relative to apply cwd
  -> systemd resolves it relative to WorkingDirectory=<source>

scheduler.unit_prefix contains "../"
  -> write_units writes paths such as out/../../escape-demo.service

loopctl_bin is a bare name visible only on the operator PATH
  -> apply resolves it and renders an absolute operator-local path
  -> the scheduled service may run as another user without access to that path

environment_file is a symlink inside a git tree pointing outside
  -> placement validation passes because assert_under resolves the symlink target

user-scope unit dir with XDG_CONFIG_HOME set
  -> install path remains ~/.config/systemd/user, not $XDG_CONFIG_HOME/systemd/user

direct X follow-candidate --output-dir is absolute/traversing
  -> write_follow_candidates writes outside the memory ledger

direct X discover-follows --digest-json is absolute/traversing
  -> discover-follows reads arbitrary filesystem JSON

direct X snapshot-following --output is absolute/traversing
  -> snapshot-following writes outside source/memory

direct research CLI --config is absolute/traversing
  -> arXiv/X config loaders read outside the source tree

direct CLI .local config sibling is a symlink escaping source
  -> ArxivIntelConfig.load / IntelConfig.load read it without containment

loopctl logs reads a hand-edited run record log_path
  -> logs can read arbitrary paths outside the memory worktree/log area
```

## Findings

### 1. `scheduler.loopctl_bin` accepts an existing but non-executable file

Relevant files:

- `src/loopcraft/deploy.py`
- `src/loopcraft/config.py`
- `tests/test_deploy.py`

Current state:

- `resolve_loopctl_command()` treats an absolute `scheduler.loopctl_bin` as valid when
  `Path(head).exists()` is true.
- It does not require the path to be a regular executable file.
- `plan_deployment()` therefore renders services for a non-executable file such as
  `/tmp/not-executable-loopctl` with mode `0644`.

Why this matters:

M2's `apply` is supposed to surface host dependency problems before runtime. A non-executable
`ExecStart` path is guaranteed to fail under systemd, but currently passes render validation. This
is the same deployment-boundary class as the earlier bare-`loopctl` finding: the unit text can be
generated, but it is not runnable.

Recommended fix:

- In `resolve_loopctl_command()`, require the resolved executable to be a regular executable file
  (`Path.is_file()` plus `os.access(path, os.X_OK)`), both for absolute paths and paths resolved
  through `shutil.which()`.
- Return a render problem such as `scheduler.loopctl_bin '<path>' is not executable`.
- Add tests for:
  - absolute path exists but is not executable;
  - absolute path is a directory;
  - bare command resolved by `PATH` remains accepted only when executable.

### 2. Invalid `scheduler.environment_file` file types can crash `apply`

Relevant files:

- `src/loopcraft/deploy.py`
- `src/loopcraft/env.py`
- `tests/test_deploy.py`

Current state:

- `environment_file_health()` checks whether `scheduler.environment_file` exists and whether it is
  outside the source/memory trees.
- It then calls `parse_env_file(path)`.
- `parse_env_file()` calls `path.read_text()` when the path exists.
- If `scheduler.environment_file` points at a directory, `environment_file_health()` raises
  `IsADirectoryError`, and `plan_deployment()` raises rather than returning a structured plan
  problem.

Why this matters:

`apply` should aggregate deployment problems and report them without traceback. A misconfigured
environment file path is a normal host setup error, not an exceptional crash. This violates the M2
pattern established for manifests, render problems, and preflight errors: validation should fail
closed with a readable problem.

Recommended fix:

- In `environment_file_health()`, require `Path.is_file()` before parsing.
- Catch `OSError` from parsing and return a problem instead of raising.
- Consider making `parse_env_file()` itself robust for non-files if it is intended as a generic
  helper.
- Add tests for:
  - `scheduler.environment_file` points at a directory;
  - unreadable environment file reports a problem and does not raise;
  - `plan_deployment()` includes the problem in `env_problems`.

### 3. Relative `scheduler.environment_file` paths validate against the wrong context

Relevant files:

- `src/loopcraft/deploy.py`
- `src/loopcraft/scheduler.py`
- `src/loopcraft/config.py`
- `loopcraft.toml`

Current state:

- `environment_file_health()` resolves `scheduler.environment_file` with `Path(env_file).expanduser()`.
- For a relative path, that checks relative to the current `apply` process working directory.
- The rendered systemd unit keeps the original configured value:
  `EnvironmentFile=<scheduler.environment_file>`.
- In a focused probe, `environment_file = "secrets.env"` passed validation when `secrets.env`
  existed in the `apply` cwd, but the rendered unit would still contain `EnvironmentFile=secrets.env`.

Why this matters:

Systemd `EnvironmentFile=` should be an absolute host path for reliable unattended deployment. A
relative path is ambiguous: `apply` validates it relative to one cwd, while systemd may reject it or
interpret it differently. This breaks the "same environment apply validated" invariant.

Recommended fix:

- Require `scheduler.environment_file` to be absolute after `expanduser()`.
- Render the expanded absolute path, not the raw configured string.
- If `~` is allowed in `loopcraft.toml`, expand it before both validation and unit rendering.
- Add tests for:
  - relative `environment_file` is rejected with a clear problem;
  - `~` expands to an absolute path and the rendered unit uses the expanded path;
  - absolute file outside source/memory remains accepted.

### 4. Relative `scheduler.path` entries make `apply` and systemd resolve different binaries

Relevant files:

- `src/loopcraft/config.py`
- `src/loopcraft/scheduler.py`
- `src/loopcraft/runners/capabilities.py`
- `tests/test_config.py`
- `tests/test_deploy.py`

Current state:

- `scheduler.path` is an arbitrary string.
- Scheduled preflight uses `shutil.which(binary, path=config.scheduled_path)`.
- The rendered service uses `WorkingDirectory=<source>` and `Environment=PATH=<scheduled_path>`.
- Relative PATH entries are resolved relative to the process cwd at execution time. In a focused
  probe, `scheduler.path = "tools"` was resolved by `apply` relative to the `apply` cwd, while the
  systemd service would resolve it relative to `WorkingDirectory=<source>`.

Why this matters:

Review 03/04 aligned preflight and systemd around a shared PATH string, but relative PATH entries
still mean that string resolves to different directories depending on cwd. M2 deployment should not
depend on the operator running `apply` from the same directory that systemd later uses.

Recommended fix:

- Validate `scheduler.path` as a colon-separated list of absolute directories.
- Reject empty components (which mean current directory), relative entries, and traversal-ish
  entries.
- Optionally normalize/expand `~` before rendering.
- Add tests for:
  - relative PATH entry rejected;
  - empty PATH component rejected;
  - absolute PATH entries accepted and rendered exactly after normalization.

### 5. `scheduler.unit_prefix` can escape the unit output directory

Relevant files:

- `src/loopcraft/scheduler.py`
- `src/loopcraft/deploy.py`
- `src/loopcraft/config.py`
- `tests/test_scheduler.py`
- `tests/test_deploy.py`

Current state:

- `SchedulerConfig.unit_prefix` is a free string.
- `unit_name()` returns `f"{config.scheduler.unit_prefix}{loop_id}.{kind.value}"`.
- `write_units()` writes each rendered unit to `out_dir / unit.filename`.
- `install_units()` writes each rendered unit to `unit_dir / unit.filename`.
- If `unit_prefix` contains path separators or traversal, `unit.filename` is not a filename.
  A focused probe with `unit_prefix="../../escape-"` produced paths such as
  `out/../../escape-demo.service`.

Why this matters:

Rendered unit filenames are generated files and must remain under the staging directory or systemd
unit directory. A configurable prefix that can contain `/`, `..`, absolute-path syntax, or glob-ish
special characters is a path escape and install-safety issue. It also affects `fleet` install-state
checks, which use glob patterns built from the prefix.

Recommended fix:

- Validate `unit_prefix` as a safe filename prefix, for example `^[A-Za-z0-9_.-]+$`, and reject
  prefixes containing `/`, `..`, path separators, whitespace, glob metacharacters, or absolute path
  syntax.
- In `write_units()` and `install_units()`, defense-in-depth check that `out_dir / unit.filename`
  and `unit_dir / unit.filename` remain under the target directory before writing.
- Add tests for:
  - `unit_prefix="../"` is rejected before rendering;
  - `write_units()` refuses any rendered filename escaping `out_dir`;
  - `install_units()` refuses any rendered filename escaping the systemd unit dir.

### 6. Direct X follow-candidate output can write outside the memory ledger

Relevant files:

- `src/loopcraft/research_intel/x/store.py`
- `src/loopcraft/research_intel/x/cli.py`
- `CONTRIBUTING.md`

Current state:

- `IntelStore.follow_candidates_dir(output_dir)` returns `self.resolve(Path(output_dir))` when an
  `--output-dir` argument is provided.
- `IntelStore.resolve()` only maps `state/...` paths into the memory ledger; otherwise it returns
  the path as given.
- `write_follow_candidates()` writes markdown/JSON files under that directory.
- Therefore `loopcraft-x-intel discover-follows --output-dir /tmp/foo` or a traversing relative
  path writes outside the memory tree.

Why this matters:

`CONTRIBUTING.md` says loop output lives in the configured memory tree, and new control-plane state
should not write local outputs outside it. The direct CLI is still part of the shipped loopcraft
surface, and this output path is durable loop output. If arbitrary external output directories are
intended for an interactive debug command, that should be explicit and guarded; otherwise it should
use the same `state/...` vocabulary as the rest of the loop outputs.

Recommended fix:

- Prefer requiring `--output-dir` to be a `state/...` path, resolved through `LoopcraftConfig`.
- If arbitrary paths are intentionally allowed for debugging, rename/document the flag as an
  explicit export path, reject traversal/relative surprises, and ensure it is not used by the loop
  manifest/control-plane path.
- Add tests for absolute and traversing `--output-dir` values.

### 7. Bare `scheduler.loopctl_bin` resolves on the operator PATH, not the scheduled PATH

Relevant files:

- `src/loopcraft/deploy.py`
- `src/loopcraft/config.py`
- `src/loopcraft/scheduler.py`
- `tests/test_deploy.py`

Current state:

- `resolve_loopctl_command()` supports a non-absolute `scheduler.loopctl_bin`.
- For a bare command, it calls `shutil.which(head)` with no explicit path.
- Scheduled runtime/tool resolution now uses `config.which()` and `scheduler.path`, but
  `loopctl_bin` resolution still uses the operator process PATH.

Why this matters:

The primary `ExecStart` command can be resolved from the operator shell while the rendered service
runs under a different user and different PATH. For example, `loopctl_bin = "loopctl"` can resolve
to a user-local `.venv/bin/loopctl` during `apply`; systemd will execute that absolute path later,
but a system-scope service running as `User=loopcraft` may not have permission to traverse the
operator's home/workspace path. The docstring says this is resolved for the "systemd context", but
the current lookup is still interactive-shell based.

Recommended fix:

- Prefer requiring `scheduler.loopctl_bin` to be absolute for deploy/install, or resolve it against
  `scheduler.path` instead of the operator PATH.
- If operator-PATH resolution remains allowed, validate that the resolved absolute path is
  executable by the configured service user/scope, or make the warning explicit and non-successful
  for `--install`.
- Add tests where a bare `loopctl_bin` exists only on the operator PATH but not on
  `scheduler.path`.

### 8. In-tree `scheduler.environment_file` symlinks can bypass the "outside git trees" rule

Relevant files:

- `src/loopcraft/deploy.py`
- `src/loopcraft/paths.py`
- `tests/test_deploy.py`

Current state:

- `environment_file_health()` checks placement with `assert_under(root, path)`.
- `assert_under()` resolves symlinks before testing containment.
- The validation logic treats `assert_under()` raising as "good, outside this tree".
- Therefore, an environment file path lexically inside the source or memory tree that is a symlink
  to an external file passes the "outside both git trees" check.

Why this matters:

The security model says secrets stay outside both git trees. A symlink stub inside the memory or
source repo can itself be tracked, copied, reviewed, or deployed as part of repo state while
pointing at a secret-bearing external file. That violates the intent even though the resolved
target is outside the tree.

Recommended fix:

- Check both lexical containment and resolved containment for `scheduler.environment_file`.
- Reject any configured environment-file path that is under `source_path` or `memory_path` before
  resolving symlinks.
- Also reject symlink environment-file paths if the policy is "regular secret file only".
- Add a test with `memory_path/secrets.env -> /tmp/real-secrets.env`.

### 9. User-scope unit directory ignores `XDG_CONFIG_HOME`

Relevant files:

- `src/loopcraft/deploy.py`
- `src/loopcraft/config.py`
- `tests/test_deploy.py`

Current state:

- `systemd_unit_dir()` returns `Path.home() / ".config/systemd/user"` for user-scope units.
- It does not consult `XDG_CONFIG_HOME`.
- `systemctl --user` follows the XDG user configuration directory convention.

Why this matters:

On hosts with `XDG_CONFIG_HOME` set, `apply --install` can copy units to `~/.config/systemd/user`
while `systemctl --user` looks under `$XDG_CONFIG_HOME/systemd/user`. The install can appear to
succeed in tests or partial environments but put units in the wrong location for the actual user
manager.

Recommended fix:

- Resolve user-scope systemd unit dir as:
  - `$XDG_CONFIG_HOME/systemd/user` when `XDG_CONFIG_HOME` is set;
  - otherwise `~/.config/systemd/user`.
- Add tests for both cases.

### 10. Multi-word `scheduler.loopctl_bin` is rendered as an unescaped systemd command line

Relevant files:

- `src/loopcraft/deploy.py`
- `src/loopcraft/scheduler.py`
- `loopcraft.toml`
- `tests/test_scheduler.py`

Current state:

- `resolve_loopctl_command()` intentionally supports multi-word command prefixes such as
  `uv run loopctl`.
- It resolves the first token and returns `" ".join([resolved, *rest])`.
- `_render_service()` writes `ExecStart={loopctl_command} run {manifest.id}` directly.

Why this matters:

Systemd `ExecStart=` has its own command-line parsing and escaping rules. Rejoining a `shlex` split
command with plain spaces is fragile, especially for paths or arguments containing spaces or
characters that need systemd escaping. This can make the documented `uv run loopctl` style render a
unit that does not execute the intended argv.

Recommended fix:

- Represent the command internally as `list[str]`, not a joined string.
- Render `ExecStart=` with systemd-safe escaping/quoting for each argument.
- Add tests for:
  - `loopctl_bin = "uv run loopctl"`;
  - an absolute executable path with spaces;
  - a configured argument containing shell-sensitive characters.

### 11. `apply --out` can write generated units into arbitrary directories, including source

Relevant files:

- `src/loopcraft/cli.py`
- `src/loopcraft/deploy.py`
- `src/loopcraft/config.py`
- `tests/test_cli_m2.py`

Current state:

- `loopctl apply --out DIR` passes `Path(out)` directly to `write_units()`.
- `write_units()` creates the directory and writes generated unit files there.
- There is no guard preventing `--out` from pointing inside the source repo or another surprising
  location.

Why this matters:

Generated systemd units are build/deploy artifacts. The default path correctly uses
`<memory>/var/systemd`, but `--out ./loops/generated` or another source-tree path can write
generated files into the source repo, contrary to the source/memory separation rules.

Recommended fix:

- By default, require `--out` to be under the memory tree or outside the source tree.
- If arbitrary `--out` is intentionally supported as an export/debug feature, document that clearly
  and still reject traversal/symlink escapes and source-tree paths unless an explicit
  `--allow-source-output` flag is used.
- Add tests for `--out` under source and under memory.

### 12. Install and rollback follow symlinked unit paths in the destination directory

Relevant files:

- `src/loopcraft/deploy.py`
- `tests/test_deploy.py`

Current state:

- `install_units()` computes `dest = unit_dir / unit.filename`.
- It reads and writes `dest` directly.
- If `dest` is a symlink, `read_text()` and `write_text()` operate on the symlink target.
- Rollback also restores through the same `dest` path.

Why this matters:

An existing symlink in the systemd unit directory can make install or rollback mutate a file outside
the intended unit directory. This is especially important because install may run with elevated
permissions for system scope.

Recommended fix:

- Before writing, reject `dest.is_symlink()` or use safe replacement via a temporary regular file
  and atomic rename that replaces the symlink itself rather than its target.
- Defense-in-depth check that both lexical and resolved destinations stay under the unit directory.
- Add tests for symlinked `loop-demo.service` and rollback behavior.

### 13. Scheduled live probes inherit the operator environment beyond PATH and EnvironmentFile

Relevant files:

- `src/loopcraft/config.py`
- `src/loopcraft/runners/capabilities.py`
- `src/loopcraft/probes.py`
- `tests/test_capabilities.py`

Current state:

- `config.probe_env()` in scheduled mode returns `{**os.environ, "PATH": self.scheduled_path}` and
  then overlays `scheduler.environment_file` values.
- This fixes PATH and scheduled credentials, but other operator-only variables remain present.

Why this matters:

Some live probes can be influenced by environment variables beyond credentials and PATH, for
example proxy settings, SSL certificate paths, cloud config locations, or vendor-specific flags.
That can still create an `apply`/systemd mismatch: a Slack probe may pass because the operator
shell had `HTTPS_PROXY` or `SSL_CERT_FILE`, while the rendered unit will not have those unless they
are in the `EnvironmentFile`.

Recommended fix:

- For scheduled probes, build an environment that starts from a minimal baseline rather than the
  full operator environment.
- Include only the validated `PATH`, `LOOPCRAFT_SOURCE`, `LOOPCRAFT_MEMORY`, and
  `EnvironmentFile` variables, plus a small explicit allowlist if needed.
- Add a test proving an operator-only variable is not present in `probe_env()` for scheduled mode.

### 14. `discover-follows --digest-json` can read arbitrary filesystem paths

Relevant files:

- `src/loopcraft/research_intel/x/cli.py`
- `src/loopcraft/research_intel/x/store.py`
- `tests/test_x_intel.py`

Current state:

- `XIntelRunner.discover_follows()` uses `Path(digest_json)` directly when `--digest-json` is
  provided.
- It then reads that path with `read_text()` and parses it as JSON.
- When `--digest-json` is omitted, the code uses `self.store.latest_digest_json()`, which stays
  under the configured ledger-backed digest directory.

Why this matters:

The flag bypasses both source containment and memory-ledger containment. A direct CLI invocation
can read any JSON-looking file the process can access. The direct CLI is part of the shipped
loopcraft surface, and this command is a loop-adjacent workflow; its file inputs should use the
same explicit source/memory vocabulary as the rest of the project.

Recommended fix:

- Require `--digest-json` to be a `state/...` path, resolved through `LoopcraftConfig`, or make the
  flag explicitly an export/debug-only path with safe absolute/traversal checks.
- If arbitrary reads are intentionally allowed, document that as an interactive-only escape hatch
  and keep it out of skills/control-plane paths.
- Add tests for absolute and traversing `--digest-json` values.

### 15. `snapshot-following --output` can write arbitrary filesystem paths

Relevant files:

- `src/loopcraft/research_intel/x/cli.py`
- `tests/test_x_intel.py`

Current state:

- `snapshot_following()` turns the `--output` argument into `Path(output_path)`.
- It creates the parent directory and writes JSON there.
- The default output path is a gitignored source-relative file
  `config/x_following_snapshot.local.json`, but callers can pass any absolute or traversing path.

Why this matters:

This command writes private source configuration used by the X loop. The default is sensible, but
the flag allows writing outside source/memory/worktree with no containment or explicit export
semantics. That contradicts the public/private config split unless it is deliberately documented as
a user-chosen export path.

Recommended fix:

- Require the snapshot output to be a safe source-relative path under the source tree, preferably
  a `*.local.*` path.
- Reject absolute paths, `..`, and paths outside the source tree.
- Add tests for `/tmp/...`, `../...`, and a valid `config/*.local.json` path.

### 16. Direct arXiv/X `--config` paths are not source-bound

Relevant files:

- `src/loopcraft/research_intel/arxiv/cli.py`
- `src/loopcraft/research_intel/x/cli.py`
- `src/loopcraft/research_intel/arxiv/config.py`
- `src/loopcraft/research_intel/x/config.py`
- `tests/test_arxiv_intel.py`
- `tests/test_x_intel.py`

Current state:

- Manifest `content.config` is validated as a safe source-relative path.
- Control-plane preflight validates the effective content config.
- Direct CLIs accept `--config` and pass `Path(config_path)` directly to the typed config loaders.
- Absolute paths and `..` traversal are therefore allowed on the direct CLI surface.

Why this matters:

The direct CLIs are documented and used by skills for debugging. If they accept arbitrary config
paths, they can read outside the source tree and silently diverge from the manifest-declared
content config. That weakens the source/memory split and makes direct runs less representative of
scheduled runs.

Recommended fix:

- Resolve `--config` through `LoopcraftConfig.resolve_source_path()` unless an explicit
  debug/export flag is added.
- Keep the default path source-relative.
- Add tests that direct arXiv/X CLIs reject absolute and traversing config paths.

### 17. Direct config loaders follow `.local.*` symlinks without source containment

Relevant files:

- `src/loopcraft/settings.py`
- `src/loopcraft/research_intel/arxiv/config.py`
- `src/loopcraft/research_intel/x/config.py`
- `src/loopcraft/runners/capabilities.py`
- `src/loopcraft/worktree.py`

Current state:

- Preflight checks that local content-config siblings remain under the source tree.
- Worktree staging also checks local content-config overrides before copying them.
- Direct `ArxivIntelConfig.load()` and `IntelConfig.load()` call `local_override_path(path)` and
  read the returned file directly.
- A symlinked `config/x_intel.local.yaml` pointing outside the source tree is therefore read by the
  direct CLI path, even though the control-plane preflight/staging path would reject it.

Why this matters:

This creates a direct-CLI/control-plane mismatch and a source-boundary read escape. The public/local
override mechanism is a central pattern; every path that resolves the local override should apply
the same containment rule.

Recommended fix:

- Add a contained local-override resolver that takes `LoopcraftConfig` or a source root and checks
  both lexical and resolved containment.
- Use it in direct config loaders or pass resolved config paths from the direct CLIs after
  source-root validation.
- Add direct CLI tests for symlinked `.local.yaml` escaping source.

### 18. X following snapshot fetch resolves relative to CWD, not source/worktree

Relevant files:

- `src/loopcraft/research_intel/x/cli.py`
- `src/loopcraft/research_intel/x/config.py`
- `src/loopcraft/runners/capabilities.py`

Current state:

- `following_snapshot` in X YAML is validated as a safe source-relative path.
- Preflight resolves it through `config.resolve_source_path()`.
- `_fetch_from_snapshot()` later calls `load_following_snapshot_handles(Path(snapshot))`.
- That `Path(snapshot)` is relative to the current working directory, not necessarily the source
  tree or staged worktree.

Why this matters:

The same declared snapshot path can resolve differently in direct CLI runs depending on CWD. It can
also diverge from the path that preflight validated. In the staged headless run, CWD may be the
worktree; in direct debugging, README/skills often tell users to run from source. This ambiguity is
exactly what source-relative config is supposed to avoid.

Recommended fix:

- Resolve `following_snapshot` through `LoopcraftConfig.resolve_source_path()` in the direct CLI
  path, or ensure the effective staged config rewrites it to the staged worktree path.
- Add tests for direct X run from a non-source CWD with a source-relative snapshot.

### 19. `load_dotenv()` is CWD-relative rather than source-root-relative

Relevant files:

- `src/loopcraft/env.py`
- `src/loopcraft/cli.py`
- `src/loopcraft/research_intel/x/cli.py`

Current state:

- `load_dotenv()` defaults to `Path(".env")`.
- `loopctl main()` calls `load_dotenv()` before `LoopcraftConfig.load(args.source)`.
- `XIntelRunner.__init__()` also calls `load_dotenv()` before resolving config.

Why this matters:

When a user invokes `loopctl` or a direct CLI from outside the source root while setting
`LOOPCRAFT_SOURCE`, the repo `.env` is not loaded. Conversely, a random `.env` in the current
working directory can influence a source tree elsewhere. That is a path-resolution mismatch for
credentials and config overrides.

Recommended fix:

- Resolve `LoopcraftConfig` first, then load `config.source_path / ".env"`.
- Keep support for an explicit dotenv path if needed.
- Add tests for invoking from outside the source tree with `LOOPCRAFT_SOURCE` set.

### 20. `loopctl logs` trusts `log_path` from editable run records

Relevant files:

- `src/loopcraft/cli.py`
- `src/loopcraft/store.py`
- `tests/test_cli.py`

Current state:

- `loopctl logs` reads `latest.log_path` from the run record.
- It checks only that the path exists.
- It then reads that path directly.
- Run records live in the git-versioned memory ledger and are editable by design.

Why this matters:

A corrupted or hand-edited run record can make `loopctl logs <loop>` read arbitrary local files.
The ledger is trusted-ish local state, but `logs` is still a CLI file read surface and should
constrain log paths to the memory tree's worktree/log area or to paths that were produced by
Loopcraft.

Recommended fix:

- When reading logs, require `log_path` to resolve under `memory_path/var/worktrees` or another
  configured log root.
- If legacy run records contain external paths, report a clear "log path outside allowed root"
  problem rather than reading it.
- Add a test with a hand-written run record pointing to an external file.

### 21. `write_follow_candidates()` does not use `contained_child()` for date-stamped files

Relevant files:

- `src/loopcraft/research_intel/x/store.py`

Current state:

- X/arXiv digest writers use `contained_child()` when composing date/run-stamped output filenames.
- `write_follow_candidates()` writes `out_dir / f"{date_stamp}.md"` and `.json` directly.
- Today `date_stamp` comes from `datetime.strftime("%Y-%m-%d")`, so traversal is unlikely, but the
  helper is inconsistent with the stronger pattern used elsewhere.

Why this matters:

This is lower severity than the arbitrary `--output-dir` issue, but the codebase has already
standardized on `contained_child()` for runtime-derived filename components. Using it consistently
reduces future regression risk if `date_stamp` ever becomes configurable or environment-derived.

Recommended fix:

- Use `contained_child()` in `write_follow_candidates()`.
- Add a small store-level regression test for a malicious date stamp if the method remains public.

## Notes On Non-Issues Checked

- `state/...` manifest input/output paths are guarded by `safe_state_relpath()` and
  `assert_under()`.
- `logic.skill`, `logic.verify`, and `content.config` are source-relative and validated before
  staging.
- Worktree staging defends against source symlink escapes inside skill directories.
- Research digest/history filenames now use `contained_child()` around run/date stamp filenames.
- Review 04's live-probe scheduled PATH issue is fixed for Slack: `probe_slack_api()` executes the
  resolved `nv-tools` path and passes `config.probe_env()`.
- Worktree staging validates skill/content paths and blocks source symlink escapes within skill
  directories.
