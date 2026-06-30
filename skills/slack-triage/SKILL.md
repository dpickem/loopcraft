---
name: slack-triage
description: >-
  Triage and summarize everything aimed at the operator on Slack — @mentions,
  DMs, group DMs, and a configured list of high-signal channels — into a single
  categorized digest. Observe-only: never send a message.
readonly: true
tools: [nv-tools.slack]
verify: "state/slack/triage-latest.md exists and lists >=1 categorized item; state/slack/seen.json updated"
---

# Slack triage & summarizer

You are an observe-tier loop. You **read** Slack and **write two files**: the
markdown digest (`state/slack/triage-latest.md`) and the JSON cursor
(`state/slack/seen.json`). You never send, react to, or edit anything in Slack.

## Scope

- @mentions of the operator
- direct messages (DMs)
- group DMs
- channels listed in `skills/slack-triage/channels.txt` (one channel per line;
  ignore blank lines and `#` comments). In a staged run this file is the
  effective list already resolved from the public/private config split
  (env var > `channels.local.txt` > committed public file); just read it.

## Window

- Process messages since the last run. Use `state/slack/seen.json` as the cursor
  (a map of `channel/thread -> last_seen_ts`). If it is missing, look back 24h.
- After triage, update `state/slack/seen.json` with the newest timestamps seen.

## Procedure

1. Use `nv-tools slack` to fetch mentions, DMs, group DMs, and the configured
   channels within the window. Deduplicate by thread.
2. Categorize every item as exactly one of:
   - **ACTION** — needs a reply or a decision from the operator
   - **REVIEW** — FYI that the operator should read but likely won't act on
   - **INFO** — purely informational / ambient
3. Write the digest to the absolute output path given in the I/O contract
   (`state/slack/triage-latest.md`), using the structure below.

## Output format (`state/slack/triage-latest.md`)

```markdown
# Slack triage — <YYYY-MM-DD HH:MM>

## Action required
- [ ] <one line>: who / where (link) — why it needs you

## Review / FYI
- <one line>: who / where (link)

## Informational
- <one line>: who / where (link)

## Prioritized checklist
1. <highest-priority action item>
2. ...
```

Lead with **Action required**; end with the prioritized checklist. If a section
is empty, write `- none`.

## Hard rules

- **Never send, react to, schedule, or draft-into-Slack anything.** This loop is
  read-only. Replies/drafts are a future `propose`-tier capability and are out of
  scope here.
- Write only the declared output paths (the digest `state/slack/triage-latest.md`
  and the cursor `state/slack/seen.json`). Do not create side databases or other
  files.
