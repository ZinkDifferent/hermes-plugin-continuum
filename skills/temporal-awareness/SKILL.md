---
name: temporal-awareness
description: Verify current time, date, and timezone before any temporal claim. Convert relative time to absolute timestamps. Handle cross-host timezone coordination and log timestamp parsing.
category: devops
---

# Temporal Awareness

## ⚠️ MANDATORY: Load on EVERY reply

This skill is NOT optional. Load it at the START of every turn — regardless of whether the user's request appears to involve time. Context windows span sessions. A turn written at 23:00 and read at 12:00 the next day is 13 hours stale. Every turn is potentially a different point in time.

**Failure pattern (Jun 16, 2026):** Agent told the user "sleep well" and "Dr. Jenabi at 9 tomorrow" — but it was 12:50 PM the NEXT DAY. The appointments had already happened. The agent was speaking from stale session context, not reality. This is the exact failure this mandatory load prevents.

### The Protocol

1. **At the start of your reply**, before any reasoning, before any tool call:
   - Run `date +"%Y-%m-%d %H:%M:%S %Z (%:z)"`
   - Note the output. This is "now."
2. **Audit every temporal term** in your planned response:
   - "now" → verify it matches the system time you checked
   - "today" → is it still today relative to session start?
   - "yesterday" / "tomorrow" / "recently" / "soon" → if you're using these from session context, re-check against system time
3. **Correct stale context.** If the session started hours ago but you're still speaking as if "now" is yesterday, re-anchor your language.

### When you skip this

The user told me to load temporal-awareness EVERY reply and I still didn't. The consequence was giving bedtime wishes at midday after appointments had already passed. This is the #1 agent blind spot and the user explicitly flagged it.

## Rule

**Before every temporal statement** — "now", "recently", "this morning", "in 2 hours", "yesterday", "due soon", "expired", "last week" — you MUST verify the actual system time.

Context windows span sessions. A turn written at 03:00 and read at 15:00 is 12 hours stale. Relative terms rot immediately.

**Pitfall — Cron `next_run_at` goes stale between reads:** When you read a cron job's `next_run_at` field via `cronjob(list)` and then reference it later in conversation, the value may already be in the past. Cron schedules advance with every completed cycle. Always re-check (`date` + fresh cron list) before describing a scheduled time.

## Quick Check

```bash
date +"%Y-%m-%d %H:%M:%S %Z (%:z)"
```

Example output:

```text
2026-04-23 15:32:18 EDT (-04:00)
```

Use `timedatectl` when you also need NTP sync status or DST details:

```bash
timedatectl status
```

