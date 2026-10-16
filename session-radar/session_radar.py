#!/usr/bin/env python3
"""Session Radar: summarize all Claude Code sessions running in tmux.

Discovers every `claude` process inside tmux panes, finds each one's
transcript (~/.claude/projects/<munged-cwd>/<session-id>.jsonl), summarizes
each conversation with a cheap model via `claude -p`, groups related
sessions, and writes a markdown overview.

Usage:
    session_radar.py                 # full run, report to ~/.claude/session-radar/
    session_radar.py --model haiku   # summarizer model (default: haiku)
    session_radar.py --no-group      # skip the grouping pass
    session_radar.py --list          # just list discovered sessions, no LLM calls
"""

import argparse
import concurrent.futures
import datetime
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys

CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")
RADAR_DIR = os.path.expanduser("~/.claude/session-radar")
DIGEST_TOKENS = 50_000        # per-session token budget for the summarizer
CHARS_PER_TOKEN = 4           # rough ratio for English/code mix
DIGEST_CHARS = DIGEST_TOKENS * CHARS_PER_TOKEN
FIRST_MSG_CHARS = 8_000       # cap on the opening user message
TURN_CHARS = 10_000           # cap on each individual turn


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


# ---------------------------------------------------------------- discovery

def tmux_panes():
    fmt = ("#{session_name}\t#{window_index}\t#{pane_index}\t#{pane_pid}"
           "\t#{pane_id}\t#{window_activity}\t#{window_name}")
    r = run(["tmux", "list-panes", "-a", "-F", fmt])
    if r.returncode != 0:
        sys.exit(f"tmux not available or no server running: {r.stderr.strip()}")
    panes = []
    for line in r.stdout.splitlines():
        sess, win, pane, pid, pane_id, activity, wname = line.split("\t", 6)
        panes.append({"target": f"{sess}:{win}.{pane}", "session": sess,
                      "window_name": wname, "pid": int(pid),
                      "pane_id": pane_id, "activity": int(activity or 0)})
    return panes


def process_table():
    r = run(["ps", "-axo", "pid=,ppid=,lstart=,command="])
    procs, children = {}, {}
    pat = re.compile(r"^\s*(\d+)\s+(\d+)\s+(\w{3} \w{3} [ \d]\d [\d:]{8} \d{4})\s+(.*)$")
    for line in r.stdout.splitlines():
        m = pat.match(line)
        if not m:
            continue
        pid, ppid = int(m.group(1)), int(m.group(2))
        started = datetime.datetime.strptime(m.group(3), "%a %b %d %H:%M:%S %Y")
        procs[pid] = {"ppid": ppid, "started": started, "command": m.group(4)}
        children.setdefault(ppid, []).append(pid)
    return procs, children


AGENT_TOOLS = ("claude", "codex")


def agent_tool(command):
    argv0 = command.split()[0] if command.split() else ""
    base = os.path.basename(argv0)
    return base if base in AGENT_TOOLS else None


def find_agent_descendant(root_pid, procs, children):
    """BFS the process tree under a pane pid for a claude/codex process."""
    queue = [root_pid]
    while queue:
        pid = queue.pop(0)
        info = procs.get(pid)
        if info:
            tool = agent_tool(info["command"])
            if tool:
                return pid, info, tool
        queue.extend(children.get(pid, []))
    return None, None, None


def parse_lsof_names(out):
    """Parse `lsof -Fn` output into {pid: [paths]}."""
    result, pid = {}, None
    for line in out.splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
            result[pid] = []
        elif line.startswith("n") and pid is not None:
            result[pid].append(line[1:])
    return result


def batch_cwds(pids):
    """One lsof call for all pids -> {pid: cwd}."""
    if not pids:
        return {}
    r = run(["lsof", "-a", "-p", ",".join(map(str, pids)), "-d", "cwd", "-Fn"])
    return {pid: paths[0] for pid, paths in parse_lsof_names(r.stdout).items() if paths}


def batch_open_rollouts(pids):
    """One lsof call for codex pids -> {pid: open rollout jsonl path}."""
    if not pids:
        return {}
    r = run(["lsof", "-a", "-p", ",".join(map(str, pids)), "-Fn"])
    rollouts = {}
    for pid, paths in parse_lsof_names(r.stdout).items():
        for p in paths:
            if "/.codex/sessions/" in p and p.endswith(".jsonl"):
                rollouts[pid] = p
                break
    return rollouts


def munge(path):
    return re.sub(r"[^A-Za-z0-9]", "-", path)


UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def candidate_dirs(cwd):
    """Transcript dirs for a session cwd. --worktree sessions store their
    transcript under the main repo's dir, not the worktree's."""
    paths = [cwd]
    if "/.claude/worktrees/" in cwd:
        paths.append(cwd.split("/.claude/worktrees/")[0])
    dirs = [os.path.join(CLAUDE_PROJECTS, munge(p)) for p in paths]
    return [d for d in dirs if os.path.isdir(d)]


NAMED_CACHE = os.path.join(RADAR_DIR, "named.json")


def find_named_session(name, proj_dir, claimed):
    """Resolve a `--resume <name>` (non-UUID) session via its custom-title
    record. The full project-dir scan is slow (can be hundreds of MB), so
    resolved names are cached and only re-verified against the single file."""
    pattern = f'"customTitle":"{name}"'
    key = f"{proj_dir}|{name}"
    try:
        with open(NAMED_CACHE) as f:
            named = json.load(f)
    except (OSError, json.JSONDecodeError):
        named = {}
    p = named.get(key)
    if p and p not in claimed and os.path.isfile(p):
        if run(["grep", "-q", "-F", pattern, p]).returncode == 0:
            return p
    r = run(["grep", "-l", "-F", pattern, "--include=*.jsonl", "-r", proj_dir])
    hits = [h for h in r.stdout.splitlines() if h not in claimed]
    if hits:
        best = max(hits, key=lambda h: os.stat(h).st_mtime)
        named[key] = best
        os.makedirs(RADAR_DIR, exist_ok=True)
        with open(NAMED_CACHE, "w") as f:
            json.dump(named, f)
        return best
    return None


SESSIONS_REGISTRY = os.path.expanduser("~/.claude/sessions")


def locate_by_registry(pid, started, cwd, claimed):
    """Exact match via Claude Code's own per-process registry:
    ~/.claude/sessions/<pid>.json records the live sessionId (kept current
    across /resume), so picker-resumed sessions resolve without guessing.
    Guards against pid reuse by requiring the recorded start time to agree
    with the process start time."""
    try:
        with open(os.path.join(SESSIONS_REGISTRY, f"{pid}.json")) as f:
            reg = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    sid = reg.get("sessionId")
    if not sid or not UUID_RE.match(sid):
        return None
    started_at = reg.get("startedAt")
    if started_at and abs(started_at / 1000 - started.timestamp()) > 120:
        return None  # stale entry from a previous process with this pid
    dirs = candidate_dirs(cwd)
    reg_cwd = reg.get("cwd")
    if reg_cwd and reg_cwd != cwd:
        dirs = candidate_dirs(reg_cwd) + dirs
    for d in dirs:
        path = os.path.join(d, f"{sid}.jsonl")
        if os.path.isfile(path) and path not in claimed:
            return path
    return None


def locate_transcript_exact(cwd, command, claimed):
    """Resolve via --resume <uuid> or --resume <name>; None if not resumable."""
    dirs = candidate_dirs(cwd)
    m = re.search(r"--resume\s+(\S+)", command)
    if not m:
        return None
    ref = m.group(1)
    if UUID_RE.match(ref):
        for d in dirs:
            p = os.path.join(d, f"{ref}.jsonl")
            if os.path.isfile(p):
                return p
    else:  # resumed by session name
        for d in dirs:
            p = find_named_session(ref, d, claimed)
            if p:
                return p
    return None


def locate_by_birthtime(cwd, started, claimed):
    """Confident heuristic: transcript created within minutes of proc start."""
    start_ts = started.timestamp()
    best, best_delta = None, None
    for d in candidate_dirs(cwd):
        for name in os.listdir(d):
            if not name.endswith(".jsonl"):
                continue
            p = os.path.join(d, name)
            if p in claimed:
                continue
            st = os.stat(p)
            birth = getattr(st, "st_birthtime", st.st_mtime)
            delta = abs(birth - start_ts)
            if delta < 300 and (best_delta is None or delta < best_delta):
                best, best_delta = p, delta
    return best


def locate_by_activity(cwd, activity, claimed):
    """Last-resort guess: the transcript whose last write lines up with the
    pane's last activity. Unlike "most recently modified", this cannot match
    a transcript of a closed session that was busy more recently than this
    pane, nor an idle one while this pane is active."""
    if not activity:
        return None
    best, best_delta = None, None
    for d in candidate_dirs(cwd):
        for name in os.listdir(d):
            if not name.endswith(".jsonl"):
                continue
            p = os.path.join(d, name)
            if p in claimed:
                continue
            delta = abs(os.stat(p).st_mtime - activity)
            if delta <= 120 and (best_delta is None or delta < best_delta):
                best, best_delta = p, delta
    return best


CODEX_SESSIONS = os.path.expanduser("~/.codex/sessions")


def locate_codex_exact(pid, command, rollouts, claimed):
    """Codex holds its rollout open (lsof), or names it via `resume <uuid>`."""
    p = rollouts.get(pid)
    if p and p not in claimed and os.path.isfile(p):
        return p
    m = re.search(r"resume\s+([0-9a-f-]{36})", command)
    if m:
        hits = [h for h in glob.glob(f"{CODEX_SESSIONS}/*/*/*/rollout-*-{m.group(1)}.jsonl")
                if h not in claimed]
        if hits:
            return max(hits, key=lambda h: os.stat(h).st_mtime)
    return None


def locate_codex_fallback(started, claimed):
    start_ts = started.timestamp()
    best, best_delta = None, None
    for h in glob.glob(f"{CODEX_SESSIONS}/*/*/*/rollout-*.jsonl"):
        if h in claimed:
            continue
        st = os.stat(h)
        birth = getattr(st, "st_birthtime", st.st_mtime)
        delta = abs(birth - start_ts)
        if delta < 120 and (best_delta is None or delta < best_delta):
            best, best_delta = h, delta
    return best


PANES_STATE = os.path.join(RADAR_DIR, "panes.json")


def discover():
    panes = tmux_panes()
    procs, children = process_table()
    found = []
    for pane in panes:
        pid, info, tool = find_agent_descendant(pane["pid"], procs, children)
        if pid:
            found.append((pane, pid, info, tool))
    cwds = batch_cwds([pid for _, pid, _, _ in found])
    rollouts = batch_open_rollouts([pid for _, pid, _, t in found if t == "codex"])
    try:
        with open(PANES_STATE) as f:
            pane_map = json.load(f)
    except (OSError, json.JSONDecodeError):
        pane_map = {}

    sessions, claimed = [], set()
    for pane, pid, info, tool in found:
        sessions.append({
            "pane": pane["target"], "tmux_session": pane["session"],
            "window_name": pane["window_name"], "pid": pid,
            "pane_id": pane["pane_id"], "activity": pane["activity"],
            "cwd": cwds.get(pid, "?"), "tool": tool,
            "started": info["started"].isoformat(sep=" "),
            "start_ts": info["started"], "command": info["command"],
            "transcript": None, "guessed": False,
        })
    # pass 1: exact matches (CC session registry, resume id/name, codex lsof)
    for s in sessions:
        if s["tool"] == "codex":
            s["transcript"] = locate_codex_exact(s["pid"], s["command"], rollouts, claimed)
        else:
            s["transcript"] = (locate_by_registry(s["pid"], s["start_ts"], s["cwd"], claimed)
                               or locate_transcript_exact(s["cwd"], s["command"], claimed))
        if s["transcript"]:
            claimed.add(s["transcript"])
    # pass 2: previously pinned pane -> transcript mappings. Pinned matches
    # survive claude restarts/updates, which break the start-time heuristics.
    for s in sessions:
        if s["transcript"]:
            continue
        pin = pane_map.get(s["pane_id"])
        if not pin:
            continue
        p = pin.get("transcript")
        if not p or p in claimed or not os.path.isfile(p):
            continue
        same_proc = pin.get("proc_started") == s["started"]
        active_since_restart = os.stat(p).st_mtime >= s["start_ts"].timestamp() - 60
        if same_proc or active_since_restart:
            s["transcript"] = p
            claimed.add(p)
    # pass 3: confident heuristic — transcript created at proc start
    for s in sessions:
        if s["transcript"]:
            continue
        if s["tool"] == "codex":
            s["transcript"] = locate_codex_fallback(s["start_ts"], claimed)
        else:
            s["transcript"] = locate_by_birthtime(s["cwd"], s["start_ts"], claimed)
        if s["transcript"]:
            claimed.add(s["transcript"])
    # pin everything confidently matched so far
    for s in sessions:
        if s["transcript"]:
            pane_map[s["pane_id"]] = {"transcript": s["transcript"],
                                      "proc_started": s["started"]}
    # pass 4: last-resort guess via pane-activity correlation (never pinned,
    # never used for renaming)
    for s in sessions:
        if s["transcript"] or s["tool"] == "codex":
            continue
        s["transcript"] = locate_by_activity(s["cwd"], s["activity"], claimed)
        if s["transcript"]:
            claimed.add(s["transcript"])
            s["guessed"] = True
    # drop pins for panes that no longer exist
    live = {s["pane_id"] for s in sessions}
    pane_map = {k: v for k, v in pane_map.items() if k in live}
    os.makedirs(RADAR_DIR, exist_ok=True)
    with open(PANES_STATE, "w") as f:
        json.dump(pane_map, f)
    for s in sessions:
        del s["start_ts"]
    return sessions


# ---------------------------------------------------------------- transcript

def extract_text(content):
    """Pull human-readable text out of a message content field."""
    if isinstance(content, str):
        return content
    parts = []
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif item.get("type") == "tool_use":
                parts.append(f"[tool: {item.get('name', '?')}]")
    return "\n".join(parts)


NOISE = re.compile(r"^\s*(<command-|<local-command|<system-reminder|Caveat:)")


def read_transcript(path):
    """Return (turns, meta). turns = [(role, text)], newest last."""
    turns, meta = [], {"branch": None, "summary": None, "last_ts": None}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") == "summary":
                    meta["summary"] = rec.get("summary")
                    continue
                if rec.get("gitBranch"):
                    meta["branch"] = rec["gitBranch"]
                if rec.get("timestamp"):
                    meta["last_ts"] = rec["timestamp"]
                if rec.get("isMeta"):
                    continue
                if rec.get("type") not in ("user", "assistant"):
                    continue
                msg = rec.get("message") or {}
                content = msg.get("content")
                if rec.get("type") == "user" and isinstance(content, list) and \
                        any(isinstance(i, dict) and i.get("type") == "tool_result" for i in content):
                    continue
                text = extract_text(content).strip()
                if not text or NOISE.match(text):
                    continue
                turns.append((rec["type"], text))
    except OSError:
        pass
    return turns, meta


def read_codex_transcript(path):
    """Parse a codex rollout jsonl (both the item_completed and the older
    response_item/agent_message formats). Returns (turns, meta)."""
    turns, meta = [], {"branch": None, "summary": None, "last_ts": None}
    seen = set()
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("timestamp"):
                    meta["last_ts"] = rec["timestamp"]
                payload = rec.get("payload") or {}
                if rec.get("type") == "session_meta":
                    git = payload.get("git") or {}
                    meta["branch"] = git.get("branch")
                    continue
                role, text = None, None
                rtype, ptype = rec.get("type"), payload.get("type")
                if rtype == "event_msg" and ptype == "item_completed":
                    item = payload.get("item") or {}
                    role = {"UserMessage": "user", "AgentMessage": "assistant"}.get(item.get("type"))
                    text = "\n".join(c.get("text", "") for c in item.get("content", [])
                                     if isinstance(c, dict))
                elif rtype == "event_msg" and ptype in ("agent_message", "user_message"):
                    role = "assistant" if ptype == "agent_message" else "user"
                    text = payload.get("message") or ""
                elif rtype == "response_item" and ptype == "message":
                    role = payload.get("role")
                    if role not in ("user", "assistant"):
                        continue  # developer/system context injections
                    text = "\n".join(c.get("text", "") for c in payload.get("content", [])
                                     if isinstance(c, dict))
                if not role or not text:
                    continue
                text = text.strip()
                if not text or text.startswith("<"):  # <permissions...>, <environment_context>...
                    continue
                if (role, text) not in seen:
                    seen.add((role, text))
                    turns.append((role, text))
    except OSError:
        pass
    return turns, meta


def read_any_transcript(path, tool):
    return read_codex_transcript(path) if tool == "codex" else read_transcript(path)


def build_digest(turns, digest_chars=None):
    if not turns:
        return ""
    digest_chars = digest_chars or DIGEST_CHARS
    first = f"FIRST USER MESSAGE:\n{turns[0][1][:FIRST_MSG_CHARS]}\n\n"
    recent, budget = [], digest_chars - len(first)
    for role, text in reversed(turns[1:]):
        chunk = f"{role.upper()}: {text[:TURN_CHARS]}\n\n"
        if budget - len(chunk) < 0:
            break
        recent.append(chunk)
        budget -= len(chunk)
    return first + "RECENT CONVERSATION (newest last):\n" + "".join(reversed(recent))


# ---------------------------------------------------------------- LLM calls

SUMMARY_PROMPT = """You are given a digest of a Claude Code coding session.
Reply with ONLY a JSON object, no markdown fence, with these keys:
  "title": short label for the session, max 8 words
  "feature": the feature/workstream this belongs to, 1-4 words (sessions on the same feature should get the same string)
  "status": one of "in-progress", "waiting-for-user", "idle-done", "blocked"
  "summary": 2-3 sentences: what the session is doing and where it stands
  "next": what the user should do next in this session, one short sentence
  "tab": ultra-short label for a terminal tab, MAX 16 characters, e.g. "overlay-bloat"
  "is_pr_review": true if the session's main task is reviewing a pull request, else false
    (when true, keep "tab" to MAX 9 characters, e.g. "pr1209" — a suffix gets appended)

Digest:
"""

GROUP_PROMPT = """Below is a JSON list of active Claude Code sessions with titles and summaries.
Group them into workstreams: sessions working on the same feature/topic belong together.
Strong signals that sessions belong together: they mention the same PR numbers,
the same git branch, the same subsystem/module names, or one session reviews or
tests what another implements. Prefer fewer, larger groups over many singletons,
but do not force unrelated work together. Singletons are fine when genuinely
unrelated. Reply with ONLY a JSON object:
  {"groups": [{"name": "<workstream name, max 6 words>", "members": [<id>, ...]}]}
Every session id must appear in exactly one group.

Sessions:
"""


def parse_json_reply(text):
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def call_claude(prompt, model, workdir):
    r = subprocess.run(
        ["claude", "-p", "--model", model],
        input=prompt, capture_output=True, text=True, cwd=workdir, timeout=300,
    )
    return r.stdout if r.returncode == 0 else None


def cache_key(path):
    st = os.stat(path)
    return f"{st.st_mtime_ns}:{st.st_size}"


def summarize_session(sess, model, workdir, cache, digest_chars):
    turns, meta = read_any_transcript(sess["transcript"], sess.get("tool"))
    sess["branch"] = meta["branch"]
    sess["last_ts"] = meta["last_ts"]
    key = cache_key(sess["transcript"])
    hit = cache.get(sess["transcript"])
    if hit and hit.get("key") == key and hit.get("model") == model \
            and hit.get("digest") == digest_chars and "tab" in hit.get("summary", {}):
        sess["summary"] = hit["summary"]
        sess["cached"] = True
        return sess
    digest = build_digest(turns, digest_chars)
    if not digest:
        sess["summary"] = {"title": meta["summary"] or "(empty session)",
                           "feature": "unknown", "status": "idle-done",
                           "summary": "No conversation content found.", "next": ""}
        return sess
    reply = call_claude(SUMMARY_PROMPT + digest, model, workdir)
    parsed = parse_json_reply(reply) if reply else None
    if not parsed:
        if hit and hit.get("summary"):
            parsed = hit["summary"]  # summarizer failed — keep the stale summary
        else:
            parsed = {"title": meta["summary"] or "(summary failed)", "feature": "unknown",
                      "status": "?", "summary": "Summarizer call failed.", "next": ""}
    else:
        cache[sess["transcript"]] = {"key": key, "model": model,
                                     "digest": digest_chars, "summary": parsed}
    sess["summary"] = parsed
    return sess


def group_sessions(sessions, model, workdir):
    payload = [{"id": i, "project": os.path.basename(s["cwd"]),
                "branch": s.get("branch"),
                "title": s["summary"]["title"], "feature": s["summary"]["feature"],
                "summary": s["summary"]["summary"]}
               for i, s in enumerate(sessions)]
    reply = call_claude(GROUP_PROMPT + json.dumps(payload, indent=1), model, workdir)
    parsed = parse_json_reply(reply) if reply else None
    if parsed and isinstance(parsed.get("groups"), list):
        seen, groups = set(), []
        for g in parsed["groups"]:
            members = [i for i in g.get("members", [])
                       if isinstance(i, int) and 0 <= i < len(sessions) and i not in seen]
            seen.update(members)
            if members:
                groups.append({"name": g.get("name", "Ungrouped"), "members": members})
        leftovers = [i for i in range(len(sessions)) if i not in seen]
        if leftovers:
            groups.append({"name": "Ungrouped", "members": leftovers})
        return groups
    # fallback: group by project directory
    by_proj = {}
    for i, s in enumerate(sessions):
        by_proj.setdefault(os.path.basename(s["cwd"]), []).append(i)
    return [{"name": k, "members": v} for k, v in by_proj.items()]


GROUPING_THRESHOLD = 4  # tmux sessions with this many claude sessions or fewer
                        # become a single group named after the tmux session


def compute_groups(sessions, model, workdir):
    """Group per tmux session: small ones (≤ GROUPING_THRESHOLD) collapse into
    one group named after the tmux session; larger ones get LLM workstream
    grouping run on just their members. Groups never span tmux sessions."""
    by_tmux = {}
    for i, s in enumerate(sessions):
        by_tmux.setdefault(s["tmux_session"], []).append(i)
    groups = []
    for tmux_name, idxs in by_tmux.items():
        if len(idxs) <= GROUPING_THRESHOLD:
            groups.append({"name": tmux_name, "members": idxs})
        else:
            sub = group_sessions([sessions[i] for i in idxs], model, workdir)
            groups += [{"name": g["name"], "members": [idxs[j] for j in g["members"]]}
                       for g in sub]
    return groups


GENERIC_NAME_RE = re.compile(r"\d+\.\d+\.\d+")  # claude code panes show the CC version
RENAMED_STATE = os.path.join(RADAR_DIR, "renamed.json")


def is_generic_window_name(name):
    return bool(GENERIC_NAME_RE.fullmatch(name)) or name in ("codex", "claude")


def is_pr_review(sm):
    v = sm.get("is_pr_review")
    if v is not None:
        return bool(v)
    text = f"{sm.get('title', '')} {sm.get('tab', '')}".lower()
    return "review" in text and bool(re.search(r"\bpr\b|#\d+|pull request", text))


def rename_windows(sessions):
    """Rename tmux windows after their session summaries — but only windows
    that carry a generic name (CC version number / "codex") or a name this
    tool assigned previously. User-chosen names are never touched."""
    r = run(["tmux", "list-windows", "-a", "-F", "#{session_name}:#{window_index}\t#{window_name}"])
    current = dict(line.split("\t", 1) for line in r.stdout.splitlines() if "\t" in line)
    try:
        with open(RENAMED_STATE) as f:
            assigned = json.load(f)
    except (OSError, json.JSONDecodeError):
        assigned = {}

    def mtime(s):
        try:
            return os.stat(s["transcript"]).st_mtime if s["transcript"] else 0
        except OSError:
            return 0

    best = {}
    for s in sessions:
        if not s.get("summary") or s.get("guessed"):
            continue  # never rename off a guessed transcript mapping
        win = s["pane"].rsplit(".", 1)[0]
        if win not in best or mtime(s) > mtime(best[win]):
            best[win] = s
    renamed = 0
    for win, s in best.items():
        cur = current.get(win)
        if cur is None or not (is_generic_window_name(cur) or assigned.get(win) == cur):
            continue  # user-named window — hands off
        sm = s["summary"]
        name = re.sub(r"\s+", " ", (sm.get("tab") or sm.get("title") or "")).strip()
        if not name or name.startswith("("):
            continue
        if is_pr_review(sm):
            suffix = f"_{s.get('tool', 'claude')}"
            name = name[:16 - len(suffix)].rstrip(" _-") + suffix
        name = name[:16].strip()
        if cur != name:
            run(["tmux", "rename-window", "-t", win, name])
            renamed += 1
        assigned[win] = name
    with open(RENAMED_STATE, "w") as f:
        json.dump(assigned, f)
    return renamed


def rename_only(args):
    """Cheap 15-minute pass: name windows that are still generic-labeled.
    Uses cached summaries (stale is fine — the hourly run refreshes them);
    only sessions with no cache entry at all get a fresh haiku call."""
    workdir = os.path.join(RADAR_DIR, "workdir")
    os.makedirs(workdir, exist_ok=True)
    sessions = [s for s in discover() if s["transcript"]]
    r = run(["tmux", "list-windows", "-a", "-F", "#{session_name}:#{window_index}\t#{window_name}"])
    current = dict(line.split("\t", 1) for line in r.stdout.splitlines() if "\t" in line)
    try:
        with open(RENAMED_STATE) as f:
            assigned = json.load(f)
    except (OSError, json.JSONDecodeError):
        assigned = {}
    candidates = []
    for s in sessions:
        if s.get("guessed"):
            continue  # don't summarize/rename off a guessed mapping
        win = s["pane"].rsplit(".", 1)[0]
        cur = current.get(win)
        if cur is not None and (is_generic_window_name(cur) or assigned.get(win) == cur):
            candidates.append(s)
    if not candidates:
        print("rename-only: nothing to do", file=sys.stderr)
        return
    cache_path = os.path.join(RADAR_DIR, "cache.json")
    try:
        with open(cache_path) as f:
            cache = json.load(f)
    except (OSError, json.JSONDecodeError):
        cache = {}
    fresh = 0
    for s in candidates:
        hit = cache.get(s["transcript"])
        if hit:
            s["summary"] = hit["summary"]  # possibly stale — fine for a tab name
        else:
            summarize_session(s, args.model, workdir, cache,
                              args.digest_tokens * CHARS_PER_TOKEN)
            fresh += 1
    if fresh:
        with open(cache_path, "w") as f:
            json.dump(cache, f)
    n = rename_windows(candidates)
    print(f"rename-only: {len(candidates)} candidate(s), {fresh} summarized fresh, "
          f"{n} renamed", file=sys.stderr)


# ---------------------------------------------------------------- report

STATUS_ICON = {"in-progress": "🔄", "waiting-for-user": "🙋", "idle-done": "✅",
               "blocked": "⛔", "?": "❓"}


def age_str(iso_ts):
    if not iso_ts:
        return "?"
    try:
        ts = datetime.datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        delta = datetime.datetime.now(datetime.timezone.utc) - ts
        mins = int(delta.total_seconds() // 60)
        if mins < 60:
            return f"{mins}m ago"
        if mins < 60 * 24:
            return f"{mins // 60}h {mins % 60}m ago"
        return f"{mins // (60 * 24)}d ago"
    except ValueError:
        return "?"


def render(sessions, groups, started_at):
    lines = [f"# Claude Code Session Radar", "",
             f"*{started_at:%Y-%m-%d %H:%M}* — {len(sessions)} active session(s) in tmux", ""]
    lines.append("| | Session | Workstream | Pane | Last activity |")
    lines.append("|---|---|---|---|---|")
    for g in groups:
        for i in g["members"]:
            s = sessions[i]
            sm = s["summary"]
            icon = STATUS_ICON.get(sm.get("status", "?"), "❓")
            badge = " `⬡ codex`" if s.get("tool") == "codex" else ""
            lines.append(f"| {icon} | {sm['title']}{badge} | {g['name']} | "
                         f"`{s['pane']}` | {age_str(s.get('last_ts'))} |")
    counts = {}
    for s in sessions:
        st = s["summary"].get("status", "?")
        counts[st] = counts.get(st, 0) + 1
    legend = " · ".join(f"{STATUS_ICON.get(k, '❓')} {k}: {v}" for k, v in sorted(counts.items()))
    lines += ["", legend, ""]
    for g in groups:
        lines.append(f"## {g['name']}")
        lines.append("")
        for i in g["members"]:
            s = sessions[i]
            sm = s["summary"]
            icon = STATUS_ICON.get(sm.get("status", "?"), "❓")
            branch = f" · `{s['branch']}`" if s.get("branch") else ""
            guess = " *(transcript guessed)*" if s.get("guessed") else ""
            badge = " · `⬡ codex`" if s.get("tool") == "codex" else ""
            lines.append(f"### {icon} {sm['title']}{badge}  —  `{s['pane']}`")
            lines.append(f"*{s['cwd']}*{branch} · last activity {age_str(s.get('last_ts'))}{guess}")
            lines.append("")
            lines.append(sm.get("summary", ""))
            if sm.get("next"):
                lines.append(f"\n**Next:** {sm['next']}")
            lines.append(f"\nJump: `tmux switch-client -t '{s['pane']}'`")
            lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- jump mode

HEADER_KEY = "__header__"
DIM, BLUE, MAGENTA, RESET = "\x1b[2m", "\x1b[1;34m", "\x1b[35m", "\x1b[0m"


def bucket_jump_sessions(sessions, cache, group_state):
    """Bucket live sessions by workstream, most recently active group first.
    Returns [(group, [(session, summary_or_None, title, icon, age), ...]), ...]."""
    def mtime(s):
        try:
            return os.stat(s["transcript"]).st_mtime if s["transcript"] else 0
        except OSError:
            return 0

    gname = {}
    for g in group_state.get("groups", []):
        for p in g["members"]:
            gname[p] = g["name"]
    buckets = {}
    for s in sessions:
        buckets.setdefault(gname.get(s["transcript"], "new / ungrouped"), []).append(s)
    ordered = sorted(buckets.items(),
                     key=lambda kv: max(mtime(s) for s in kv[1]), reverse=True)

    out = []
    for group, members in ordered:
        members.sort(key=mtime, reverse=True)
        rows = []
        for s in members:
            hit = cache.get(s["transcript"]) if s["transcript"] else None
            sm = hit["summary"] if hit else None
            title = sm["title"] if sm else f"{os.path.basename(s['cwd'])} (not summarized yet)"
            icon = STATUS_ICON.get(sm.get("status", "?"), "❓") if sm else "❓"
            age = age_str(datetime.datetime.fromtimestamp(
                mtime(s), datetime.timezone.utc).isoformat()) if mtime(s) else "?"
            rows.append((s, sm, title, icon, age))
        out.append((group, rows))
    return out


def build_jump_items(sessions, cache, group_state):
    """Return (fzf_lines, previews): sessions bucketed by workstream, with a
    non-selectable header line per group. Line fields: key \t group \t display.
    The hidden group field makes group names fuzzy-searchable."""
    items, previews = [], []
    for group, rows in bucket_jump_sessions(sessions, cache, group_state):
        bar = "─" * max(2, 46 - len(group))
        items.append(f"{HEADER_KEY}\t{group}\t{BLUE}── {group} {bar}{RESET}")
        previews.append(f"# {group}\n\n" + "\n".join(
            f"{icon} {title}  ·  {age}" for _, _, title, icon, age in rows))

        for s, sm, title, icon, age in rows:
            tool = s.get("tool", "claude")
            tag = f"{MAGENTA}⬡ codex{RESET} " if tool == "codex" else ""
            items.append(f"{s['pane']}\t{group} {tool}\t  {icon} {tag}{title}  "
                         f"{DIM}·  {age}  ·  {s['pane']}{RESET}")
            lines = [f"# {icon} {title}", "", f"tool:    {tool}",
                     f"group:   {group}",
                     f"pane:    {s['pane']}", f"project: {s['cwd']}",
                     f"active:  {age}", ""]
            if sm:
                lines += [sm.get("summary", ""), ""]
                if sm.get("next"):
                    lines += [f"Next: {sm['next']}"]
            else:
                lines += ["No cached summary yet — run session_radar.py to generate one."]
            previews.append("\n".join(lines))
    return items, previews


def render_jump_markdown(sessions, cache, group_state):
    """The prefix+g view as a standalone markdown document — same data as the
    picker (live discovery + cached summaries, no LLM calls), laid out so
    another agent can read it: one section per workstream, one entry per
    session with pane, project, status, summary and next step."""
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    legend = "  ".join(f"{v} {k}" for k, v in STATUS_ICON.items() if k != "?")
    lines = ["# Claude Code sessions in tmux", "",
             f"*{now}* — {len(sessions)} live session(s), grouped by workstream, "
             f"most recent first. Status legend: {legend}.", "",
             "Jump to a session with `tmux switch-client -t '<pane>'`.", ""]
    for group, rows in bucket_jump_sessions(sessions, cache, group_state):
        lines += [f"## {group}", ""]
        for s, sm, title, icon, age in rows:
            tool = s.get("tool", "claude")
            lines += [f"### {icon} {title}", "",
                      f"- pane: `{s['pane']}`",
                      f"- tool: {tool}",
                      f"- project: `{s['cwd']}`",
                      f"- last activity: {age}"]
            if sm:
                lines += [f"- status: {sm.get('status', '?')}", ""]
                if sm.get("summary"):
                    lines += [sm["summary"], ""]
                if sm.get("next"):
                    lines += [f"**Next:** {sm['next']}", ""]
            else:
                lines += ["", "_No cached summary yet — run the full radar to generate one._", ""]
    return "\n".join(lines).rstrip() + "\n"


def load_jump_state():
    cache, group_state = {}, {}
    try:
        with open(os.path.join(RADAR_DIR, "cache.json")) as f:
            cache = json.load(f)
    except (OSError, json.JSONDecodeError):
        pass
    try:
        with open(os.path.join(RADAR_DIR, "state.json")) as f:
            group_state = json.load(f)
    except (OSError, json.JSONDecodeError):
        pass
    return cache, group_state


JUMP_EXPORT = os.path.join(RADAR_DIR, "jump.md")


def export_jump(path, sessions=None):
    """Write the prefix+g view as markdown to `path` ('-' = stdout)."""
    if sessions is None:
        sessions = discover()
    cache, group_state = load_jump_state()
    text = render_jump_markdown(sessions, cache, group_state)
    if path == "-":
        sys.stdout.write(text)
        return path
    path = os.path.expanduser(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    return path


def jump_mode():
    if not shutil.which("fzf"):
        sys.exit("fzf is required for --jump (brew install fzf)")
    sessions = discover()
    if not sessions:
        sys.exit("No Claude Code sessions found in tmux.")
    cache, group_state = load_jump_state()
    items, previews = build_jump_items(sessions, cache, group_state)
    # Keep a markdown copy of exactly this view for handing to another agent.
    try:
        export_jump(JUMP_EXPORT, sessions)
    except OSError:
        pass

    jump_dir = os.path.join(RADAR_DIR, "jump")
    shutil.rmtree(jump_dir, ignore_errors=True)
    os.makedirs(jump_dir)
    for i, text in enumerate(previews):
        with open(os.path.join(jump_dir, f"{i}.md"), "w") as f:
            f.write(text)

    r = subprocess.run(
        ["fzf", "--ansi", "--layout=reverse", "--delimiter", "\t", "--with-nth", "3..",
         "--preview", f"cat {shlex.quote(jump_dir)}/{{n}}.md",
         "--preview-window", "right:55%:wrap",
         "--bind", f"enter:transform:[ {{1}} = {HEADER_KEY} ] && echo ignore || echo accept",
         "--bind", f"double-click:transform:[ {{1}} = {HEADER_KEY} ] && echo ignore || echo accept",
         "--prompt", "jump to session > ", "--header",
         f"{len(sessions)} Claude Code session(s) — Enter to switch  ·  "
         f"markdown copy: {JUMP_EXPORT.replace(os.path.expanduser('~'), '~')}"],
        input="\n".join(items), text=True, stdout=subprocess.PIPE,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return  # cancelled
    pane = r.stdout.split("\t")[0]
    if pane == HEADER_KEY:
        return
    if os.environ.get("TMUX"):
        subprocess.run(["tmux", "switch-client", "-t", pane])
        subprocess.run(["tmux", "select-window", "-t", pane.rsplit(".", 1)[0]])
    else:
        os.execvp("tmux", ["tmux", "attach-session", "-t", pane.split(":")[0]])


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="haiku", help="summarizer model (default: haiku)")
    ap.add_argument("--group-model", default="sonnet", help="grouping model (default: sonnet)")
    ap.add_argument("--no-group", action="store_true", help="group by project dir instead of LLM")
    ap.add_argument("--jobs", type=int, default=8, help="parallel summarizer calls")
    ap.add_argument("--digest-tokens", type=int, default=DIGEST_TOKENS,
                    help=f"per-session token budget for the digest (default {DIGEST_TOKENS})")
    ap.add_argument("--list", action="store_true", help="only list discovered sessions")
    ap.add_argument("--jump", action="store_true",
                    help="interactive fzf picker: choose a session and switch to its tmux pane")
    ap.add_argument("--export", nargs="?", const=JUMP_EXPORT, metavar="PATH",
                    help="write the prefix+g view as markdown (no LLM calls) to PATH "
                         f"(default {JUMP_EXPORT}; '-' for stdout) and exit")
    ap.add_argument("--no-rename", action="store_true",
                    help="don't rename tmux windows after session summaries")
    ap.add_argument("--rename-only", action="store_true",
                    help="only label generic-named windows (cheap; no report/grouping)")
    ap.add_argument("--output", default=None, help="report path (default: ~/.claude/session-radar/)")
    args = ap.parse_args()

    if args.jump:
        jump_mode()
        return
    if args.export:
        out = export_jump(args.export)
        if out != "-":
            print(f"wrote {out}", file=sys.stderr)
        return
    if args.rename_only:
        rename_only(args)
        return

    started_at = datetime.datetime.now()
    workdir = os.path.join(RADAR_DIR, "workdir")
    os.makedirs(workdir, exist_ok=True)

    print("Discovering Claude Code sessions in tmux...", file=sys.stderr)
    sessions = discover()
    with_transcript = [s for s in sessions if s["transcript"]]
    orphans = [s for s in sessions if not s["transcript"]]
    print(f"Found {len(sessions)} claude process(es); "
          f"{len(with_transcript)} transcript(s) located.", file=sys.stderr)
    for s in orphans:
        print(f"  ! no transcript found for pane {s['pane']} (cwd {s['cwd']})", file=sys.stderr)

    if args.list:
        for s in sessions:
            print(f"{s['pane']:<40} {s['cwd']:<60} {s['transcript'] or '-'}")
        return

    if not with_transcript:
        print("Nothing to summarize.", file=sys.stderr)
        return

    cache_path = os.path.join(RADAR_DIR, "cache.json")
    try:
        with open(cache_path) as f:
            cache = json.load(f)
    except (OSError, json.JSONDecodeError):
        cache = {}

    digest_chars = args.digest_tokens * CHARS_PER_TOKEN
    print(f"Summarizing with {args.model} ({args.jobs} parallel, "
          f"~{args.digest_tokens // 1000}k tokens/session)...", file=sys.stderr)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as ex:
        list(ex.map(lambda s: summarize_session(s, args.model, workdir, cache, digest_chars),
                    with_transcript))
    fresh = sum(1 for s in with_transcript if not s.get("cached"))
    print(f"  {len(with_transcript) - fresh} cached, {fresh} summarized fresh", file=sys.stderr)
    with open(cache_path, "w") as f:
        json.dump(cache, f)

    if not args.no_rename:
        n = rename_windows(with_transcript)
        if n:
            print(f"  renamed {n} tmux window(s)", file=sys.stderr)

    state_path = os.path.join(RADAR_DIR, "state.json")
    try:
        with open(state_path) as f:
            state = json.load(f)
    except (OSError, json.JSONDecodeError):
        state = {}
    idx_by_path = {s["transcript"]: i for i, s in enumerate(with_transcript)}

    if args.no_group:
        by_tmux = {}
        for i, s in enumerate(with_transcript):
            by_tmux.setdefault(s["tmux_session"], []).append(i)
        groups = [{"name": k, "members": v} for k, v in by_tmux.items()]
    elif fresh == 0 and set(idx_by_path) == set(state.get("transcripts", [])):
        # nothing changed since last run — reuse the previous grouping
        print("No changes — reusing previous grouping.", file=sys.stderr)
        groups = [{"name": g["name"],
                   "members": [idx_by_path[p] for p in g["members"] if p in idx_by_path]}
                  for g in state["groups"]]
        groups = [g for g in groups if g["members"]]
    else:
        print(f"Grouping with {args.group_model}...", file=sys.stderr)
        groups = compute_groups(with_transcript, args.group_model, workdir)
        state = {"transcripts": list(idx_by_path),
                 "groups": [{"name": g["name"],
                             "members": [with_transcript[i]["transcript"] for i in g["members"]]}
                            for g in groups]}
        with open(state_path, "w") as f:
            json.dump(state, f)

    report = render(with_transcript, groups, started_at)
    if orphans:
        report += "\n## Unmapped claude processes\n\n"
        for s in orphans:
            report += f"- `{s['pane']}` in *{s['cwd']}* (no transcript found)\n"

    out = args.output
    if not out:
        os.makedirs(RADAR_DIR, exist_ok=True)
        stamped = os.path.join(RADAR_DIR, f"overview-{started_at:%Y%m%d-%H%M%S}.md")
        with open(stamped, "w") as f:
            f.write(report)
        latest = os.path.join(RADAR_DIR, "latest.md")
        shutil.copyfile(stamped, latest)
        out = stamped
        print(f"\nReport: {stamped}\n        {latest}\n", file=sys.stderr)
    else:
        with open(out, "w") as f:
            f.write(report)
        print(f"\nReport: {out}\n", file=sys.stderr)

    print(report)


if __name__ == "__main__":
    main()
