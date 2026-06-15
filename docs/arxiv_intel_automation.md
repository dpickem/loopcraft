# Daily arXiv Intelligence Automation

The arXiv workflow fetches abstract metadata from the official arXiv API, ranks papers against loopcraft interests, and writes Markdown plus JSON digests.

## Local Setup

1. Copy `config/arxiv_intel.example.json` to `config/arxiv_intel.json`.
2. Tune categories and terms if needed:
   - `sources.categories`: arXiv categories such as `cs.AI`, `cs.CL`, `cs.LG`, `stat.ML`.
   - `sources.search_terms`: abstract/title search terms for foundation models, LLMs, post-training, agentic workflows, loop engineering, RSI, and self-improving systems.
3. Run:

```bash
./scripts/daily_arxiv_intel.sh
```

## Suggested Codex Recurring Instruction

Run this daily in `/Users/dpickem/workspace/loopcraft`:

```text
Run ./scripts/daily_arxiv_intel.sh. Read the generated Markdown digest path printed by the command. Summarize the 5-10 most interesting papers for ML, foundation models, LLMs, post-training, harness/loop engineering, agentic workflows and use-cases, recursive self-improvement, and self-improving systems. Include arXiv abstract and PDF links, and mention code/model/data links from arXiv comments when present. Report arXiv API errors first.
```

## Notes

- The workflow fetches metadata and abstracts only. It does not download PDFs.
- State is stored locally in SQLite so daily runs avoid repeating already-seen papers.
- arXiv API docs: https://info.arxiv.org/help/api/user-manual.html
