# Daily X Intelligence Automation

The X intelligence workflow is a first-class Loopcraft loop. It can be run through
`loopctl` (`make run LOOP=x-intel`) or directly through its CLI for debugging.

## Local Setup

1. Tune `config/x_intel.json` sources if needed. If the changes are private/local,
   use gitignored `config/x_intel.local.json` instead.
2. Add one or more official X API sources:
   - `sources.following_snapshot`: preferred local snapshot of accounts you follow.
   - `sources.list_ids`: preferred for curated feeds.
   - `sources.following_user_ids`: expands an account's following list, then searches posts from those authors.
   - `sources.author_handles`: explicitly monitored handles.
   - `sources.search_queries`: topic queries for recent search.
3. Copy `.env.example` to `.env` and fill in credentials:

```bash
cp .env.example .env
```

4. Generate or refresh the followed-account snapshot:

```bash
python -m loopcraft.research_intel.x.cli snapshot-following
```

With `X_API_OAUTH2_ACCESS_TOKEN` from an OAuth 2.0 user-context flow, `snapshot-following` infers the current account from `/2/users/me`. `X_API_OAUTH2_CLIENT_ID` and `X_API_OAUTH2_CLIENT_SECRET` only identify the app; they do not authenticate your X user by themselves.

## Manual Run

```bash
make run LOOP=x-intel
make daily-x-intel
python -m loopcraft.research_intel.x.cli discover-follows --config config/x_intel.json
```

The command prints the generated digest path and writes Markdown plus JSON files into the
loopcraft memory ledger under `state/research/x/`. Follow recommendations are written under
`state/research/x/follow-candidates/`.

## Suggested Codex Recurring Instruction

Run this daily in `/Users/dpickem/workspace/loopcraft`:

```text
Run make run LOOP=x-intel. Then run python -m loopcraft.research_intel.x.cli discover-follows --config config/x_intel.json. Read state/research/x/latest.md and any follow-candidate paths printed by the commands. Summarize the most important posts for ML, AI, foundation/frontier model practice, harness engineering, loop engineering, loopcraft, agents, evals, and tool use. Highlight posts from frontier-lab authors separately. Include promising new people or organizations to follow, with evidence links. If either command reports X API rate limits, auth failures, or other errors, report those first and include the raw error summary.
```

## Notes

- The workflow uses the official X API. It does not scrape the browser.
- State is stored in the loopcraft memory ledger (`state/research/x/seen.json`,
  `source-state.json`, and `posts.jsonl`) so repeats avoid already-seen posts when the X API returns stable IDs. Each run writes an archived digest under `state/research/x/history/`, while `latest.md` / `latest.json` point at the newest digest.
- `X_API_BEARER_TOKEN` is the only required credential for the default app-only fetch path. `X_API_OAUTH2_ACCESS_TOKEN` is only needed if you want `/2/users/me` support. Codex provides the reasoning/summarization layer when the recurring automation runs; no OpenAI API key is needed in this repo for that path.
- The default config searches posts from `config/x_following_snapshot.json`, then applies an AI/model/evals/tool-use topic clause and local ranker filters.

## Related arXiv Automation

For daily paper discovery, run this in the same workspace:

```text
Run make run LOOP=arxiv-intel. Read state/research/arxiv/latest.md from the loopcraft memory ledger. Summarize the 5-10 most interesting papers for ML, foundation models, LLMs, post-training, harness/loop engineering, agentic workflows and use-cases, recursive self-improvement, and self-improving systems. Include arXiv abstract and PDF links. Report arXiv API errors first.
```
