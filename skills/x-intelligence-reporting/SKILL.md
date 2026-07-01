---
name: x-intelligence-reporting
description: Summarize X/Twitter intelligence digests for ML, AI, frontier/foundation model practice, agents, evals, tool use, harness engineering, loop engineering, and loopcraft. Use when asked to run or interpret the loopcraft X intelligence workflow, prepare a daily Codex automation report from X posts, highlight frontier-lab posts, discover follow candidates, or extract direct links from X posts and linked external articles/blogs/papers.
---

# X Intelligence Reporting

## Workflow

1. Work in the loopcraft source tree from the runtime context (the repo with
   `Makefile`, `config/`, and `src/`) unless the user gives another workspace.
2. Run `make run LOOP=x-intel` when the user wants a fresh fetch through the control plane. Use `make daily-x-intel` only when debugging the direct CLI. If network access or X API credentials fail, report that first.
3. Read `state/research/x/latest.md` and `latest.json` from the loopcraft memory ledger (or the emitted direct-CLI paths). Prefer JSON for exact fields and Markdown for human-readable ordering.
4. When useful, run `PYTHONPATH=src python -m loopcraft.research_intel.x.cli discover-follows --config config/x_intel.json` and read the emitted follow-candidate Markdown/JSON paths.
5. When summarizing, include both the X post permalink and any external links from `entities.urls[*].expanded_url` or Markdown `External links:` lines.
6. Group findings by theme rather than only by score. Use these default themes:
   - Frontier-lab signal
   - Loop engineering and long-running agents
   - Harnesses, orchestration, verifiers, and memory
   - Evals, observability, traces, and post-training
   - Tool use, local systems, and production practice
7. Highlight frontier-lab posts separately. Treat frontier-lab attribution as explicit handle/config metadata, not loose profile-text inference.
8. Include follow recommendations when available. For each candidate, include why they surfaced and at least one evidence post link.
9. Call out practical loopcraft implications: what to read, what to implement, what to watch, and what should influence automation or agent-harness design.

## Reporting Standards

- Lead with errors, rate limits, auth failures, or empty fetches if present.
- Mention generated date and fetched/ranked counts when available.
- Link every cited X post using `https://x.com/{username}/status/{id}`.
- Link external resources explicitly by title or domain; do not hide important blog/paper links behind the X post alone.
- Avoid over-weighting repeated posts from one author; collapse near-duplicates in prose.
- Be clear when a conclusion is inferred from post text rather than verified from the linked article.
- Keep the report concise enough for daily reading, but include enough links that the user can immediately open the primary sources.

## Useful Commands

```bash
make daily-x-intel
PYTHONPATH=src python -m loopcraft.cli run x-intel
PYTHONPATH=src python -m loopcraft.research_intel.x.cli snapshot-following
PYTHONPATH=src python -m loopcraft.research_intel.x.cli discover-follows --config config/x_intel.json
PYTHONPATH=src python -m pytest -q
```

## Expected Output Shape

Use this structure unless the user asks for something else:

```markdown
**Daily X Intelligence**
Fetched N posts; ranked M. Errors: none.

**Main Findings**
- Theme: concise synthesis. X post: [author](...). External: [resource](...). Why it matters: ...

**Frontier-Lab Highlights**
- Lab/person: summary. X post: [...](...). External: [...](...).

**Follow Candidates**
- Candidate: why they surfaced. Profile: [...](...). Evidence: [...](...).

**Loopcraft Implications**
- Actionable observation for loop engineering, harnesses, evals, agents, or tool use.
```
