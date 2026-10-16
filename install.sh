#!/usr/bin/env bash
set -euo pipefail

DOTFILES="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

link() {
	local rel="$1"
	local src="$DOTFILES/$rel"
	local dst="$HOME/$rel"
	mkdir -p "$(dirname "$dst")"
	if [ -e "$dst" ] && [ ! -L "$dst" ]; then
		local backup="$dst.backup.$(date +%Y%m%d%H%M%S)"
		mv "$dst" "$backup"
		echo "backed up $dst -> $backup"
	fi
	ln -sfn "$src" "$dst"
	echo "linked $dst -> $src"
}

link .gitconfig
link .tmux.conf
link .claude/CLAUDE.md
link .config/ccstatusline/settings.json
link .tmux/autosave.sh
link .claude/skills/session-radar/SKILL.md

# Session Radar + park/restore live at ~/claude/session-radar (referenced by .tmux.conf)
SR_DST="$HOME/claude/session-radar"
mkdir -p "$(dirname "$SR_DST")"
if [ -e "$SR_DST" ] && [ ! -L "$SR_DST" ]; then
	backup="$SR_DST.backup.$(date +%Y%m%d%H%M%S)"
	mv "$SR_DST" "$backup"
	echo "backed up $SR_DST -> $backup"
fi
ln -sfn "$DOTFILES/session-radar" "$SR_DST"
echo "linked $SR_DST -> $DOTFILES/session-radar"
command -v uv >/dev/null || echo "warning: session radar needs uv (brew install uv)"
command -v fzf >/dev/null || echo "warning: session radar jump (prefix+g) needs fzf (brew install fzf)"

# Bootstrap tmux plugin manager (required by .tmux.conf)
TPM_DIR="$HOME/.tmux/plugins/tpm"
if [ ! -d "$TPM_DIR" ]; then
	git clone https://github.com/tmux-plugins/tpm "$TPM_DIR"
	echo "installed tpm -> $TPM_DIR"
	echo "open tmux and press \`+I (backtick + I) to install plugins"
fi
