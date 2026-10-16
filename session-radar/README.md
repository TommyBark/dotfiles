# Session Radar

Summarizes every Claude Code **and Codex** session currently open in tmux: one
haiku summary per session, grouped into workstreams by sonnet, written to a
markdown report. Codex sessions are tagged `⬡ codex` (magenta in the picker).

Also renames tmux windows after each session's summary (≤16 chars, PR reviews
suffixed `_claude`/`_codex`) — but only windows with generic names (the CC
version number or `codex`) or names the radar assigned itself; manually named
windows are never touched (tracked in `renamed.json`). Disable with
`--no-rename`.

Codex discovery: codex holds its rollout (`~/.codex/sessions/.../rollout-*.jsonl`)
open, so `lsof` maps process → transcript exactly; both rollout formats
(item_completed and response_item) are parsed. Named claude sessions
(`--resume <name>`) resolve via a cached `"customTitle"` scan (`named.json`).

## Run

```bash
uv run python ~/claude/session-radar/session_radar.py
```

Report lands in `~/.claude/session-radar/latest.md` (plus a timestamped copy)
and is printed to stdout. Also available inside any Claude Code session as the
`/session-radar` skill.

Handy alias for your shell rc:

```bash
alias radar='uv run python ~/claude/session-radar/session_radar.py'
```

## How it works

1. `tmux list-panes -a` → walk each pane's process tree for a `claude` process.
2. Map process → transcript: Claude Code's own registry
   (`~/.claude/sessions/<pid>.json`) records each live process's session id,
   including sessions resumed through the `/resume` picker, so this is an
   exact match (guarded against pid reuse by start time). Fallbacks, in
   order: `--resume <id>` on the command line; transcript creation time vs
   process start time in `~/.claude/projects/<munged-cwd>/`; and last, pane
   activity correlation (marked "guessed" — never used for renaming).
3. Parse the transcript JSONL, build a digest (first user message + recent
   turns), summarize via `claude -p --model haiku`. Summaries are cached by
   transcript mtime+size in `~/.claude/session-radar/cache.json`, so repeat
   runs only pay for sessions that changed.
4. One sonnet call groups sessions into workstreams (shared PRs, branches,
   subsystems). `--no-group` falls back to grouping by project directory.

## Jumping between sessions

`--jump` opens an fzf picker over all live sessions (cached summaries in the
preview pane, most recent activity first); selecting one switches the tmux
client to that pane. Bound to **`prefix+g`** in `~/.tmux.conf` via
`display-popup`, so it works as an overlay from anywhere in tmux. Uses only
cached summaries — no LLM calls, instant. Sessions never summarized before
show up as "(not summarized yet)"; run the full radar to fill them in.

### Exporting the view for another agent

The same view is available as markdown, e.g. to paste into another Claude
session or attach to an issue:

```bash
uv run python ~/claude/session-radar/session_radar.py --export          # → ~/.claude/session-radar/jump.md
uv run python ~/claude/session-radar/session_radar.py --export -        # → stdout
uv run python ~/claude/session-radar/session_radar.py --export out.md   # → custom path
```

No LLM calls — it uses cached summaries, same as the picker. Opening the
picker (`prefix+g`) also refreshes `jump.md` automatically, and **`prefix+G`**
exports it directly and copies the path to the clipboard.

## Automatic refresh

A launchd agent (`~/Library/LaunchAgents/com.tomas.session-radar.plist`)
runs every 15 minutes, 09:00–23:45 **system time**:

- **on the hour** — full radar: summaries, grouping, report refresh
- **:15 / :30 / :45** — `--rename-only`: labels windows that still carry a
  generic name (new sessions get one haiku call each; everything else is
  cache hits, ~1s)

Runs land in `~/.claude/session-radar/auto-run.log`. Unchanged sessions cost
nothing (summary cache), and when *no* session changed the Sonnet grouping
call is skipped too (grouping state in `state.json`).

Manage it with:

```bash
launchctl kickstart gui/$UID/com.tomas.session-radar   # run now
launchctl bootout   gui/$UID/com.tomas.session-radar   # disable
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.tomas.session-radar.plist  # re-enable
```

To change the schedule, edit the `StartCalendarInterval` array in the plist
and re-run bootout + bootstrap.

The plist is kept in this repo at `launchd/com.tomas.session-radar.plist` but is
not installed by `install.sh` (macOS-only, opt-in). To enable it on a new Mac:

```bash
cp ~/claude/session-radar/launchd/com.tomas.session-radar.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.tomas.session-radar.plist
```

It hard-codes `/Users/tomas` and `/opt/homebrew/bin/uv`; adjust if those differ.

## Flags

`--list` (discovery only, no LLM), `--jump` (fzf switcher), `--no-group`,
`--model`, `--group-model`, `--jobs`, `--digest-tokens`, `--output`.

## Park / restore across a reboot (`park.py`)

`prefix+P` (or `uv run python park.py park`) gracefully exits every Claude/Codex
pane and writes `~/.claude/session-radar/parked.json` (pane → cwd, session id),
then forces a tmux-resurrect save. After reboot, continuum restores the layout
and `@resurrect-hook-post-restore-all` runs `park.py restore --auto`, which types
`cd <cwd> && claude --resume <id>` into each pane. The manifest is consumed
(renamed to `parked-restored-*.json` + `.log`) so it never relaunches twice.

- Session ids come from the exit banner, else `~/.claude/sessions/<pid>.json`
  (exact), else the radar heuristics. The cwd is the Claude process's own
  cwd, so worktree sessions resume inside their worktree even if the shell
  sat elsewhere.
- Claude is exited with a double Ctrl+D, which only works on an empty prompt:
  panes with an unsent draft, mid-turn panes, and the session running park are
  left alone and listed. `--force` uses Ctrl+C (interrupts, discards drafts).
- Warns about worktrees under `/tmp` (wiped on reboot) with uncommitted changes.
- Re-running `park` merges into the manifest, so you can clear busy panes and go again.

`park.py park --dry-run`, `park.py status`, `park.py restore [--dry-run]`,
`park --session NAME` to limit to one tmux session.
