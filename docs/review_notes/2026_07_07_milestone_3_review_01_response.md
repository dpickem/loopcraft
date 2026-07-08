# Milestone 3 Review 01 — Response

Response to [`2026_07_07_milestone_3_review_01_report.md`](2026_07_07_milestone_3_review_01_report.md),
the first review of the M3 Claude + Cursor adapters and `loopctl vendor` on
`feat/m3-adapters`.

Both findings are addressed.

## Commits

The reviewed M3 implementation was committed as a baseline, then the findings
were fixed on top:

```text
0eb1c74 feat: add M3 Claude + Cursor adapters + loopctl vendor (portability)
1533c5b fix: address M3 review 01 findings
```

The fixes are in `1533c5b`. Inspect with `git show 1533c5b -- <file>`.

## Status

```text
make test && make compile && make validate
```

- `make test`: **359 passed** (up from 356 at review time), fully offline.
- `make compile`: passes.
- `make validate`: passes, 3 manifests.

## Findings

### 1. `vendor get`/`list` no longer report an invalid default as OK — fixed

`default_vendor` (from `loopcraft.toml` or `LOOPCRAFT_VENDOR`) is still loaded
runner-agnostically — keeping `loopcraft.config` free of a `loopcraft.runners`
import (which would be a cycle). Instead, `loopctl vendor get`/`list` now
validate the **effective** default against the registered adapter set
(`available_vendors()`):

- when the default has no adapter, both commands exit nonzero (`ExitCode.FAILURE`),
  emit `data.default_ok = false`, and print `error: default vendor '<x>' has no
  runtime adapter (available: [...])`;
- this covers both an invalid `loopcraft.toml` value and an invalid
  `LOOPCRAFT_VENDOR` override (which wins at load time and is reported as the
  effective default).

`vendor set` already rejected unknown vendors, and `run`/`apply` already fail on
an unregistered vendor via `get_runner()` (surfaced per-loop in the apply plan),
so the only reporting gap was `get`/`list` — now closed.

- Files: `src/loopcraft/cli.py`.
- Tests: `tests/test_cli_vendor.py::test_vendor_get_flags_invalid_config_default`,
  `::test_vendor_list_flags_invalid_config_default`,
  `::test_vendor_get_flags_invalid_env_override` (plus the existing happy-path,
  unknown-`set`, missing-name, and env-override-note tests).

### 2. Model checks documented as a shape/typo guard, not availability — fixed

The adapters' model checks are intentionally **local shape/typo guards**, not
live catalog lookups: Codex flags a non-`gpt`/`o*`/`codex` slug, Claude flags a
non-`sonnet`/`opus`/`claude-*` slug, and Cursor (cross-provider,
account/plan-dependent) accepts any model. Real availability probing would
require per-CLI, account- and plan-specific calls that are not offline-testable
and belong to a later milestone, so rather than overclaim, the deferral is now
explicit:

- The README's *Runtime portability* section states plainly that preflight does
  not query the vendor's live model catalog; a plausible-but-unavailable model
  (and any Cursor model) is validated by the vendor CLI at run time, and live
  catalog probing is deferred. The existing adapter comments already frame the
  probes as "flag an obviously wrong vendor model … not track an exact catalog."
- No code strings claim availability validation; the probe messages say a model
  "is not a recognized … model" (a shape statement).

Chosen over adding live probes now because (a) they cannot be exercised in the
offline test suite, (b) catalogs are account/plan-specific, and (c) `run` already
surfaces a truly unavailable model as a failed run record — the deferral note
keeps `apply`'s promise honest without a heuristic pretending to be an
availability check.

- Files: `README.md` (deferral note).

## Notes on healthy areas

Review 01 confirmed the adapters are registered and exposed via `get_runner()` /
`available_vendors()`, the prompt is shared in `BaseRunner` and identical across
vendors for one manifest, `vendor set/get/list` has focused tests, and the new
modules follow the file-organization/docstring conventions — all unchanged by
these fixes. The cross-provider sub-agent and per-role multi-model work remains
M3.5.
