# Milestone 1 Review 08 — Report (branch implementation follow-up)

Review target: local branch `feat/m1-control-plane` in
`/Users/dpickem/workspace/loopcraft`.

Reviewed state:

- HEAD: `6176a42 docs: add M1 review 07 report and response`
- Review 07 implementation commit: `30d5223 fix: address M1 review 07 findings`

Reference scope:

- M1 build plan and control-plane contracts in
  `docs/loopcraft-implementation-design.html`
- `CONTRIBUTING.md`
- Review 07 response in
  `docs/review_notes/2026_07_02_milestone_1_review_07_response.md`
- Prior Review 01–07 reports and responses, to avoid repeating acknowledged
  deferrals or already-fixed findings

This pass verified the five Review 07 fixes and reviewed the branch again for
remaining path, dependency, and public/private-contract gaps. The findings
below are additional issues not covered by the earlier reports.

## Verification

The review shell initially omitted the local Python/nv-tools directories from
`PATH`. With the known runtime locations restored, the full project checks ran
from `/Users/dpickem/workspace/loopcraft`:

```text
PATH=/Users/dpickem/miniconda3/bin:/Users/dpickem/.local/bin:$PATH \
  make test compile validate check PYTHON=/Users/dpickem/miniconda3/bin/python
```

Result:

- `make test`: passed, 182 tests.
- `make compile`: passed.
- `make validate`: passed, 3 manifests.
- `make check`: passed; all required and optional binaries were found in this
  adjusted environment.

Focused diagnostics reproduced the following behaviors outside the current
suite:

- An in-source directory symlink from a skill to the source root passed the
  contained walker and copied the repo's `.env` into the staged skill bundle.
- A symlinked `x_intel.local.yaml` resolving outside source was read and
  accepted by content-config preflight, even though staging later rejects it.
- Passing traversal strings as the internal `run_stamp` and `date_stamp` wrote
  both digest files outside the configured memory tree.
- With only `X_API_OAUTH2_ACCESS_TOKEN` set, X runtime/auth code accepted the
  credential but manifest preflight still reported
  `required env var not set: X_API_BEARER_TOKEN`.
- A configured but nonexistent X following snapshot was silently treated as an
  empty source; the run returned 0 and wrote a successful empty digest.

The live Slack exit-criteria run was not executed because it would access real
external data. The offline suite continues to use a stub adapter for the
end-to-end control-plane path.

## Findings

### 1. The contained skill walker can stage unrelated in-repo secrets and handles directory aliases inconsistently

Relevant files:

- `src/loopcraft/worktree.py`
- `src/loopcraft/runners/capabilities.py`
- `src/loopcraft/settings.py`
- `tests/test_worktree.py`
- `tests/test_capabilities.py`

Current state:

- `_copytree_contained()` now requires each enumerated target to stay under
  `config.source_path`, which closes the outside-tree leak from Review 07.
- The allowed boundary is the entire source repo, not the declared skill
  directory. A skill asset can therefore be a directory symlink to the source
  root (or another unrelated in-repo directory), and the walker recursively
  stages everything reachable there—including gitignored local files and
  credentials such as `.env`. The focused diagnostic copied
  `SECRET_TOKEN=leaked` into
  `skills/<skill>/all-source/.env` in the worktree.
- The walker uses one global `seen_dirs` set keyed by resolved directory. When
  an allowed directory symlink aliases a real directory that has already been
  visited, the alias traversal is skipped before its destination is created.
  This conflicts with the documented policy that an in-source-resolving
  symlink is dereference-copied; file symlinks are copied, but directory aliases
  can disappear depending on traversal order.
- Content-config preflight has a parallel gap: typed validators call
  `local_override_path()` and read the effective local sibling without first
  applying source containment to that sibling. A local-config symlink outside
  source can therefore be read during preflight and reported valid, while the
  subsequent staging phase rejects the same file.

Why this matters:

Source containment is broader than asset authorization. The design stages a
minimal loop bundle, and `CONTRIBUTING.md` requires private credentials/local
state not to leak. Allowing a skill to pull any in-repo path into its bundle can
expose exactly those gitignored private files. The preflight/staging disagreement
also means `--dry-run` can report ready for a config the real run refuses.

Recommended fix:

- Restrict staged skill entries to the resolved declared skill-directory root,
  not merely the repository root. The simplest safe policy is to reject
  directory symlinks; if they remain supported, require their targets to stay
  under the skill root.
- Use ancestry-local cycle detection rather than a global resolved-directory
  set if directory aliases are expected to materialize independently.
- Apply `_assert_source_contained()` to the effective local override before any
  preflight validator reads it, sharing the exact helper/path choice with
  staging.
- Add tests for a skill symlink to source root/`.env`, an allowed directory
  alias's staged contents, and an outside local-config symlink during preflight.

### 2. Internal run-id/date environment values can traverse digest and history directories

Relevant files:

- `src/loopcraft/config.py`
- `src/loopcraft/research_intel/arxiv/cli.py`
- `src/loopcraft/research_intel/arxiv/store.py`
- `src/loopcraft/research_intel/x/cli.py`
- `src/loopcraft/research_intel/x/store.py`
- `tests/test_arxiv_intel.py`
- `tests/test_x_intel.py`

Current state:

- Control-plane values for `LOOPCRAFT_RUN_ID` and `LOOPCRAFT_RUN_DATE` are safe,
  but both direct workflows trust any inherited values for these environment
  variables.
- `write_digest()` appends the strings directly below already-resolved
  directories (`history_dir / f"{run_stamp}.md"` and
  `digest_dir / f"{date_stamp}.md"`) without reapplying state-path validation or
  checking that each stamp is one safe filename component.
- Focused traversal stamps (`../../../../../escaped-run` and
  `../../../../../escaped-date`) created both Markdown files outside the
  configured memory tree. The same construction is duplicated in the arXiv
  and X stores.

Why this matters:

These variables are an internal control-plane protocol, but direct CLIs are
also public entry points and inherit the caller's environment (the X CLI also
loads `.env`). A stale, malformed, or deliberately supplied value can bypass
the fixed manifest output contract and the ledger containment guarantees fixed
in prior reviews.

Recommended fix:

- Validate `RUN_ID_ENV` and `RUN_DATE_ENV` before use. Require the canonical
  control-plane run-id format and a real ISO `YYYY-MM-DD` date, or at minimum a
  single safe filename component with no separators/traversal.
- Add defense in depth in both stores: after composing every history/digest
  path, assert it remains under the resolved directory/ledger root before
  writing.
- Convert invalid protocol values into the standard structured CLI failure,
  rather than falling back silently or raising a traceback.
- Add tests for traversal, absolute values, malformed dates, and valid
  control-plane values for both workflows.

### 3. The X manifest rejects an OAuth credential that the runtime explicitly supports

Relevant files:

- `loops/x-intel.yaml`
- `src/loopcraft/runners/capabilities.py`
- `src/loopcraft/research_intel/x/config.py`
- `src/loopcraft/research_intel/x/cli.py`
- `tests/test_capabilities.py`

Current state:

- The manifest declares `depends_on.env: [X_API_BEARER_TOKEN]`, so the generic
  env check requires that exact variable.
- The `x-api` auth probe accepts either `X_API_BEARER_TOKEN` or
  `X_API_OAUTH2_ACCESS_TOKEN`.
- `XApiTokens.token(require_user_context=False)` and the scheduled `run`
  workflow also accept OAuth access token first, with bearer token as fallback.
- Consequently, an OAuth-only environment that the implementation can use
  successfully still fails preflight with a missing-bearer error. The focused
  check reproduced exactly that problem.

Why this matters:

The manifest is supposed to be the authoritative, explicit dependency
contract. Here it is stricter than—and contradictory to—the runtime and auth
probe, so a valid host is refused before execution. It also reports the same
credential requirement twice with different semantics (`env` exact-one vs.
`auth` either-one).

Recommended fix:

- Choose one contract and make all layers agree. If scheduled X runs support
  either credential, remove the exact bearer env declaration and let the
  `x-api` auth probe own the alternative requirement.
- If the manifest vocabulary needs to express alternatives generally, add an
  explicit `any_of` env/auth shape rather than listing one member as mandatory.
- If bearer-only is intended, change `XApiTokens.token()` and documentation to
  reject OAuth for scheduled runs instead.
- Add preflight tests for bearer-only, OAuth-only, neither, and both.

### 4. The configured X following snapshot is an undeclared dependency that can fail open

Relevant files:

- `config/x_intel.yaml`
- `loops/x-intel.yaml`
- `src/loopcraft/research_intel/x/config.py`
- `src/loopcraft/research_intel/x/cli.py`
- `src/loopcraft/research_intel/x/follow_discovery.py`
- `tests/test_x_intel.py`

Current state:

- `sources.following_snapshot` is an optional `Path` inside content config, and
  the README encourages a private
  `config/x_following_snapshot.local.json` value.
- The referenced snapshot is not a manifest input or separately declared
  content asset, is not staged with `content.config`, and is not checked by
  capability preflight.
- `_snapshot_handles()` treats a configured missing file as an empty list. A
  scheduled run therefore silently drops that configured source, returns
  success, and emits an empty/partial digest; the focused missing-snapshot run
  returned 0 and wrote `latest.json`.
- If the snapshot exists but contains malformed JSON or a non-object shape,
  `_snapshot_handles()` raises outside the `XApiError` isolation in
  `_collect_raw_posts()`, so the direct CLI can produce a traceback rather than
  its JSON envelope.
- Absolute or traversing snapshot paths are accepted by the Pydantic model,
  allowing the content config to introduce an undeclared read outside the
  source/memory boundaries.

Why this matters:

The design's central rule is that every input/dependency is explicit and
validated before the loop runs. A configured source that disappears silently
is especially dangerous for an intelligence loop: the digest looks successful
but its coverage has degraded. The hidden path also makes the staged loop
bundle incomplete and non-portable.

Recommended fix:

- Model referenced content assets explicitly (for example
  `content.assets`) or otherwise resolve `following_snapshot` through a shared
  contained source-asset mechanism.
- When configured, require the snapshot to exist, be a regular contained file,
  and validate its JSON/schema during preflight; then stage it with the config.
- Treat a missing/malformed configured snapshot as a named source error (or a
  preflight failure), never as an empty successful source.
- Add tests for missing, malformed, wrong-shape, absolute/traversing, valid
  public, and valid private snapshot files through preflight and the run CLI.

## Notes On Resolved/Healthy Areas

- All five Review 07 findings are substantially addressed: enumerated staging
  entries receive real-path checks, both readiness commands share safe
  preflight, run dates are handed down deterministically, content configs are
  parsed/typed before agent startup with structured direct-CLI failures, and
  fleet output ownership is unambiguous.
- The full offline test suite, compile check, manifest validation, and dependency
  check pass in the configured runtime environment.
- Previously acknowledged deferrals are not repeated as new findings: the
  staged asset bundle is not yet a real git worktree, token/cost plus
  max-turn/max-token enforcement remain adapter follow-ups, and full TOCTOU
  symlink-race hardening remains outside M1.
