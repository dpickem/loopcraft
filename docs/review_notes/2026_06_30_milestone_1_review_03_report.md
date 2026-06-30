# Milestone 1 Review 03 — Report (response follow-up)

Review target: committed local branch at `/Users/dpickem/workspace/loopcraft`.

Reviewed commits (hashes updated after the later history scrub that removed the
confidential channel list; originally 04ff46e / 0888646 / 80b0083):

- `9a0f311 feat: add M1 loopcraft control plane`
- `20c0e34 docs: add CONTRIBUTING guide`
- `1d0cdf1 docs: add M1 review notes (reviews 01 and 02)`

Reference baseline:

- `docs/review_notes/2026_06_30_milestone_1_review_02_report.md`
- `docs/review_notes/2026_06_30_milestone_1_review_02_response.md`
- M1 in `obsidian/dpickem_default/40-Resources/docs/loopcraft-implementation-design.html`
- `CONTRIBUTING.md`

This pass re-reviewed the branch after the Review 02 response, focusing on whether the stale-output
detection, `logic.skill` source-boundary validation, and M1 scheme-output rejection were actually
fixed and covered by tests.

## Verification

Commands run from `/Users/dpickem/workspace/loopcraft`:

```text
git status --short
make test && make compile && make validate
make check
PYTHONPATH=src python -m loopcraft.cli deps check --loop slack-triage
```

Result:

- `git status --short`: clean before writing this Review 03 report.
- `make test`: passed, 70 tests.
- `make compile`: passed.
- `make validate`: passed, 1 manifest.
- `make check`: passed.
- `loopctl deps check --loop slack-triage`: returned 1 with the expected loop-specific preflight
  failure in this environment: `auth bundle 'nv-tools': could not run 'nv-tools health'`.

## Findings

No blocking findings remain from Review 02. The three Review 02 issues are materially fixed:

- Stale pre-existing outputs no longer satisfy a clean Codex exit.
- `logic.skill` is validated as a safe source-relative path in manifest validation, preflight, and
  asset staging.
- Scheme-style outputs are rejected by validation for M1, so validate/run behavior now agrees.

### 1. M1 path vocabulary is still split between `state/...` and ledger-relative paths

Relevant files:

- `src/loopcraft/config.py`
- `src/loopcraft/manifest.py`
- `tests/test_store.py`
- `docs/review_notes/2026_06_30_milestone_1_review_02_response.md`

Current state:

- The Review 02 response says M1 supports only ledger `state/...` paths.
- `LoopManifest.validate()` rejects external sinks, but it still accepts unprefixed paths such as
  `slack/out.md` because `is_state_path()` treats any path without a scheme head as a store-owned
  ledger path.
- `LoopcraftConfig.resolve_state_path()` intentionally supports ledger-relative paths without the
  `state/` prefix, and `tests/test_store.py` covers that behavior.

Why this matters:

This is not a correctness blocker, because the accepted unprefixed paths are still safely resolved
inside `<memory>/ledger`. It is a contract clarity gap: M1 docs and response language say only
`state/...`, while the implementation supports a broader ledger-relative vocabulary.

Recommended fix:

- Choose one contract for M1.
- If `state/...` is the intended manifest vocabulary, reject unprefixed input/output paths in
  `LoopManifest.validate()` and keep ledger-relative paths as a lower-level `Store` API only.
- If ledger-relative paths are intentional, update the response/docs and add a manifest validation
  test that makes this explicit.

### 2. Prompt skill loading should share the same source resolver as preflight/staging

Relevant files:

- `src/loopcraft/runners/codex.py`
- `src/loopcraft/config.py`
- `tests/test_runner.py`

Current state:

- `CodexRunner.preflight()` resolves `logic.skill` through `config.resolve_source_path()`.
- `stage_loop_assets()` also validates and resolves the skill path through the safe source path
  helpers.
- `_build_prompt()` still reads skill text with `(ctx.config.source_path / loop.logic.skill).resolve()`
  directly.

Why this matters:

CLI validation makes this non-blocking for normal runs, because unsafe manifests are rejected before
the runner is invoked. Still, the runner now has two different source-path resolution paths, and
unit tests can call `_build_prompt()` / `run()` directly with unsafe manifests. Keeping all
source-path reads behind `resolve_source_path()` reduces future drift.

Recommended fix:

- Use `ctx.config.resolve_source_path(loop.logic.skill)` in `_build_prompt()`.
- If the skill path is unsafe or missing at prompt-build time, either omit embedded skill text with
  a clear problem earlier in preflight, or raise a runner-internal error covered by tests.
- Add a focused test that prompt skill loading uses the safe resolver path.

### 3. Slack skill wording still underspecifies the cursor output

Relevant files:

- `skills/slack-triage/SKILL.md`
- `loops/slack-triage.yaml`

Current state:

- The manifest now correctly declares `state/slack/seen.json` as both input and output.
- The skill frontmatter `verify` still mentions only `state/slack/triage-latest.md`.
- The skill body says the loop writes "one markdown file", while later instructions also require
  updating `state/slack/seen.json`.

Why this matters:

The runner will catch a missing or stale cursor now, so this is not a control-plane blocker. It can
still confuse the agent executing the loop: the first instruction says one markdown file, while the
I/O contract and later window instructions require a JSON cursor update.

Recommended fix:

- Update the skill frontmatter `verify` to include `state/slack/seen.json`.
- Change the opening instruction to say the loop writes one markdown digest plus the declared JSON
  cursor.
- Keep the hard rule aligned with the manifest, for example "Write only the declared output paths."

## Notes On Resolved/Healthy Areas

- Review 02's stale-output regression is covered by `test_run_flags_stale_unrefreshed_output`.
- Review 02's unsafe-skill-path regression is covered by manifest and worktree tests.
- Review 02's external-output mismatch is covered by manifest and CLI tests.
- The current M1 implementation remains offline-testable and passes the full local suite.
