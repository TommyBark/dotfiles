---
name: session-radar
description: Summarize all Claude Code sessions currently open in tmux — one cheap-model summary per session, grouped by workstream, written to a markdown report. Use when the user invokes /session-radar, asks "what are all my sessions doing", wants an overview of open Claude Code sessions, or has lost track of their tmux panes.
---

# Session Radar

Discovers every `claude` process in tmux, maps each to its transcript in
`~/.claude/projects/`, summarizes each conversation with haiku (cached — only
changed sessions are re-summarized), groups related sessions into workstreams
with sonnet, and writes a markdown overview.

## Quick start

Run the tool and show the user the result:

```bash
uv run python ~/claude/session-radar/session_radar.py
```

- Report is written to `~/.claude/session-radar/latest.md` (plus a timestamped
  copy) and printed to stdout.
- A fresh run over N never-seen sessions makes N haiku calls + 1 sonnet call
  (~1 min for ~10 sessions); repeat runs are fast thanks to the cache.

After it finishes, present the report content directly to the user (don't just
point at the file). Keep the `tmux switch-client -t '...'` jump commands — the
user uses those to hop to a session.

## Useful flags

| Flag | Effect |
|------|--------|
| `--list` | Just list discovered sessions and transcript paths, no LLM calls |
| `--jump` | Interactive fzf session switcher (also bound to `prefix+g` in tmux) — needs a TTY, so don't run it from inside Claude Code |
| `--export [PATH]` | Write the `prefix+g` view as markdown (cached summaries, no LLM calls) to PATH, default `~/.claude/session-radar/jump.md`; `-` for stdout. Use this to hand the current session landscape to another agent |
| `--digest-tokens N` | Per-session token budget for summarization (default 50000) |
| `--no-group` | Skip the LLM grouping pass, group by project dir instead |
| `--model X` | Summarizer model (default `haiku`) |
| `--group-model X` | Grouping model (default `sonnet`) |
| `--jobs N` | Parallel summarizer calls (default 8) |

## Troubleshooting

- "tmux not available" — no tmux server running; sessions must live in tmux.
- A pane listed under "Unmapped claude processes" — its transcript couldn't be
  located (rare; happens if cwd changed since launch). `--list` shows what was
  found.
- "(transcript guessed)" marker — should be rare now: transcripts resolve
  exactly via `~/.claude/sessions/<pid>.json`. A guess means that registry
  entry was missing or stale, and the pane was matched by activity instead.
  Guessed panes are never renamed.
