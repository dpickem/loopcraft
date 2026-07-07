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

Install [uv](https://docs.astral.sh/uv/) and prepare the project environment:

```bash
uv sync
```

The Makefile uses `uv run` for every Python command, so tests, control-plane
commands, and direct intelligence CLIs all execute in the same locked project
environment.

```bash
make list                       # show known loops
make fleet                      # all loops in a table (schedule, last run, install state)
make validate                   # validate every manifest in loops/
make check                      # probe runtimes/tools + dry-run validate
make run LOOP=slack-triage      # run one loop now, headless
make run LOOP=arxiv-intel       # run daily arXiv intelligence through loopctl
make run LOOP=x-intel           # run daily X intelligence through loopctl
make status                     # last run per loop
make logs LOOP=slack-triage     # tail the last run's log
make init                       # bootstrap the memory tree (M2)
make auth                       # credential status + guidance (M2)
make apply                      # validate the fleet + render systemd units (M2)
make remove LOOP=slack-triage   # undeploy one loop (inverse of apply) (M2)
make test                       # unit tests
```

`loopctl` is the real interface; the `make` targets are thin `uv run` wrappers.
A run
writes its output(s) into the memory tree's ledger (e.g.
`ledger/slack/triage-latest.md`) and a durable run record to `ledger/runs/`.

Configuration lives in `loopcraft.toml` (default vendor, host, memory path).
Override the memory location at runtime with `LOOPCRAFT_MEMORY`.

> Requires `uv`, the `codex` CLI, and `nv-tools` on PATH to actually run
> `slack-triage`;
> `make check` reports anything missing before a run rather than failing at 3am.
> `loopctl deps check` fails only on the **required** M1 binaries
> (`python` from uv's environment, `git`, `codex`, `nv-tools`); **optional**
> future-runtime binaries
> (`claude`, `cursor-agent`) are reported but never fail the check, so a
> Codex-only setup stays green. Use `loopctl deps check --loop <id>` to check
> just one loop's declared runtime and dependencies. The harvester and web UI
> arrive in later milestones (M4+).

## Runtime portability (M3)

The runtime is a config value, not a rewrite: the **Codex, Claude, and Cursor**
adapters all consume the same manifest, so an existing loop runs unchanged on
any of them. The vendor is resolved as: per-loop `runtime.vendor` (or
`loopctl run --vendor <v>`) → the global default in `loopcraft.toml`
(`default_vendor`) → `codex`. The `LOOPCRAFT_VENDOR` env var overrides the file
at runtime.

```bash
loopctl vendor list           # show adapters (codex/claude/cursor); marks the default
loopctl vendor get            # print the current default
loopctl vendor set claude     # flip the global default (writes loopcraft.toml)
loopctl run <loop> --vendor cursor   # one-off override for a single run
```

Each adapter shares the vendor-neutral prompt and dependency preflight and adds
only its own binary/model checks (`codex` / `claude` / `cursor-agent`). Model
values differ per vendor (see the tables below); Cursor is cross-provider so its
model is left to the CLI to validate.

## Scheduling & deployment (M2)

M2 turns one-shot `loopctl run` into a scheduled, unattended fleet on the
always-on host. Three commands stand it up:

> **Platform limitation:** M2 deployment is Linux/systemd-only. `loopctl apply`
> renders `.service`, `.timer`, and `.path` units and `--install` uses
> `systemctl`. macOS uses `launchd`, which is not implemented yet; on macOS use
> `loopctl run <loop>` manually or run deployment on a Linux host/VM.

```bash
loopctl init          # bootstrap the memory tree (ledger/runs/artifacts + git)
loopctl auth          # report credential status + guidance for every declared dep
loopctl apply ./loops # validate the fleet, then render systemd units
```

- **`loopctl init`** creates the memory-tree directories (`ledger/`,
  `ledger/runs/`, `artifacts/`, `var/systemd/`), `git init`s the memory tree
  (skip with `--no-git`), and confirms the source tree is usable. It is
  idempotent and never writes to the source repo.
- **`loopctl auth`** aggregates the `depends_on.auth`, `apis`, and `env`
  declared across all loops, probes each once (read-only), and reports which are
  satisfied on this host and how to fix the rest. This is the guided credential
  check the design runs before `apply`.
- **`loopctl apply`** runs the full pre-deploy check first — manifest schema,
  the cross-loop dependency DAG (duplicate ids, multi-producer outputs, unknown
  upstream loops, cycles), the scheduled environment (see below), and each
  loop's adapter preflight (tools/auth/env) — so **an unmet dependency is
  reported at `apply`, not at 3am**. It then renders each loop's `cadence` into
  systemd units under `<memory>/var/systemd/`:
  - a `cron` cadence renders a `.timer` + `.service` pair (`OnCalendar=` from the
    cron expression, `Persistent=true` so a trigger missed while the VM was down
    is caught up);
  - an `on-artifact` cadence renders a `.path` + `.service` pair that wakes the
    loop when an upstream ledger output changes;
  - `event` cadence has no unattended representation yet (it lands in M8).

### Trigger Options

Loop manifests choose their trigger with `cadence.type`:

- `cron`: rendered by `loopctl apply` as a systemd `.timer` + `.service`.
  `cadence.at` is a 5-field cron expression, translated to `OnCalendar=...`.
  Timers include `Persistent=true`, so a missed run is caught up once when the
  host comes back.
- `on-artifact`: rendered as a systemd `.path` + `.service`. Loopcraft watches
  declared ledger `state/...` inputs with `PathModified=...`; inputs that the
  same loop also writes are ignored so cursor files do not self-trigger.
- `event`: accepted in manifests for the future reactive/webhook model, but not
  deployable by M2. `apply` reports it as a render problem until M8.

The rendered `ExecStart` uses the **absolute** path of `scheduler.loopctl_bin`
(resolved on PATH when a bare name), since a systemd unit does not inherit your
shell PATH; `apply` fails if it cannot be resolved. For the same reason the
rendered unit sets `Environment=PATH=` to the **scheduled PATH**
(`scheduler.path`, or systemd's default when unset), and `apply` preflight
resolves runtime/tool binaries (`codex`, `nv-tools`, declared tools) against
that exact PATH — so a binary that only lives on your shell PATH does not make
`apply` pass. Default `apply` is **side-effect-free unless the plan is fully
clean** — a plan blocked by an unmet dependency writes nothing (pass
`--render-invalid` to render diagnostic units anyway), so `fleet` never shows
`staged` for a rejected loop.

Flags: `--dry-run` (validate + plan, write nothing), `--skip-preflight`
(structural + DAG checks only), `--render-invalid` (render even when checks
fail), `--out DIR` (render elsewhere), and `--install` (transactionally copy
units into the systemd unit directory and `enable --now` their triggers, with
rollback on failure — only when every check passed; requires `systemctl`).
- **`loopctl remove <loop>` / `loopctl remove --all`** is the inverse of
  `apply`: it disables a loop's timer/path trigger (`systemctl disable --now`)
  and deletes both its installed units (from the systemd unit dir) and its
  staged units (from `<memory>/var/systemd/`). It works from the files on disk,
  so a loop can be undeployed even after its manifest changed or was removed;
  `--dry-run` shows what would be removed.

`scheduler.environment_file` is the authority for a scheduled service's
credentials — a systemd unit does not see your `.env` or shell. So `apply` and
`auth` validate credentials against that file, not the operator's process
environment: **`apply` preflight and `auth` resolve declared env vars and auth
bundles (e.g. `x-api`'s token) from the environment file only**, and the file
must exist, live outside **both** git trees, and contain every env var the loops
declare. A token that lives only in your shell/`.env` therefore does *not* make
`apply` pass (the deployed service wouldn't have it), and a token that lives only
in the environment file *does*. Direct `loopctl run` is unchanged: it executes
in your current process, so its preflight uses the live environment (including
`.env`).

Per-host deployment settings (unit scope, service `User=`, the out-of-tree
secrets `EnvironmentFile=`, the `loopctl` path) live in the `[scheduler]` table
of `loopcraft.toml`. Secrets stay outside **both** git trees; manifests
reference credential names, never values.

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
  scheduler.py    # cron -> systemd OnCalendar + timer/service/path unit rendering
  deploy.py       # fleet pre-deploy validation, unit planning, install (apply)
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
uv run loopcraft-x-intel snapshot-following
make run LOOP=x-intel
uv run loopcraft-x-intel discover-follows --config config/x_intel.yaml
```

Fill in `X_API_BEARER_TOKEN` (or `X_API_OAUTH2_ACCESS_TOKEN`; either satisfies the loop's `x-api` auth dependency) in `.env` before running. Tune the committed public
defaults in `config/x_intel.yaml`, or create a gitignored
`config/x_intel.local.yaml` for private/local overrides (including a private
`sources.following_snapshot`, e.g. `config/x_following_snapshot.local.json`).
When a `*.local.yaml` sibling exists it is preferred automatically whether the
loop runs via `make`/`loopctl run` or the direct CLI, and the control plane
stages the effective config into the isolated run worktree. Outputs are written to
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
gitignored `config/arxiv_intel.local.yaml` for private/local overrides. A
`*.local.yaml` sibling is preferred automatically (via `make`/`loopctl run` or
the direct CLI) and staged into the run worktree by the control plane. Outputs
are written to `~/workspace/loopcraft_memory/ledger/research/arxiv/` by default
(`latest.md`, `latest.json`, archived `history/*.md/json`, `seen.json`,
`papers.jsonl`).
