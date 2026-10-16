#!/usr/bin/env bash
# Save tmux sessions with tmux-resurrect every 5 minutes, replacing
# tmux-continuum's status-bar autosave (which forked on every status redraw).
# Started from ~/.tmux.conf via `run-shell -b`; exits when the tmux server does.
# A lock dir keeps config reloads from starting a second loop.

interval=${1:-300}
lock="${TMPDIR:-/tmp}/tmux-autosave-$(id -u).lock"

if ! mkdir "$lock" 2>/dev/null; then
	kill -0 "$(cat "$lock/pid" 2>/dev/null)" 2>/dev/null && exit 0
	rm -rf "$lock" && mkdir "$lock" || exit 0
fi
echo $$ >"$lock/pid"
trap 'rm -rf "$lock"' EXIT

while tmux has-session 2>/dev/null; do
	sleep "$interval"
	tmux has-session 2>/dev/null || break
	~/.tmux/plugins/tmux-resurrect/scripts/save.sh quiet
done
