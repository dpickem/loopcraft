# Milestone 1 Review 08 — Response

Response to [`2026_07_02_milestone_1_review_08_report.md`](2026_07_02_milestone_1_review_08_report.md),
the whole-branch follow-up review of `feat/m1-control-plane` after the Review 07
fixes. Prior history:
[01 report](2026_06_30_milestone_1_review_01_report.md) /
[01 response](2026_06_30_milestone_1_review_01_response.md),
[02 report](2026_06_30_milestone_1_review_02_report.md) /
[02 response](2026_06_30_milestone_1_review_02_response.md),
[03 report](2026_06_30_milestone_1_review_03_report.md) /
[03 response](2026_06_30_milestone_1_review_03_response.md),
[04 report](2026_07_02_milestone_1_review_04_report.md) /
[04 response](2026_07_02_milestone_1_review_04_response.md),
[05 report](2026_07_02_milestone_1_review_05_report.md) /
[05 response](2026_07_02_milestone_1_review_05_response.md),
[06 report](2026_07_02_milestone_1_review_06_report.md) /
[06 response](2026_07_02_milestone_1_review_06_response.md),
[07 report](2026_07_02_milestone_1_review_07_report.md) /
[07 response](2026_07_02_milestone_1_review_07_response.md).

All four findings are addressed, each with regression tests.

## Commit

The fixes are in one commit on `feat/m1-control-plane`:

```text
5e6d470 fix: address M1 review 08 findings
```

Inspect with `git show 5e6d470 -- <file>` using the file lists below.

## Status

Verification from the repo root:

```text
make test && make compile && make validate
```

- `make test`: **198 passed** (up from 182 at review time), fully offline.
- `make compile`: passes.
- `make validate`: passes, 3 manifests.

Every focused diagnostic from the report now fails safely:

```text
skill -> source-root symlink:  REFUSED: skill asset escapes allowed root: .../skills/demo/all-source
                               (no .env staged into the worktree)
outside local-config symlink:  preflight problem 'content.config: content config local
                               override escapes allowed root: ...' (matches staging)
traversal run/date stamps:     CLI: structured rc-2 'invalid LOOPCRAFT_RUN_ID/RUN_DATE' failure;
                               store: 'digest output escapes allowed root' (nothing written outside)
OAuth-only environment:        x-intel preflight passes (no missing-bearer problem)
missing configured snapshot:   rc 1 with named error 'snapshot source: following snapshot
                               not found: ...' (regression test; no silent empty digest)
```

## Findings

### 1. Skill walker could stage unrelated in-repo secrets; alias/preflight inconsistencies — fixed

Containment was scoped to the whole repository, so a skill directory symlink to
the source root staged everything reachable (including gitignored `.env`); the
global visited-set could drop allowed directory aliases; and preflight read a
local config sibling without the containment staging applies.

- `_copytree_contained()` now scopes containment to the **resolved declared
  skill directory**: every enumerated entry must resolve under the skill root,
  not merely the repo. A directory symlink to the source root (or any unrelated
  in-repo location) aborts staging with `SourcePathError`; a symlink resolving
  under the skill root is still dereference-copied. This is deliberately
  stricter than Review 07's repo-wide rule — the staged bundle is supposed to
  be minimal, and cross-skill reaches were never a supported pattern.
- Cycle detection is ancestry-local (a branch stops only when a directory
  reappears in its own ancestor chain), so two allowed aliases of one in-skill
  directory each materialize their contents instead of depending on traversal
  order.
- `_check_content_config_validity()` applies the same containment guard to the
  `*.local.*` sibling (via the shared `local_sibling_path` + `assert_under`
  pair staging uses) **before** any validator reads it, so preflight and
  staging can no longer disagree about a symlinked local override.
- Files: `src/loopcraft/worktree.py`, `src/loopcraft/runners/capabilities.py`.
- Tests: `tests/test_worktree.py::test_stage_rejects_skill_symlink_to_unrelated_repo_dir`
  (asserts no `.env` reaches the worktree),
  `::test_stage_materializes_in_skill_directory_aliases_independently`;
  `tests/test_capabilities.py::test_capabilities_reject_symlinked_local_override_escaping_source`.
  The Review 06/07 symlink tests (in-skill sibling links, declared-dir alias)
  still pass, confirming the allowed direction is preserved.

### 2. Run-id/date env values could traverse digest and history directories — fixed

Both workflows trusted inherited `LOOPCRAFT_RUN_ID`/`LOOPCRAFT_RUN_DATE` values
and appended them directly to resolved directories.

- The protocol values are now validated at both direct-CLI boundaries before
  any work, via shared helpers in `loopcraft.config`:
  `validate_run_id_stamp()` requires the canonical control-plane run-id shape
  (`<YYYYMMDDTHHMMSSZ>-<hex8>`, one safe filename component) and
  `validate_run_date_stamp()` requires a real ISO `YYYY-MM-DD` calendar date.
  `resolve_run_stamps()` applies both, keeping the current-clock fallback only
  for standalone runs where neither variable is set. An invalid inherited value
  becomes the standard structured failure (exit code 2), not a fallback or a
  traceback.
- Defense in depth: both stores compose every history/digest filename through a
  new shared `paths.contained_child()`, which asserts the composed path stays
  under its already-resolved directory before anything is written.
- Files: `src/loopcraft/config.py`, `src/loopcraft/paths.py`,
  `src/loopcraft/research_intel/arxiv/cli.py`,
  `src/loopcraft/research_intel/arxiv/store.py`,
  `src/loopcraft/research_intel/x/cli.py`,
  `src/loopcraft/research_intel/x/store.py`.
- Tests: `tests/test_x_intel.py::test_x_run_rejects_traversal_run_stamp_env`
  and `tests/test_arxiv_intel.py::test_arxiv_run_rejects_malformed_run_date_env`
  (structured rc-2 failures, nothing written outside);
  `::test_x_store_write_digest_contains_stamps` /
  `::test_arxiv_store_write_digest_contains_stamps` (store guard). Valid
  control-plane values remain covered by the existing run-id/run-date
  declared-output tests.

### 3. The X manifest rejected an OAuth credential the runtime supports — fixed

`depends_on.env: [X_API_BEARER_TOKEN]` was stricter than — and contradictory
to — the `x-api` auth probe and `XApiTokens.token()`, both of which accept
either credential.

- The report's first option is taken: the exact bearer env declaration is
  removed from `loops/x-intel.yaml` and the `x-api` auth probe owns the
  either-one requirement, so all layers (manifest, preflight, runtime) agree.
  The manifest comments the delegation explicitly, and the README now says
  either token satisfies the loop's auth dependency. A general `any_of`
  manifest vocabulary was considered and deferred: with the auth-bundle
  abstraction already expressing the alternative, no second mechanism is
  needed in M1.
- Files: `loops/x-intel.yaml`, `README.md`.
- Tests: `tests/test_capabilities.py::test_x_api_auth_probe_accepts_either_token`
  (bearer-only, OAuth-only, both → pass; neither → problem naming both
  variables); `tests/test_manifest.py::test_x_manifest_lets_auth_probe_own_credential_choice`
  (the manifest cannot silently re-pin one env var).

### 4. The X following snapshot was an undeclared dependency that failed open — fixed

The configured snapshot was not validated, not staged, silently treated as
empty when missing, and could crash the CLI (or read outside the tree) when
malformed or absolute.

- `sources.following_snapshot` is now validated by the content model as a safe
  source-relative path (absolute and traversing values fail Pydantic
  validation, which the Review 07 machinery already surfaces as preflight
  problems and structured direct-CLI failures).
- Preflight validates the snapshot as a declared dependency: the registered
  `x-intel` content validator resolves it under the source root and runs a new
  strict loader (`load_following_snapshot_handles`) that requires a regular
  file, valid JSON, and the expected `{"users": [...]}` shape.
- The snapshot is staged into the run worktree: a content-asset resolver
  registry (`CONTENT_ASSET_RESOLVERS`, alongside `CONTENT_VALIDATORS`) extracts
  referenced asset paths from the effective config, and `stage_loop_assets()`
  stages them verbatim after the shadowing pass — so a `*.local.*` snapshot
  keeps the exact name the config refers to and the bundle is complete and
  portable.
- At run time the snapshot fetch uses the same strict loader; a missing or
  malformed configured snapshot becomes a **named source error** through the
  existing per-source isolation (`snapshot source: following snapshot not
  found: ...`, exit code 1) instead of a silently empty successful digest, and
  a malformed file can no longer escape as a traceback. The tolerant reader
  remains only for `discover-follows`, where the snapshot is a filter input,
  not a fetch source.
- Files: `src/loopcraft/research_intel/x/config.py`,
  `src/loopcraft/research_intel/x/follow_discovery.py`,
  `src/loopcraft/research_intel/x/cli.py`,
  `src/loopcraft/runners/capabilities.py`, `src/loopcraft/worktree.py`,
  `src/loopcraft/cli.py`.
- Tests: `tests/test_x_intel.py::test_x_config_rejects_unsafe_following_snapshot_paths`,
  `::test_load_following_snapshot_handles_is_strict` (missing, malformed,
  wrong shape, valid),
  `::test_x_run_reports_missing_snapshot_as_named_source_error` (run CLI: rc 1
  with the named error);
  `tests/test_capabilities.py::test_capabilities_flag_missing_following_snapshot`,
  `::test_capabilities_flag_malformed_following_snapshot`,
  `::test_capabilities_accept_valid_following_snapshot` (also covers the asset
  resolver); `tests/test_worktree.py::test_stage_extra_assets_stages_verbatim_and_requires_existence`.

## Notes on healthy areas

Review 08 confirmed all five Review 07 fixes are substantially in place and
that the full offline suite, compile check, manifest validation, and dependency
check pass in the configured runtime environment. Previously acknowledged
deferrals remain unchanged and are not re-argued: the staged asset bundle is
not yet a real git worktree, token/cost plus max-turn/max-token enforcement
remain adapter follow-ups, and full TOCTOU symlink-race hardening stays outside
M1.
