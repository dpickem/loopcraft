# Daily arXiv Intelligence Automation

The arXiv workflow fetches abstract metadata from the official arXiv API, ranks papers against loopcraft interests, and writes Markdown plus JSON digests.

## Local Setup

1. Tune `config/arxiv_intel.json` categories and terms if needed. If the changes
   are private/local, use gitignored `config/arxiv_intel.local.json` instead.
2. Relevant fields:
   - `sources.categories`: arXiv categories such as `cs.AI`, `cs.CL`, `cs.LG`, `stat.ML`.
   - `sources.search_terms`: abstract/title search terms for foundation models, LLMs, post-training, agentic workflows, loop engineering, RSI, and self-improving systems.
3. Run through the control plane (preferred) or directly:

```bash
make run LOOP=arxiv-intel
make daily-arxiv-intel
```

## Suggested Codex Recurring Instruction

Run this daily in `/Users/dpickem/workspace/loopcraft`:

```text
Run make run LOOP=arxiv-intel. Read state/research/arxiv/latest.md from the loopcraft memory ledger. Summarize the 5-10 most interesting papers for ML, foundation models, LLMs, post-training, harness/loop engineering, agentic workflows and use-cases, recursive self-improvement, and self-improving systems. Include arXiv abstract and PDF links, and mention code/model/data links from arXiv comments when present. Report arXiv API errors first.
```

## Notes

- The workflow fetches metadata and abstracts only. It does not download PDFs.
- State is stored in the loopcraft memory ledger (`state/research/arxiv/seen.json` and `papers.jsonl`) so daily runs avoid repeating already-seen papers. Each run writes an archived digest under `state/research/arxiv/history/`, while `latest.md` / `latest.json` point at the newest digest.
- arXiv API docs: https://info.arxiv.org/help/api/user-manual.html
