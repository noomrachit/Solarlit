---
name: Context optimization workflow
description: 3-step routine for keeping Claude Code context usage low when working in this repo
---

**Rule:** At the start of each new work session/phase, and periodically during long sessions, follow
this 3-step routine to keep context usage under control:

1. **New session → `/context` baseline.** Start a new phase of work in a fresh session rather than
   continuing an old one indefinitely, then run `/context` to see how much context is already used
   before doing anything else.
2. **`/mcp` → trim unused servers.** Open the MCP management view and disable/remove any MCP servers
   not needed for that round of work. Fewer connected servers means fewer tool definitions and less
   background data the model has to carry in context.
3. **`CLAUDE.md` → progressive disclosure.** Keep the main `CLAUDE.md` limited to the commands and
   architecture notes needed on almost every task. Push specialized, narrow-topic details into separate
   files under `.agents/memory/` (as already done for BigInt handling, the auth flow, Python venvs,
   etc.) and reference them by path from `CLAUDE.md` instead of inlining them. This repo's `CLAUDE.md`
   already follows this pattern — keep new additions consistent with it rather than growing the main
   file directly.

**Why:** Larger context windows cost more to process per turn and dilute the relevance of what's
actually loaded (more tools to pick from, more docs to skim). Trimming at session start and keeping
docs split by topic keeps each session's context focused on the task at hand.
