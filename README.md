# loopcraft

A small control plane that turns loops into running infrastructure, plus the
local intelligence workflows that seed the first fleet.

A loop is declared once as a vendor-neutral **manifest** (`loops/*.yaml`). A
**runtime adapter** translates it into a headless run on a specific vendor, a
thin **store** writes everything into a separate **memory tree**, and `loopctl`
ties it together. Source (this repo) and memory (`~/workspace/loopcraft_memory`)
are deliberately separate trees — see `loopcraft.toml`.

## Control plane (M1)

M1 proves the core run path end to end on a single vendor: the manifest schema +
validator, the Codex runtime adapter (`preflight` + `run`), `loopctl run` in an
isolated worktree, and the thin ledger write-path. The first loop is
`slack-triage` (L1) — observe-only, single connector, no upstream dependencies.

```bash
make list                       # show known loops
make validate                   # validate every manifest in loops/
make check                      # probe runtimes/tools + dry-run validate
make run LOOP=slack-triage      # run one loop now, headless
make run LOOP=arxiv-intel       # run daily arXiv intelligence through loopctl
make run LOOP=x-intel           # run daily X intelligence through loopctl
make status                     # last run per loop
make logs LOOP=slack-triage     # tail the last run's log
make test                       # unit tests
```

`loopctl` is the real interface; the `make` targets are thin wrappers. A run
writes its output(s) into the memory tree's ledger (e.g.
`ledger/slack/triage-latest.md`) and a durable run record to `ledger/runs/`.

Configuration lives in `loopcraft.toml` (default vendor, host, memory path).
Override the memory location at runtime with `LOOPCRAFT_MEMORY`.

> Requires the `codex` CLI and `nv-tools` on PATH to actually run `slack-triage`;
> `make check` reports anything missing before a run rather than failing at 3am.
> Claude/Cursor adapters, the scheduler, harvester, and UI arrive in later
> milestones (M2+).

### M2 Tasks

- Add scheduler/auth/apply: bootstrap the host, validate auth/env/tool
  dependencies, and render the loop manifests into deployable timers/services.
- Reorganize core control-plane plumbing into `loopcraft/control/` once the M2
  scheduler/auth/apply boundary lands. Keep this separate from research-loop
  cleanup so the control-plane refactor follows the new scheduler shape instead
  of pre-optimizing M1 modules.

### Runtime Models

Loop manifests specify a runtime in `runtime.vendor`, and may pin a model in
`runtime.model`. Keep reasoning effort separate in `runtime.reasoning_effort`;
do not bake it into the model slug (for example, use `model: gpt-5.5` plus
`reasoning_effort: medium`, not `gpt-5.5-medium`).

Codex model slugs available on this account (`codex debug models`):

| Model slug | Default effort | Supported efforts |
| --- | --- | --- |
| `gpt-5.6-sol` (account default) | `medium` | `low`, `medium`, `high`, `xhigh`, `max`, `ultra` |
| `gpt-5.6-terra` | `medium` | `low`, `medium`, `high`, `xhigh`, `max`, `ultra` |
| `gpt-5.6-luna` | `medium` | `low`, `medium`, `high`, `xhigh`, `max` |
| `gpt-5.5` | `xhigh` | `low`, `medium`, `high`, `xhigh` |
| `gpt-5.4` | `medium` | `low`, `medium`, `high`, `xhigh` |
| `gpt-5.4-mini` | `medium` | `low`, `medium`, `high`, `xhigh` |

Claude Code model values (`claude --help`):

| Model value | Meaning | Effort flag |
| --- | --- | --- |
| `sonnet` | Alias for the latest Sonnet available to Claude Code. Prefer this unless a loop needs a pinned version. | `--effort low|medium|high|xhigh|max` |
| `opus` | Alias for the latest Opus available to Claude Code. Use for review/checker-heavy loops. | `--effort low|medium|high|xhigh|max` |
| `claude-opus-4-8` | Example full model name accepted by Claude Code. Full model names are for reproducible pinning and may change as providers update catalogs. | `--effort low|medium|high|xhigh|max` |

### Layout

```
loops/            # loop manifests (one YAML per loop)
skills/           # vendor-neutral SKILL.md per loop (+ bundled Codex skills)
src/loopcraft/
  cli.py          # loopctl
  config.py       # loopcraft.toml + path resolution
  manifest.py     # LoopManifest schema, validator, dependency DAG check
  store.py        # the single sanctioned persistence path (ledger + run records)
  runners/        # the portability seam: base protocol + codex adapter
  research_intel/arxiv/    # arXiv intelligence loop implementation
  research_intel/x/        # X intelligence loop implementation
```

## Codex Setup

Install the repo-bundled Codex skills into your local Codex skill directory:

```bash
make install-codex
```

This copies bundled skills from `skills/` into `${CODEX_HOME:-$HOME/.codex}/skills` and validates them.

## Daily X Intelligence

This repo includes a Loopcraft loop plus a direct CLI that uses the official X API, stores persistent state in the memory ledger, ranks posts for loopcraft/frontier-model relevance, and emits a Markdown digest.

```bash
cp .env.example .env
python -m loopcraft.research_intel.x.cli snapshot-following
make run LOOP=x-intel
python -m loopcraft.research_intel.x.cli discover-follows --config config/x_intel.yaml
```

Fill in `X_API_BEARER_TOKEN` in `.env` before running. Tune the committed public
defaults in `config/x_intel.yaml`, or create a gitignored
`config/x_intel.local.yaml` for private/local overrides (including a private
`sources.following_snapshot`, e.g. `config/x_following_snapshot.local.json`).
Outputs are written to
`~/workspace/loopcraft_memory/ledger/research/x/` by default (`latest.md`,
`latest.json`, archived `history/*.md/json`, `seen.json`, `source-state.json`,
`posts.jsonl`).

For `snapshot-following`, set `X_API_OAUTH2_ACCESS_TOKEN` from X's OAuth 2.0 Authorization Code with PKCE flow. Client ID/secret alone are not enough for `/2/users/me`.

`discover-follows` reads the latest digest, filters out accounts from the
configured private following snapshot when one exists, hydrates candidate
profiles through X, and writes follow recommendations under
`~/workspace/loopcraft_memory/ledger/research/x/follow-candidates/`.

## Daily arXiv Intelligence

This repo also includes an arXiv abstract-first Loopcraft loop for ML, foundation models, LLMs, post-training, agentic workflows, harness/loop engineering, recursive self-improvement, and self-improving systems.

```bash
make run LOOP=arxiv-intel
```

Tune the committed public defaults in `config/arxiv_intel.yaml`, or create a
gitignored `config/arxiv_intel.local.yaml` for private/local overrides. Outputs
are written to `~/workspace/loopcraft_memory/ledger/research/arxiv/` by default
(`latest.md`, `latest.json`, archived `history/*.md/json`, `seen.json`,
`papers.jsonl`).

