---
name: arxiv-intelligence-reporting
description: Summarize arXiv paper intelligence digests for ML, AI, foundation/foundation models, LLMs, post-training, agentic workflows, evals, harness engineering, loop engineering, recursive self-improvement, and self-improving systems. Use when asked to run or interpret the loopcraft arXiv workflow, prepare a daily paper digest, or identify the most interesting recent arXiv submissions.
---

# arXiv Intelligence Reporting

## Workflow

1. Work in the loopcraft repo unless the user gives another workspace.
2. Run `make daily-arxiv-intel` when the user wants a fresh paper fetch. If network access or arXiv API errors occur, report that first.
3. Read the emitted `ARXIV_DIGEST_MARKDOWN=...` and `ARXIV_DIGEST_JSON=...` paths. Prefer JSON for exact fields and Markdown for human-readable ordering.
4. Summarize the 5-10 most interesting papers, focusing on:
   - Foundation/frontier models and LLM systems
   - Post-training, preference optimization, RLHF, and evals
   - Agentic workflows, tool use, planning, memory, and orchestration
   - Harnesses, loop engineering, verifiers, and production agent infrastructure
   - Recursive self-improvement, self-improving systems, and self-evolving agents
5. Include arXiv abstract links and PDF links for every cited paper.
6. Mention code, model, dataset, or project links from arXiv comments when present.
7. Be clear when a conclusion is inferred from abstract metadata rather than the full paper.

## Reporting Standards

- Lead with errors, empty fetches, or suspiciously low paper counts.
- Mention generated date and fetched/ranked counts when available.
- Group findings by theme rather than only by score.
- Prefer papers with concrete systems, benchmarks, training recipes, eval methods, agent loops, or self-improvement mechanisms.
- Avoid presenting broad surveys as the top signal unless they contain unusually useful taxonomy or synthesis.
- End with loopcraft implications: what to read, what to prototype, what to add to eval/harness practice, and what to watch.

## Useful Commands

```bash
make daily-arxiv-intel
PYTHONPATH=src python -m loopcraft.arxiv_intel.cli run --config config/arxiv_intel.json --include-seen
PYTHONPATH=src python -m pytest -q
```

## Expected Output Shape

```markdown
**Daily arXiv Intelligence**
Fetched N papers; ranked M. Errors: none.

**Most Interesting Papers**
- Title: concise synthesis. arXiv: [...](...). PDF: [...](...). Code/data/model: [...](...). Why it matters: ...

**Themes**
- Theme: papers and shared implication.

**Loopcraft Implications**
- Actionable observation for loop engineering, harnesses, evals, agents, tool use, or self-improving systems.
```

