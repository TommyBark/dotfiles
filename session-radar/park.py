#!/usr/bin/env python3
"""Park / restore every Claude Code (and Codex) session in tmux across a reboot.

park     Gracefully exit each agent pane, record pane -> (cwd, session id) in
         a manifest, then force a tmux-resurrect save.
restore  Re-launch `cd <cwd> && claude --resume <id>` in each recorded pane.
         Wired to @resurrect-hook-post-restore-all, so it runs by itself after
         tmux-continuum restores the layout on boot.

Usage:
    park.py park [--session NAME] [--force] [--dry-run] [--no-save]
    park.py restore [--auto] [--dry-run]
    park.py status
"""

import argparse
import concurrent.futures
import datetime
import glob
import json
import os
import re
import shlex
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import session_radar as radar  # noqa: E402

MANIFEST = os.path.join(radar.RADAR_DIR, "parked.json")
RESURRECT_SAVE = os.path.expanduser("~/.tmux/plugins/tmux-resurrect/scripts/save.sh")
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
BANNER_RE = {"claude": re.compile(rf"claude --resume ({UUID})"),
             "codex": re.compile(rf"codex resume ({UUID})")}
# Claude's live spinner line looks like "✽ Seasoning… (2m 16s · ↓ 11.7k tokens)";
# finished turns say "Worked for 2m" with no ellipsis. Codex shows "esc to interrupt".
BUSY_RE = re.compile(r"…\s*\(\d+[hms]|esc to interrupt")
# Claude exits on a double Ctrl+D only when the prompt is empty, so an unsent
# draft keeps the session alive instead of being wiped. Ctrl+C clears drafts.
EXIT_KEYS = {"claude": ["C-d", "C-d"], "codex": ["C-c", "C-c"]}
FORCE_KEYS = ["C-c", "C-c", "C-c"]
SHELLS = {"zsh", "bash", "fish", "sh", "-zsh", "-bash"}


def tmux(*args):
    return radar.run(["tmux", *args])


def capture(pane, lines):
    return tmux("capture-pane", "-p", "-J", "-t", pane, "-S", f"-{lines}").stdout


def is_busy(s):
    if s["tool"] == "claude":
        status = claude_session_file(s["pid"]).get("status")
        if status:
            return status == "busy"
    tail = [l for l in capture(s["pane_id"], 40).splitlines() if l.strip()][-12:]
    return any(BUSY_RE.search(l) for l in tail)


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def own_ancestors():
    procs, _ = radar.process_table()
    pid, seen = os.getpid(), set()
    while pid and pid not in seen:
        seen.add(pid)
        pid = procs.get(pid, {}).get("ppid")
    return seen


# ------------------------------------------------------------ id / cwd lookup

CLAUDE_SESSIONS = os.path.expanduser("~/.claude/sessions")


def claude_session_file(pid):
    """Claude writes ~/.claude/sessions/<pid>.json with the live sessionId,
    cwd and status (idle/busy/waiting) - exact, unlike the radar heuristics."""
    try:
        with open(os.path.join(CLAUDE_SESSIONS, f"{pid}.json")) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def transcript_id(s):
    """Best pre-exit session id: Claude's pid file, else the located transcript."""
    if s.get("tool") == "claude":
        sid = claude_session_file(s["pid"]).get("sessionId")
        if sid:
            return sid
    t = s.get("transcript")
    if not t:
        return None
    m = re.search(rf"({UUID})\.jsonl$", t)
    return m.group(1) if m else None


def find_claude_transcript(sid):
    hits = glob.glob(os.path.join(radar.CLAUDE_PROJECTS, "*", f"{sid}.jsonl"))
    return hits[0] if hits else None


def transcript_cwd(path):
    """First cwd recorded in a Claude transcript (the dir it was launched in)."""
    with open(path, errors="replace") as f:
        for line in f:
            m = re.search(r'"cwd":"((?:[^"\\]|\\.)*)"', line)
            if m:
                return json.loads(f'"{m.group(1)}"')
    return None


def resume_cwd(tool, sid, proc_cwd):
    """Dir to run the resume from. Claude only finds a session from the dir
    whose munged path keys its transcript folder, so verify that and fall back
    to the cwd the transcript itself recorded."""
    if tool != "claude":
        return proc_cwd, None
    path = find_claude_transcript(sid)
    if not path:
        return proc_cwd, "transcript not found on disk"
    folder = os.path.dirname(path)
    if proc_cwd != "?" and folder in radar.candidate_dirs(proc_cwd):
        return proc_cwd, None
    tcwd = transcript_cwd(path)
    if tcwd and os.path.isdir(tcwd):
        return tcwd, f"using transcript cwd (process was in {proc_cwd})"
    return proc_cwd, "transcript lives under a different project dir"


# ------------------------------------------------------------------- park

def exit_pane(s, force):
    """Send exit keys, wait for the process to die, scrape the resume banner."""
    keys = EXIT_KEYS[s["tool"]] + (FORCE_KEYS if force else [])
    for key in keys:
        if not alive(s["pid"]):
            break
        tmux("send-keys", "-t", s["pane_id"], key)
        time.sleep(0.4)
    deadline = time.time() + 8
    while alive(s["pid"]) and time.time() < deadline:
        time.sleep(0.2)
    if alive(s["pid"]):
        return False, None
    time.sleep(0.5)  # let the exit banner and shell prompt land
    hits = BANNER_RE[s["tool"]].findall(capture(s["pane_id"], 15))
    return True, (hits[-1] if hits else None)


def tmp_worktree_warnings(cwds):
    """/tmp is wiped on reboot: flag agent-made worktrees there with work that
    exists nowhere else (uncommitted changes, or detached commits on no branch)."""
    warnings, repos = [], set()
    for cwd in cwds:
        r = radar.run(["git", "-C", cwd, "worktree", "list", "--porcelain"])
        if r.returncode != 0:
            continue
        paths = [l[9:] for l in r.stdout.splitlines() if l.startswith("worktree ")]
        if not paths or paths[0] in repos:
            continue
        repos.add(paths[0])
        for p in paths[1:]:
            if not p.startswith(("/tmp/", "/private/tmp/")) or not os.path.isdir(p):
                continue
            if radar.run(["git", "-C", p, "rev-parse", "--is-inside-work-tree"]).returncode:
                continue  # leftover dir of an already-removed worktree
            dirty = radar.run(["git", "-C", p, "status", "--porcelain"]).stdout
            if dirty.strip():
                n = len(dirty.splitlines())
                warnings.append(f"{p}: {n} uncommitted change(s)")
                continue
            head = radar.run(["git", "-C", p, "symbolic-ref", "-q", "HEAD"])
            if head.returncode != 0:
                on = radar.run(["git", "-C", p, "branch", "-a", "--contains", "HEAD"])
                if not on.stdout.strip():
                    warnings.append(f"{p}: detached HEAD with commits on no branch")
    return warnings


def cmd_park(args):
    sessions = radar.discover()
    if args.session:
        sessions = [s for s in sessions if s["tmux_session"] in args.session]
    if not sessions:
        print("No Claude/Codex panes found.")
        return
    mine = own_ancestors()

    plan = []
    for s in sessions:
        s["sid"] = transcript_id(s)
        s["exact"] = bool(claude_session_file(s["pid"]).get("sessionId"))
        if s["pid"] in mine:
            s["action"] = "self"
        elif is_busy(s) and not args.force:
            s["action"] = "busy"
        else:
            s["action"] = "exit"
        plan.append(s)

    if args.dry_run:
        for s in plan:
            print(f"{s['pane']:28} {s['tool']:6} {s['action']:5} {s['sid'] or '?':36}  {s['cwd']}")
        return

    todo = [s for s in plan if s["action"] == "exit"]
    print(f"Exiting {len(todo)} pane(s)...", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        results = dict(zip([s["pane_id"] for s in todo],
                           pool.map(lambda s: exit_pane(s, args.force), todo)))

    entries, problems = [], []
    for s in plan:
        exited, banner_id = results.get(s["pane_id"], (False, None))
        sid = banner_id or s["sid"]
        if s["action"] == "exit" and not exited:
            problems.append(f"{s['pane']}: still running (unsent draft or open dialog?) "
                            "- clear it and re-run, or use --force")
        elif s["action"] == "busy":
            problems.append(f"{s['pane']}: mid-turn, left running - re-run when idle, or --force")
        elif s["action"] == "self":
            problems.append(f"{s['pane']}: this is the session running park; exit it yourself")
        if not sid:
            problems.append(f"{s['pane']}: no session id found - NOT recorded")
            continue
        cwd, note = resume_cwd(s["tool"], sid, s["cwd"])
        if note:
            problems.append(f"{s['pane']}: {note}")
        entries.append({"pane": s["pane"], "tool": s["tool"], "session_id": sid,
                        "cwd": cwd, "exited": exited,
                        "id_source": "exit-banner" if banner_id
                        else "pid-file" if s["exact"]
                        else "guessed" if s.get("guessed") else "transcript"})

    # merge with a previous park (e.g. a second pass over panes that were busy)
    old = load_manifest() or {"entries": []}
    keep = {e["pane"]: e for e in old["entries"]}
    keep.update({e["pane"]: e for e in entries})
    manifest = {"parked_at": datetime.datetime.now().isoformat(timespec="seconds"),
                "entries": sorted(keep.values(), key=lambda e: e["pane"])}
    os.makedirs(radar.RADAR_DIR, exist_ok=True)
    with open(MANIFEST, "w") as f:
        json.dump(manifest, f, indent=2)

    for e in entries:
        mark = "✓" if e["exited"] else "·"
        print(f" {mark} {e['pane']:28} {e['tool']:6} {e['session_id']}  {e['cwd']}")
    print(f"\nRecorded {len(entries)} session(s) -> {MANIFEST}")

    warns = tmp_worktree_warnings({e["cwd"] for e in entries if os.path.isdir(e["cwd"])})
    if problems or warns:
        print("\nAttention:")
        for p in problems:
            print(f"  ! {p}")
        for w in warns:
            print(f"  ! /tmp worktree will be wiped on reboot - {w}")

    if not args.no_save and os.path.exists(RESURRECT_SAVE):
        radar.run([RESURRECT_SAVE, "quiet"])
        print("\ntmux-resurrect state saved. Safe to reboot.")


# ----------------------------------------------------------------- restore

def load_manifest():
    try:
        with open(MANIFEST) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def pane_command(target):
    r = tmux("display-message", "-p", "-t", target, "#{pane_current_command}")
    return r.stdout.strip() if r.returncode == 0 else None


def cmd_restore(args):
    manifest = load_manifest()
    if not manifest:
        if not args.auto:
            print(f"No parked sessions ({MANIFEST} missing).")
        return
    entries = manifest["entries"]
    if args.auto:
        # after a resurrect restore the panes are still cat-ing saved contents
        # and starting shells; wait until they all sit at a shell prompt
        deadline = time.time() + 30
        while time.time() < deadline:
            if all(pane_command(e["pane"]) in SHELLS for e in entries):
                break
            time.sleep(1)
        time.sleep(1.5)

    live_ids = {transcript_id(s) for s in radar.discover()} - {None}
    log, failed = [], []
    for e in entries:
        cmd = (f"cd {shlex.quote(e['cwd'])} && {e['tool']} "
               f"{'--resume' if e['tool'] == 'claude' else 'resume'} {e['session_id']}")
        current = pane_command(e["pane"])
        if current is None:
            failed.append(e)
            log.append(f" ✗ {e['pane']}: pane doesn't exist")
        elif current not in SHELLS:
            log.append(f" - {e['pane']}: busy running {current}, skipped")
        elif e["session_id"] in live_ids:
            log.append(f" - {e['pane']}: {e['session_id']} already open elsewhere, skipped")
        elif not os.path.isdir(e["cwd"]):
            failed.append(e)
            log.append(f" ✗ {e['pane']}: {e['cwd']} no longer exists")
        else:
            if not args.dry_run:
                tmux("send-keys", "-t", e["pane"], "-l", cmd)
                tmux("send-keys", "-t", e["pane"], "Enter")
            log.append(f" ✓ {e['pane']}: {cmd}")

    if not args.dry_run:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        done = os.path.join(radar.RADAR_DIR, f"parked-restored-{stamp}.json")
        os.replace(MANIFEST, done)
        with open(done + ".log", "w") as f:
            f.write("\n".join(log) + "\n")
        if args.auto:
            ok = sum(l.startswith(" ✓") for l in log)
            tmux("display-message", "-d", "8000",
                 f"Restored {ok}/{len(entries)} agent sessions"
                 + (f" ({len(failed)} failed, see {done}.log)" if failed else ""))
    print("\n".join(log))


def cmd_status(_args):
    manifest = load_manifest()
    if not manifest:
        print("Nothing parked.")
        return
    print(f"Parked at {manifest['parked_at']}:")
    for e in manifest["entries"]:
        print(f"  {e['pane']:28} {e['tool']:6} {e['session_id']}  {e['cwd']}  [{e['id_source']}]")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("park", help="exit all agent panes and record how to resume them")
    p.add_argument("--session", action="append", help="only this tmux session (repeatable)")
    p.add_argument("--force", action="store_true",
                   help="also exit busy panes and discard unsent drafts (Ctrl+C)")
    p.add_argument("--dry-run", action="store_true", help="show what would happen")
    p.add_argument("--no-save", action="store_true", help="skip the tmux-resurrect save")
    r = sub.add_parser("restore", help="relaunch parked sessions in their panes")
    r.add_argument("--auto", action="store_true",
                   help="hook mode: wait for restored shells, stay quiet if nothing parked")
    r.add_argument("--dry-run", action="store_true")
    sub.add_parser("status", help="show the parked manifest")
    args = ap.parse_args()
    {"park": cmd_park, "restore": cmd_restore, "status": cmd_status}[args.cmd](args)


if __name__ == "__main__":
    main()
