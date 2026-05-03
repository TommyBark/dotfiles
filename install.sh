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

# Bootstrap tmux plugin manager (required by .tmux.conf)
TPM_DIR="$HOME/.tmux/plugins/tpm"
if [ ! -d "$TPM_DIR" ]; then
	git clone https://github.com/tmux-plugins/tpm "$TPM_DIR"
	echo "installed tpm -> $TPM_DIR"
	echo "open tmux and press \`+I (backtick + I) to install plugins"
fi
