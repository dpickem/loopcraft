# loopcraft

Local intelligence workflows for loop engineering, frontier-model practice, agents, evals, harnesses, and related AI systems work.

## Codex Setup

Install the repo-bundled Codex skills into your local Codex skill directory:

```bash
make install-codex
```

This copies `skills/x-intelligence-reporting` into `${CODEX_HOME:-$HOME/.codex}/skills` and validates it.

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

See [docs/x_intel_automation.md](docs/x_intel_automation.md) for Codex automation setup notes.
