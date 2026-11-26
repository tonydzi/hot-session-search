---
name: hot-session-search
description: Search the last 14 days of ALL agent sessions (Claude Code, Codex, Gemini CLI, any JSONL) before starting work. Use when the user says "recall", "did we do this already", "find that session", "what did we decide about X", or before building something that may already exist.
---

# Hot session search

One local index over every agent session from the last 14 days. No LLM, no server, no distillation needed.

## When to use
- Before building anything: "do we already have this?"
- The user asks to recall a past session, decision, command or phrase.
- A new session starts and the topic may have been worked on in another tool (Codex, Gemini, another machine).

## How
1. Refresh the index (fast, incremental):
   `python3 ~/.claude/skills/hot-session-search/hotmem.py ingest`
2. Search with the user's own words (2 to 6 meaningful words work best):
   `python3 ~/.claude/skills/hot-session-search/hotmem.py search "<words>"`
3. Open the top 1 to 3 session files from the output (the path is the last field) and read the matching passage before answering. A snippet is a lead, not proof.
4. Answer with: what was found, in which session (date, tool), and what was NOT covered (older than 14 days, a machine that is not synced).

## Rules
- Nothing found means "not in the last 14 days of indexed folders", never "it never happened".
- Run `hotmem.py stats` to see which tools and folders are covered and how fresh the index is.
- Do not paste whole transcripts back to the user; quote the needed lines.
