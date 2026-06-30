# Contributing to loopcraft

This guide covers the development workflow, code standards, and review
requirements for loopcraft. It is written for both human developers and AI coding
assistants, so it can be referenced during automated code review.

## Quick Checklist

Before editing or submitting code, verify every item below:

- Read this guide before making changes; do not rely on memory or lint alone.
- Keep source and memory separate: code, manifests, and skills live in this repo;
  loop output lives in the configured memory tree.
- Add or update focused tests for every behavior change.
- Keep imports at module scope unless a command handler intentionally defers a
  heavy or circular import.
- Keep all signatures fully typed; use `X | None`, parameterized collections,
  and dataclasses for structured shapes.
- Prefer `dataclass` models for manifest/config/run-result structures.
- Do not add broad `type: ignore` comments; fix the type model instead, or
  document a narrow unavoidable exception.
- Do not commit generated files (`__pycache__/`, `.pytest_cache/`, `var/`,
  memory-tree state, local config, or credentials).
- Do not add checked-in shell scripts for routine workflows. Prefer `make`
  targets that call `loopctl` or `python -m ...` directly.
- Run the affected tests before calling work done. For broad changes, run
  `make test`.

## Development Setup

```bash
cd ~/workspace/loopcraft
python -m pip install -e .
make test
```

The project uses:

- **Python 3.11+** (see `requires-python` in `pyproject.toml`)
- **setuptools** for packaging
- **argparse** for the current `loopctl` CLI
- **PyYAML** for loop manifests
- **pytest** for tests

Runtime integrations such as `codex`, `claude`, `cursor-agent`, and `nv-tools`
are probed by `loopctl deps check`. They are not required for offline unit tests.

## Project Structure

```text
loops/                      # Loop manifests, one YAML file per loop
skills/                     # Vendor-neutral SKILL.md files and bundled skills
config/                     # Example config for prototype intelligence workflows
docs/                       # Workflow and automation notes
src/loopcraft/
  cli.py                    # loopctl entry point
  config.py                 # loopcraft.toml loading + source/memory paths
  manifest.py               # LoopManifest schema, validation, DAG checks
  store.py                  # Ledger writes + durable run records
  runners/                  # Runtime adapter interface + vendor adapters
  arxiv_intel/              # L2 prototype source, migrated to unified store in M2
  x_intel/                  # L2 prototype source, migrated to unified store in M2
tests/
  test_<module>.py          # Offline unit tests
```

`loopcraft.toml` wires this source tree to the memory tree, usually
`~/workspace/loopcraft_memory`. The memory tree holds `ledger/`, `artifacts/`,
and eventually the derived `loopcraft.db`; it should not be committed to this
repo.

## Running Checks

```bash
make test        # pytest tests/ -q
make compile     # byte-compile src/ and tests/
make validate    # validate manifests in loops/
make check       # deps check + manifest dry-run apply
```

For focused work, run the smallest useful test first:

```bash
PYTHONPATH=src python -m pytest tests/test_manifest.py -q
PYTHONPATH=src python -m pytest tests/test_cli.py -q
```

Run `make test` before handing off broad changes.

## Pull Request Requirements

Every change should satisfy the following before merge:

1. **Offline tests pass.** The default test suite must not require live Slack,
   X, arXiv, Codex, Claude, Cursor, or nv-tools credentials.
2. **Manifest changes validate.** Run `make validate` when touching `loops/` or
   manifest parsing.
3. **Runtime-adapter changes have tests.** Mock subprocesses and filesystem
   effects; do not call real vendor CLIs in unit tests.
4. **State changes use `loopcraft.store`.** Do not introduce per-loop side
   databases or ad hoc state files outside the configured memory tree.
5. **User-facing behavior is documented.** Update `README.md`, docs, skills, or
   example config when commands, manifests, or workflows change.
6. **Generated files are absent.** Remove `__pycache__/`, `.pytest_cache/`, and
   local outputs before committing.
7. **Design-backed changes state their shipped scope.** If the implementation
   design includes future milestones, call out what ships now and what remains
   deferred.

## Code Guidelines

### 1. Keep Loop Boundaries Explicit

A loop should be described by a manifest plus a skill:

- Manifest: runtime, cadence, tier, dependencies, inputs, outputs, artifacts,
  and verification contract.
- Skill: vendor-neutral behavior and operational rules.
- Store: all durable state writes.

Do not hide dependencies in code. If a loop needs an API, tool, auth bundle, env
var, upstream loop, input, or output, declare it in the manifest.

### 2. Source and Memory Stay Separate

Source files belong in this repo. Loop output belongs in the memory tree:

- `state/...` manifest paths resolve to `<memory>/ledger/...`.
- Run records are written under `<memory>/ledger/runs/`.
- Produced reports and other artifacts belong under `<memory>/artifacts/` once
  artifact capture lands.

Never write loop state into `src/`, `loops/`, `skills/`, or repo-local `var/`
from new control-plane code.

### 3. Tests

All behavior changes need tests:

- Unit tests for pure logic, manifest parsing, config resolution, and store
  behavior.
- CLI tests for command behavior and exit codes.
- Runner tests that mock subprocess calls and assert normalized `RunResult`
  behavior.
- Regression tests for bug fixes.

Tests must be offline by default. If an integration test is added later, it must
be opt-in and auto-skip when credentials or runtime CLIs are missing.

### 4. Imports

Keep imports at module scope:

```python
from loopcraft.manifest import LoopManifest
```

Local imports are acceptable only when they avoid a real circular dependency or
defer a heavy optional dependency from CLI startup.

### 5. Docstrings

Public modules, classes, functions, and methods should have useful docstrings.
Use the level of detail the API deserves:

- First line: concise summary.
- `Args`, `Returns`, and `Raises` where a public API benefits from them.
- Shorter docstrings are fine for private helpers when the function is obvious.

### 6. Type Annotations

All function signatures should be fully typed.

```python
def resolve_state_path(self, declared: str) -> Path:
    ...
```

Use `X | None` instead of `Optional[X]`, parameterize collections
(`list[str]`, `dict[str, object]`), and prefer dataclasses for structured data
such as manifests, configs, preflight reports, run contexts, and run results.

### 7. Closed Vocabularies

For stable vocabularies such as vendors, loci, tiers, cadence types, and run
statuses, keep the allowed values centralized and tested. As these vocabularies
grow, prefer `Enum` / `StrEnum` over repeated string literals.

### 8. No Routine Shell Scripts

Routine workflows should be exposed through `make` targets or Python modules:

```bash
make daily-arxiv-intel
PYTHONPATH=src python -m loopcraft.arxiv_intel.cli run --config config/arxiv_intel.json
```

Do not add a checked-in shell script when a `make` target or `python -m` entry
point will do. If a shell wrapper is truly necessary, document why in the PR and
keep it small, quoted, and tested where practical.

## Adding a New Loop

To add a loop:

1. Add `loops/<loop-id>.yaml`.
2. Add `skills/<loop-id>/SKILL.md`.
3. Declare all dependencies in the manifest: APIs, tools, auth, env vars,
   upstream loops, inputs, outputs, artifacts, budget, and approval policy.
4. Add tests for any parser, store, or runner behavior needed by the loop.
5. Run:

   ```bash
   make validate
   make test
   ```

Observe-tier loops should be read-only. Propose-tier loops may draft actions,
but irreversible actions must be approval-gated.

## Adding a Runtime Adapter

Runtime adapters implement the `Runner` protocol in `src/loopcraft/runners/`:

- `preflight(loop, config)`: report missing binaries, tools, auth, env vars,
  model support, or skill files before a run starts.
- `run(loop, ctx)`: execute in an isolated worktree and return a normalized
  `RunResult`.

Adapter tests must mock subprocesses and assert:

- Preflight catches missing capabilities.
- Logs are captured.
- Declared outputs are verified.
- Failures map to normalized statuses (`done`, `failed`, `stalled`,
  `needs_approval`).

Register the adapter in `src/loopcraft/runners/__init__.py` and update
documentation when the vendor becomes user-facing.

## Writing Agent Skills

The `skills/` directory contains vendor-neutral Agent Skills. Keep skills small,
explicit, and portable across Codex, Claude, and Cursor.

Skill guidelines:

- Front-load trigger words in `description`.
- Keep `SKILL.md` focused on behavior, inputs, outputs, and hard rules.
- Put large references in separate files only when they are genuinely needed.
- Keep observe-tier skills read-only.
- State approval requirements clearly for propose-tier behavior.
- Update `Makefile` installation targets if bundled skill layout changes.

## Public/Private Config Split (the prevailing pattern)

Loopcraft config is split into committed *public* files and private overrides, so
confidential values (channel names, list memberships, account ids, internal URLs)
never live in the source repo. This is the default pattern for any loop config —
follow it instead of inventing per-loop schemes.

- **Public file** (committed): holds only non-confidential placeholders and the
  documentation of how to override it. Example: `skills/slack-triage/channels.txt`.
- **Private overrides**, resolved at run time with this precedence:
  1. an **environment variable** (put it in `.env`, which is gitignored),
  2. a gitignored **`*.local.*` sibling** file (e.g. `channels.local.txt`),
  3. the public committed file.
- The control plane resolves the effective value and materializes it into the
  isolated run worktree during `stage_loop_assets()`, so the agent reads the
  resolved file and nothing confidential leaves the gitignored sources.

Conventions and helpers:

- The override env var for a skill list asset is derived from its path by
  `loopcraft.settings.asset_env_var`:
  `skills/<skill>/<file>.txt` → `LOOPCRAFT_<SKILL>_<FILE>` (uppercased, every run
  of non-alphanumeric characters becomes `_`). Example:
  `skills/slack-triage/channels.txt` → `LOOPCRAFT_SLACK_TRIAGE_CHANNELS`.
- Use `loopcraft.settings.resolve_overridable_list(...)` for list-valued config so
  precedence is consistent.
- `.gitignore` ignores `*.local.*`; never commit a `*.local.*` file.
- A public file must contain **no** confidential entries — only comments and
  safe placeholders. Add a test that the public file has no active (uncommented)
  confidential lines when that matters (see `tests/test_settings.py`).

## Credentials and Local State

- Never commit `.env`, credentials, OAuth tokens, generated digests, memory-tree
  ledgers, `*.local.*` overrides, or local outputs.
- `.env.example` may document names, but never values.
- New credential requirements belong in the relevant manifest `depends_on.auth`
  and in user docs.
- Prefer environment/config loading through `LoopcraftConfig`, `loopcraft.settings`,
  or the relevant workflow config object. Avoid scattered direct credential reads.

## Dependency Security

When adding dependencies:

1. Prefer the smallest dependency that fits the problem.
2. Add it to `pyproject.toml` with a reasonable floor only when needed.
3. Prefer structured parsers and standard libraries over ad hoc parsing.
4. Re-run tests after dependency changes.
5. Document user-visible dependency requirements in `README.md` or the relevant
   docs.

For untrusted inputs:

- Cap response sizes where practical.
- Set timeouts on network calls.
- Build subprocess commands from structured argument lists, not shell-concatenated
  strings.
- Validate file paths before writing outside a worktree or the memory tree.

## Safety Model

Loopcraft defaults to zero blast radius:

- **observe** loops are read-only and report findings.
- **propose** loops may draft actions but must park them for approval.
- **act** loops execute only within hard budgets and only for safe, reversible,
  explicitly approved scopes.

Irreversible actions such as sending messages, merging code, spending money, or
mutating remote state must remain approval-gated.
