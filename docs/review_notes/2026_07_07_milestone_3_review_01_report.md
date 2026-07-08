# Milestone 3 Review 01 — Report (Claude + Cursor adapters)

Review target: local branch `feat/m3-adapters` in
`/Users/dpickem/workspace/loopcraft`.

Reviewed state:

- Branch HEAD: `bd07331 Merge pull request #2 from dpickem/feat/m2-control-plane`
- Uncommitted M3 implementation changes in the working tree, including:
  - `src/loopcraft/runners/base.py`
  - `src/loopcraft/runners/codex.py`
  - `src/loopcraft/runners/claude.py`
  - `src/loopcraft/runners/cursor.py`
  - `src/loopcraft/runners/__init__.py`
  - `src/loopcraft/cli.py`
  - `src/loopcraft/config.py`
  - `README.md`
  - `tests/test_runner.py`
  - `tests/test_cli_vendor.py`

Reference scope:

- M3 in `docs/loopcraft-implementation-design.html`
- `CONTRIBUTING.md`

This pass focuses on M3 portability: Claude and Cursor runner implementations, `loopctl vendor`
commands, per-loop/runtime-vendor overrides, shared prompt behavior, preflight capability checks,
docstrings, file organization, and tests.

## Verification

Commands run from `/Users/dpickem/workspace/loopcraft`:

```text
uv run pytest tests/test_runner.py tests/test_cli_vendor.py -q
uv run python -m compileall -q src/loopcraft/runners src/loopcraft/cli.py src/loopcraft/config.py
```

Result:

- Focused tests passed: 33 tests.
- Byte-compile passed for the changed runner/CLI/config modules.

## Findings

### 1. `vendor get/list` can report an invalid default vendor as OK

Relevant files:

- `src/loopcraft/config.py`
- `src/loopcraft/cli.py`
- `tests/test_cli_vendor.py`

Current state:

- `LoopcraftConfig.default_vendor` is a plain string.
- `LoopcraftConfig.load()` accepts `default_vendor` from `loopcraft.toml` or `LOOPCRAFT_VENDOR`
  without validating it against registered adapters.
- `loopctl vendor get` and `loopctl vendor list` return `ok: true` using that value.
- `loopctl vendor set` rejects unknown vendors, but direct config/env values can still be invalid.

Why this matters:

M3's portability contract says "the runtime is a config value, not a rewrite" and introduces
`loopctl vendor set` as the global switch. A config/env value like `gemini` is not actually a
usable runtime in this branch, but `vendor get/list` can still report it successfully; the failure
is delayed until `run`, `apply`, or another command calls `get_runner()`. `CONTRIBUTING.md` also
requires stable vocabularies to be centralized and tested.

Recommended fix:

- Validate `default_vendor` during config loading or in `vendor get/list`, using the registered
  adapter set.
- If config loading should remain runner-agnostic, make `vendor get/list` return nonzero when the
  current default has no adapter, and include the valid vendor list.
- Add tests for invalid `default_vendor` in `loopcraft.toml` and invalid `LOOPCRAFT_VENDOR`.

### 2. M3 preflight does not actually verify model availability

Relevant files:

- `src/loopcraft/runners/codex.py`
- `src/loopcraft/runners/claude.py`
- `src/loopcraft/runners/cursor.py`
- `tests/test_runner.py`
- `README.md`

Current state:

- The M3 design calls for preflight capability checks including whether the model is available.
- Codex preflight checks only whether a model slug has a known-looking prefix.
- Claude preflight accepts aliases / `claude-*` names by shape.
- Cursor intentionally accepts any model because available models are account/plan-dependent.
- None of the adapters query the actual CLI/model catalog, nor do they document that availability
  checking is deferred.

Why this matters:

`apply` / `deps check --loop` can pass a loop whose configured model is not available to the
runtime account. That pushes a deployment-time problem into the actual scheduled run, weakening the
M3 "preflight catches capability gaps" promise. The heuristic checks are useful as typo guards, but
they are not model availability checks.

Recommended fix:

- Add real, bounded model-availability probes where the CLI supports them, or add an explicit
  `Model availability check deferred` note in README/design response and make tests/strings avoid
  claiming full availability validation.
- For Cursor, consider a warning-style preflight item if model availability cannot be probed
  locally, so operators know the CLI may still reject the model at runtime.
- Add tests that distinguish "shape looks valid" from "catalog availability verified" once the
  probing policy is chosen.

## Notes On Healthy Areas

- Claude and Cursor adapters are registered and exposed through `get_runner()` / `available_vendors()`.
- The prompt is now shared in `BaseRunner`, and tests confirm it is identical across Codex, Claude,
  and Cursor for the same manifest.
- `loopctl vendor set/get/list` has focused CLI tests for the happy path, unknown vendor rejection,
  missing name, and env override reporting.
- New adapter modules have module docstrings, typed public methods, module-scope imports, and
  module-level constants with comments.
