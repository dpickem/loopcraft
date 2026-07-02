# Verify — slack-triage

Goal (stop) conditions for one successful run. The loop is only "done" when all
of the following hold. If any check fails, the run is incomplete.

## Required outputs

- `state/slack/triage-latest.md` exists and was written this run. It contains:
  - a top-level "Themes at a glance" overview listing the themes discussed,
  - a "By theme" section grouping items thematically (not one flat bullet list),
  - an "Action required" section (may be empty, but the heading must be present),
  - at least one categorized item whenever there was any unseen activity.
- `state/slack/history/{{run_id}}.md` exists and is a run-scoped archive of the
  same digest, so previous digests are never overwritten.

## Required cursor update

- `state/slack/seen.json` is rewritten so that every message included in this
  digest is now recorded as seen. A subsequent run with no new activity must
  produce an empty (or explicitly "nothing new") digest.

## Integrity checks

- No message is reported twice across runs (the seen cursor is respected).
- The digest only references channels/DMs in scope for this loop.
- No irreversible action is taken (observe tier: read-only).
