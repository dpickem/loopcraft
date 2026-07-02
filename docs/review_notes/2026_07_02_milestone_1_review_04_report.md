# Milestone 1 Review 04 — Report (branch implementation follow-up)

Review target: local branch `feat/m1-control-plane` in
`/Users/dpickem/workspace/loopcraft`.

Reviewed state:

- HEAD: `73975bd feat(cli): JSON-envelope parity across all CLIs via shared emitter`
- Included uncommitted changes in:
  - `src/loopcraft/research_intel/x/cli.py`
  - `tests/test_x_intel.py`

Reference scope:

- M1/M2 build plan in
  `obsidian/dpickem_default/40-Resources/blog_posts/loopcraft-implementation-design.html`
- `CONTRIBUTING.md`

This pass reviews the branch as a whole after substantial implementation changes: the M1
control-plane path, Codex runner, manifest/output contract, the migrated arXiv/X research-intel
loops, JSON CLI envelopes, runtime-neutral capability probes, and the public/private config split.
The exact path requested in the prompt under `docs/blog_posts/` did not exist; the implementation
design was found at `40-Resources/blog_posts/loopcraft-implementation-design.html`.

## Verification

Commands run from `/Users/dpickem/workspace/loopcraft`:

```text
make test && make compile && make validate && make check
```

Result:

- `make test`: passed, 105 tests.
- `make compile`: passed.
- `make validate`: passed, 3 manifests.
- `make check`: passed in this environment.

These checks are useful, but they do not yet exercise a real `loopctl run arxiv-intel` /
`loopctl run x-intel` path that proves the direct Python workflows produce the exact manifest
outputs resolved by the control plane.

## Findings

### 1. Research-loop skills can recursively invoke `loopctl run`

Relevant files:

- `skills/arxiv-intelligence-reporting/SKILL.md`
- `skills/x-intelligence-reporting/SKILL.md`
- `src/loopcraft/runners/codex.py`

Current state:

- `CodexRunner.build_prompt()` tells the headless agent that, if the skill invokes a repo-local CLI
  or Makefile target, it should run it from the source tree.
- The arXiv skill says to run `make run LOOP=arxiv-intel` for a fresh paper fetch.
- The X skill says to run `make run LOOP=x-intel` for a fresh fetch.
- Inside `loopctl run arxiv-intel` or `loopctl run x-intel`, those instructions point the executing
  agent back into `loopctl run` for the same loop, instead of into the deterministic direct CLI.

Why this matters:

The M1/M2 design relies on `loopctl run <loop>` being the control-plane invocation for one loop.
If the agent spawned by that invocation re-enters `make run LOOP=<same-loop>`, it can recurse,
spawn another headless agent run, or fail to produce the declared outputs within the current
`RunContext`. This is especially risky because the tests use a stub runner and do not exercise the
actual skill instructions.

Recommended fix:

- Make loop-execution skills distinguish "operator usage" from "inside a headless loop run."
- For `arxiv-intel` and `x-intel`, instruct the in-loop agent to run the direct CLI, for example
  `PYTHONPATH=src python -m loopcraft.research_intel.arxiv.cli run --config ...`, not
  `make run LOOP=...`.
- Alternatively, bypass agent interpretation for these Python-backed observe loops by adding a
  deterministic runner/adapter path that invokes the direct CLI and records outputs.
- Add a regression test that inspects the shipped skills or built prompt and fails if a loop skill
  tells its own `loopctl run` invocation to run `make run LOOP=<same-loop>`.

### 2. Direct arXiv/X CLIs do not write the manifest's `{{run_id}}` history outputs

Relevant files:

- `loops/arxiv-intel.yaml`
- `loops/x-intel.yaml`
- `src/loopcraft/cli.py`
- `src/loopcraft/research_intel/arxiv/cli.py`
- `src/loopcraft/research_intel/arxiv/store.py`
- `src/loopcraft/research_intel/x/cli.py`
- `src/loopcraft/research_intel/x/store.py`

Current state:

- `loopctl run` resolves declared outputs by replacing `{{run_id}}` with the control-plane
  `Store.new_run_id()` value.
- The arXiv and X manifests declare run-scoped archives:
  `state/research/.../history/{{run_id}}.md` and `.json`.
- The direct arXiv CLI passes `run_stamp=now.strftime("%Y%m%dT%H%M%SZ")` to `write_digest()`.
- The direct X CLI does the same.
- Both stores write history archives as `history/{run_stamp}.md` and `history/{run_stamp}.json`,
  not as `history/{loopctl_run_id}.md/json`.

Why this matters:

Even if the headless agent runs the direct CLI successfully, the runner's declared-output check
looks for the exact paths from the manifest after `{{run_id}}` expansion. The direct CLIs naturally
produce timestamp-named history files instead, so `BaseRunner` can mark the run failed with missing
declared outputs even though the workflow wrote a digest. This breaks the manifest/I/O contract and
the design's "one loop writes declared outputs plus a durable run record" invariant.

Recommended fix:

- Pass the control-plane `run_id` into the direct CLI, for example through an env var such as
  `LOOPCRAFT_RUN_ID`, a `--run-id` flag, or a generated run context file.
- Use that run id for the manifest-declared history archive names.
- Add focused tests that resolve `arxiv-intel` and `x-intel` manifest outputs for a fake run id,
  invoke the deterministic direct workflow with mocked clients, and assert every resolved output
  exists and is listed in the run result.

### 3. Default `x-intel` can miss the declared `source-state.json` output

Relevant files:

- `config/x_intel.yaml`
- `loops/x-intel.yaml`
- `src/loopcraft/research_intel/x/cli.py`
- `src/loopcraft/research_intel/x/store.py`
- `tests/test_x_intel.py`

Current state:

- The committed public X config has no active sources: `list_ids`, `following_user_ids`,
  `following_snapshot`, `search_queries`, and `author_handles` are empty.
- `loops/x-intel.yaml` declares `state/research/x/source-state.json` as a required output.
- `IntelStore.remember_source_highwater()` writes `source-state.json` only when fetched posts
  contain numeric ids.
- On a credentialed run with the default empty-source config, `run()` can write the digest,
  `seen.json`, and `posts.jsonl`, but never create `source-state.json`.

Why this matters:

An observe loop with no new posts should still be able to complete with an empty digest if all
required state files are initialized or refreshed. As written, the shipped default config can make
`loopctl run x-intel` fail the declared-output check even when no API error occurred, because one
declared state output is absent.

Recommended fix:

- Initialize `source-state.json` during `IntelStore` construction or at the start/end of every
  successful run, even if it is `{}`.
- Add a test for the default empty-source config that runs the X workflow with a fake client/token
  and asserts `source-state.json` exists.
- Decide whether `source-state.json` should be required for all X runs or only when sources with
  high-water marks are configured; keep the manifest and store behavior aligned.

### 4. Public/private content config handling is documented but not staged by `loopctl run`

Relevant files:

- `README.md`
- `CONTRIBUTING.md`
- `src/loopcraft/worktree.py`
- `loops/arxiv-intel.yaml`
- `loops/x-intel.yaml`

Current state:

- README documents `config/x_intel.local.yaml` and `config/arxiv_intel.local.yaml` as private
  overrides for the research loops.
- `CONTRIBUTING.md` describes a public/private config split materialized into the isolated run
  worktree by `stage_loop_assets()`.
- `stage_loop_assets()` currently stages the skill directory and manifest, and applies env/local
  overrides for staged skill text assets.
- It does not stage `manifest.content.config`, nor does it resolve or materialize
  `config/*.local.yaml` for content configs.

Why this matters:

The design emphasizes clear source/memory boundaries and explicit loop inputs. If a headless run
must use research content configs, the control plane should make the effective config visible to
the run in a predictable way. Today the implementation relies on the agent running commands from
the source tree and on Makefile defaults to discover local config. That is weaker than the
documented public/private split and leaves `manifest.content.config` mostly advisory.

Recommended fix:

- Extend `stage_loop_assets()` to stage `manifest.content.config` and materialize the effective
  public/private override, or explicitly document that content configs are source-tree inputs and
  not staged in M1.
- Add tests for the expected behavior with `config/x_intel.local.yaml` and
  `config/arxiv_intel.local.yaml`.
- If local config files are intentionally read from the source tree, add them to the prompt's
  declared input/context so the headless agent and reviewer can see that dependency.

### 5. `make check` still probes future runtime binaries

Relevant files:

- `pyproject.toml`
- `src/loopcraft/cli.py`
- `README.md`
- `CONTRIBUTING.md`

Current state:

- M1 in the design depends on Codex, nv-tools, git, and Python.
- README says Claude/Cursor adapters land in later milestones.
- `[tool.loopcraft.dependencies]` includes `claude` and `cursor-agent`.
- `loopctl deps check` iterates every dependency in that table, so `make check` can fail on a
  Codex-only M1 setup because future runtime binaries are missing.

Why this matters:

This environment has `claude` and `cursor-agent`, so `make check` passed here. A clean M1
environment that matches the design may not. That creates a false "not ready" signal for code that
does not yet implement Claude/Cursor runners.

Recommended fix:

- Split dependency checks into required M1 dependencies and optional/future runtime probes.
- Make `loopctl deps check --loop <id>` check only the selected loop's declared runtime and
  dependencies.
- Keep all-runtime probing as an explicit command or JSON field that reports optional missing
  tools without failing the M1 check.

## Notes On Healthy Areas

- The branch has good offline unit coverage overall; the full local suite passed with 105 tests.
- The control-plane models have been moved to Pydantic and the CLI has a consistent JSON envelope.
- Runtime-neutral capability probes are centralized and reused by the Codex runner.
- The X fetch-path change in the working tree improves resilience by isolating per-source
  `XApiError`s and accumulating multiple errors instead of stopping at the first failed source.
