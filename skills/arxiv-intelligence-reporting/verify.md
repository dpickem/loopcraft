# Verify — arxiv-intel

Goal (stop) conditions for one successful run. The loop is only "done" when all
of the following hold.

## Required outputs

- `state/research/arxiv/latest.md` and `state/research/arxiv/latest.json` exist
  and were written this run (the current digest, markdown + structured JSON).
- `state/research/arxiv/history/{{run_id}}.md` and
  `state/research/arxiv/history/{{run_id}}.json` exist as run-scoped archives, so
  prior digests are preserved rather than overwritten.

## Required state updates

- `state/research/arxiv/papers.jsonl` has the raw records for any newly fetched
  papers appended (no duplicates).
- `state/research/arxiv/seen.json` is updated so every paper included in this
  digest is recorded as seen; a rerun with no new papers yields an empty digest.

## Integrity checks

- No paper appears twice across runs (the seen set is respected).
- Ranking reflects the interests in `config/arxiv_intel.yaml`.
- No irreversible action is taken (observe tier: read-only).
