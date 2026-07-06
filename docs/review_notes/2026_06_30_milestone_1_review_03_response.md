# Milestone 1 Review 03 — Response

Response to [`2026_06_30_milestone_1_review_03_report.md`](2026_06_30_milestone_1_review_03_report.md)
(the follow-up re-review of the committed M1 branch). Prior history:
[01 report](2026_06_30_milestone_1_review_01_report.md) /
[01 response](2026_06_30_milestone_1_review_01_response.md),
[02 report](2026_06_30_milestone_1_review_02_report.md) /
[02 response](2026_06_30_milestone_1_review_02_response.md).

Review 03 reported no blocking findings; the three items are contract-clarity/consistency
improvements. All three are addressed below, each with a regression test.

## Commit

The three fixes are in one commit on `main`:

```text
fe6e5dc fix: address M1 review 03 clarity findings
```

Inspect with `git show fe6e5dc -- <file>` using the file lists below.

## Status

Verification from the repo root:

```text
make test && make compile && make validate
```

- `make test`: **75 passed** (up from 70 at review time), fully offline.
- `make compile`: passes.
- `make validate`: passes, 1 manifest.

## Findings

### 1. M1 path vocabulary is now a single contract: `state/...` — fixed

The manifest vocabulary is now exactly `state/...`. Bare ledger-relative paths remain a
lower-level `Store` convenience but are no longer accepted in a manifest, so the docs/response
language and the implementation agree.

- `LoopManifest.validate()` now classifies every `inputs`/`outputs` entry: an absolute path is
  rejected as absolute; a `state/...` path is validated against the ledger (no `..`/escape); a
  bare ledger-relative path such as `slack/out.md` or a `ledger/...` path is rejected with
  `must use the 'state/...' prefix`; and a scheme target (`linear:...`) is rejected as an
  unsupported external sink.
- `LoopcraftConfig.resolve_state_path()` is unchanged and still accepts ledger-relative paths,
  so the `Store` API (and its tests, e.g. `research/themes.md`) keep working for internal,
  non-manifest writes.
- Files: `src/loopcraft/manifest.py`.
- Tests (`tests/test_manifest.py`): `test_unprefixed_ledger_path_requires_state_prefix`,
  `test_state_prefixed_paths_are_accepted` (and the existing
  `test_unsafe_state_paths_are_reported` still covers absolute/`..`).

### 2. Prompt skill loading shares the safe source resolver — fixed

`CodexRunner._build_prompt()` previously read the skill with a raw
`(source_path / logic.skill).resolve()`. It now goes through
`ctx.config.resolve_source_path(loop.logic.skill)`, the same helper used by `preflight()` and
`stage_loop_assets()`. An unsafe path raises `SourcePathError`, which is caught so no skill text
outside the source tree is embedded (unsafe manifests are already rejected earlier by
preflight/validation).

- Files: `src/loopcraft/runners/codex.py`.
- Tests (`tests/test_runner.py`): `test_build_prompt_uses_safe_source_resolver` (a traversing
  `logic.skill` does not leak file contents into the prompt) and
  `test_build_prompt_embeds_valid_skill` (the happy path still embeds the skill).

### 3. Slack skill wording matches the cursor contract — fixed

The skill no longer says it writes "one markdown file" while also requiring a JSON cursor.

- `skills/slack-triage/SKILL.md`: the frontmatter `verify` now includes
  `state/slack/seen.json updated`; the intro states the loop writes two files (the digest plus
  the `state/slack/seen.json` cursor); and the hard rule names both declared output paths.
- Files: `skills/slack-triage/SKILL.md`.
- Test (`tests/test_manifest.py`): `test_slack_skill_verify_mentions_cursor` guards the verify
  rubric against regressing back to digest-only wording.

## Notes on resolved/healthy areas

Review 03 confirmed the Review 02 fixes hold (stale-output detection, `logic.skill`
source-boundary validation, and M1 scheme-output rejection), each with regression coverage, and
that the suite remains offline-testable. Those areas are unchanged here.
