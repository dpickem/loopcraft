# Milestone 3 Review 02 — Report (adapter contract follow-up)

Review target: local branch `feat/m3-adapters` in
`/Users/dpickem/workspace/loopcraft`.

Reviewed state:

- `0eb1c74 feat: add M3 Claude + Cursor adapters + loopctl vendor (portability)`
- `1533c5b fix: address M3 review 01 findings`
- `c0b32ff docs: add M3 review 01 report and response`

Reference scope:

- M3 in `docs/loopcraft-implementation-design.html`
- `CONTRIBUTING.md`

This pass re-reviewed the branch after the Review 01 response, focusing on whether the new Claude
and Cursor adapters actually satisfy the design's portability contract: "an existing loop runs
unchanged" and Cursor's cross-provider sub-agent capability.

## Verification

Commands run from `/Users/dpickem/workspace/loopcraft`:

```text
make test && make compile && make validate && make check
uv run pytest tests/test_runner.py tests/test_cli_vendor.py -q
```

Result:

- Full checks passed: 359 tests, compile, validate, and check.
- Focused M3 tests passed: 33 tests.

## Findings

### 1. Cursor adapter does not grant access to declared output roots

Relevant files:

- `src/loopcraft/runners/cursor.py`
- `src/loopcraft/runners/base.py`
- `src/loopcraft/runners/codex.py`
- `src/loopcraft/runners/claude.py`
- `tests/test_runner.py`

Current state:

- `BaseRunner.run()` executes every adapter from the per-run worktree.
- Declared outputs resolve into the memory ledger, outside the per-run worktree.
- Codex grants each declared output parent directory with `--add-dir`.
- Claude also grants each declared output parent directory with `--add-dir`.
- Cursor does not grant output directories at all; its command is only
  `cursor-agent -p --output-format text [--model ...]`.
- The Cursor adapter comment says output roots are conveyed through the prompt instead of a
  per-dir flag.

Why this matters:

M3 says an existing loop runs unchanged when switching vendors. For loops whose outputs are in the
memory ledger, the agent must be able to write outside the staged worktree. A prompt line saying
"write this absolute path" is not equivalent to a filesystem permission grant. If `cursor-agent`
does not already have trust/write access to the memory ledger paths, a Cursor run can fail to
produce outputs that Codex/Claude can produce.

Recommended fix:

- If the Cursor CLI supports writable roots or workspace inclusion flags, use them for
  `self.writable_roots(ctx)` just like Codex/Claude.
- If Cursor cannot grant output roots in M3, document Cursor as a limited adapter for loops whose
  outputs are writable from its workspace, and make preflight fail or warn for ledger-output loops
  until a correct access model exists.
- Add a test asserting Cursor either includes the writable roots in its command or explicitly
  reports that declared ledger outputs are unsupported for Cursor.

### 2. M3 Cursor cross-provider sub-agent exit criterion is not implemented or documented as deferred

Relevant files:

- `docs/loopcraft-implementation-design.html`
- `README.md`
- `src/loopcraft/runners/cursor.py`
- `tests/test_runner.py`

Current state:

- The M3 design exit criterion says "a Cursor loop can spawn a cross-provider sub-agent."
- The current Cursor adapter runs one `cursor-agent -p` command with an optional model.
- There is no manifest field, prompt compiler, role/sub-agent definition, or test covering a
  Cursor sub-agent spawn.
- README says Cursor is cross-provider and leaves model validation to the CLI, but it does not
  state that cross-provider sub-agent support is deferred to M3.5 or a later milestone.
- Review 01 response notes this verbally, but the shipped README/design-facing docs still make M3
  sound broader than the implementation.

Why this matters:

This is a scope/contract mismatch. If cross-provider sub-agent spawning is intentionally deferred,
the branch can still be fine, but the shipped scope must say so. Otherwise, the branch misses part
of the M3 exit criteria.

Recommended fix:

- Either implement the Cursor sub-agent path required by the M3 exit criterion, with a manifest
  shape and tests; or
- Update README / implementation notes to state that M3 ships single-run Cursor adapter support
  only, while cross-provider sub-agents and role compilation land in M3.5.
- Add a test/documentation check for the chosen shipped scope so the branch does not overclaim.

## Notes On Healthy Areas

- The invalid-default-vendor behavior from Review 01 is fixed: `vendor get/list` return nonzero
  for unregistered defaults.
- Model checks are now documented as shape/typo guards rather than live availability checks.
- Claude and Cursor modules follow the repository's file-organization/docstring conventions.
- The shared prompt path in `BaseRunner` is a good step toward portability.
