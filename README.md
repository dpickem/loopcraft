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
  arxiv_intel/    # L2 source prototype (migrated onto the unified store in M2)
  x_intel/        # L2 source prototype (migrated onto the unified store in M2)
```

## Codex Setup

Install the repo-bundled Codex skills into your local Codex skill directory:

```bash
make install-codex
```

This copies bundled skills from `skills/` into `${CODEX_HOME:-$HOME/.codex}/skills` and validates them.

## Daily X Intelligence

This repo includes a compact, automation-ready CLI that uses the official X API, stores persistent local state in SQLite, ranks posts for loopcraft/frontier-model relevance, and emits a Markdown digest.

```bash
cp config/x_intel.example.json config/x_intel.json
cp .env.example .env
python -m loopcraft.x_intel.cli snapshot-following
python -m loopcraft.x_intel.cli run --config config/x_intel.json
python -m loopcraft.x_intel.cli discover-follows --config config/x_intel.json
```

Fill in `X_API_BEARER_TOKEN` in `.env` before running. Outputs are written to `var/x_intel/digests/` by default. Local SQLite state is written to `var/x_intel/state.sqlite3` and is intentionally ignored by git.

For `snapshot-following`, set `X_API_OAUTH2_ACCESS_TOKEN` from X's OAuth 2.0 Authorization Code with PKCE flow. Client ID/secret alone are not enough for `/2/users/me`.

`discover-follows` reads the latest digest, filters out accounts already in `config/x_following_snapshot.json`, hydrates candidate profiles through X, and writes follow recommendations under `var/x_intel/follow_candidates/`.

## Daily arXiv Intelligence

This repo also includes an arXiv abstract-first workflow for ML, foundation models, LLMs, post-training, agentic workflows, harness/loop engineering, recursive self-improvement, and self-improving systems.

```bash
cp config/arxiv_intel.example.json config/arxiv_intel.json
make daily-arxiv-intel
```

Outputs are written to `var/arxiv_intel/digests/` by default. Local SQLite state is written to `var/arxiv_intel/state.sqlite3` and is ignored by git.

See [docs/arxiv_intel_automation.md](docs/arxiv_intel_automation.md) for Codex automation setup notes.

See [docs/x_intel_automation.md](docs/x_intel_automation.md) for Codex automation setup notes.
