# hot-session-search

Find anything you discussed with **any** coding agent in the last 14 days: Claude Code, Codex, Gemini CLI, or anything that writes JSONL. One Python file, stdlib only, SQLite full-text search. No LLM, no server, no API key.

```
$ python3 hotmem.py search "hot search distillation"
hot search (last 14 days, index built 0 min ago):
- [claude · 2026-11-26T13:33] how to store memory of all sessions | …hot search works even without distillation… | ~/.claude/projects/…/<session>.jsonl
- [codex  · 2026-11-23T12:47] let's check how our RAG and brain search work | … | ~/.codex/sessions/2026/11/23/rollout-….jsonl
```

Measured on a real machine: 6,614 session files (4,457 Claude Code + 2,157 Codex) indexed in 21 s; incremental refresh 0.1 s.

## Idea in one minute

1. **One place.** Every agent already saves full transcripts on disk (`~/.claude/projects`, `~/.codex/sessions`, `~/.gemini/tmp`). Nothing to export. If you have several machines, sync those folders into one folder (Syncthing works well) and add it to `HOTMEM_ROOTS`.
2. **A hot window.** Only the last 14 days are indexed. That keeps the index small and fast, and it means you do not need perfect cleanup.
3. **Distillation is optional.** Distillation turns a long noisy session into a few clean sentences, so with thousands of sessions you get the one right answer instead of hundreds of near-misses. It is better, but it takes time and you will forget to run it. Inside a 14-day window you do not need it: the trash bin has not gone to the city dump yet, so you can still dig out the gold watch by hand. Distill later for long-term memory; search raw for the hot window.

## Install (2 minutes)

```bash
git clone https://github.com/tonydzi/hot-session-search ~/.claude/skills/hot-session-search
python3 ~/.claude/skills/hot-session-search/hotmem.py ingest
python3 ~/.claude/skills/hot-session-search/hotmem.py search "something you worked on this week"
```

Cloning into `~/.claude/skills/` also makes it a Claude Code skill (`SKILL.md`): the agent will search past sessions when you say "recall" or "did we already do this". For Codex, Cursor or other agents, point them at the same folder or add one line to `AGENTS.md`: *"Before building, run `python3 <path>/hotmem.py search "<topic>"` and read the top hits."*

## Keep it fresh (every 2 hours)

macOS / Linux, `crontab -e`:

```
0 */2 * * * /usr/bin/python3 $HOME/.claude/skills/hot-session-search/hotmem.py ingest >/dev/null 2>&1
```

Windows (PowerShell):

```powershell
schtasks /Create /SC HOURLY /MO 2 /TN "hotmem ingest" /TR "python %USERPROFILE%\.claude\skills\hot-session-search\hotmem.py ingest"
```

Every 2 hours is enough. More often only burns disk reads.

## Auto-search on the first prompt (Claude Code hook)

Add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command", "command": "python3 ~/.claude/skills/hot-session-search/hotmem.py hook" } ] }
    ]
  }
}
```

On the first real prompt of each session it injects the top 3 related past sessions into context, so the agent sees "this was already done" before it starts. It never blocks or breaks a prompt.

## Several machines, all harnesses, one search

```
laptop  ~/.claude/projects  ─┐
laptop  ~/.codex/sessions   ─┼─ Syncthing ─▶  ~/sessions-all/<machine>/...  ─▶  HOTMEM_ROOTS=~/sessions-all
desktop ~/.claude/projects  ─┘
```

```bash
export HOTMEM_ROOTS="$HOME/sessions-all"      # ';' separates folders on Windows, ':' on mac/linux
python3 hotmem.py ingest && python3 hotmem.py stats
```

## Settings

| env | default | meaning |
|---|---|---|
| `HOTMEM_DAYS` | `14` | window in days (by file modification time) |
| `HOTMEM_ROOTS` | none | extra folders to index |
| `HOTMEM_DB` | `~/.hotmem/hotmem.db` | index file; delete it to rebuild from scratch |

## Honest limits

- Keyword search with crude word-form matching (long words are matched by their first 5 letters, which helps Russian and other inflected languages). It is not semantic search.
- "Nothing found" means nothing in the indexed folders in the window, not that it never happened.
- The index stays on your disk. Obvious API keys are masked on ingest, but transcripts can contain private data: do not commit `hotmem.db`.
- Remove it: delete the folder, the cron line and `~/.hotmem`.

## How it is built

`hotmem.py` (~300 lines): walks the roots, parses each JSONL message (Claude Code, Codex rollout, Gemini chat, generic `role/content`), skips injected system instructions, stores messages in SQLite FTS5, ranks by BM25 with a gentle age decay, groups hits by session. This is a one-file distillation of a larger hot-memory engine that runs across an 11-machine agent fleet.

MIT. Built by Mycroft and Tony, Palo Alto AI Research Lab · github.com/tonydzi
