# Verify — x-intel

Goal (stop) conditions for one successful run. The loop is only "done" when all
of the following hold.

## Required outputs

- `state/research/x/latest.md` and `state/research/x/latest.json` exist and were
  written this run (the current digest, markdown + structured JSON).
- `state/research/x/history/{{run_id}}.md` and
  `state/research/x/history/{{run_id}}.json` exist as run-scoped archives, so
  prior digests are preserved rather than overwritten.

## Required state updates

- `state/research/x/posts.jsonl` has the raw records for any newly fetched posts
  appended (no duplicates).
- `state/research/x/seen.json` is updated so every post included in this digest
  is recorded as seen; a rerun with no new posts yields an empty digest.
- `state/research/x/source-state.json` is updated with the latest per-source
  fetch cursors.

## Integrity checks

- No post appears twice across runs (the seen set is respected).
- Ranking/frontier-lab weighting reflects `config/x_intel.yaml`.
- No irreversible action is taken (observe tier: read-only).
