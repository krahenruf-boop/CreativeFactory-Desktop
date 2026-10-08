#!/bin/bash
set -euo pipefail
umask 077
cf_dir="$HOME/Projects/CreativeFactory-Desktop"
cf_config="$HOME/.ssh/creativefactory_desktop.conf"
[ -f "$cf_config" ] || { echo 'Run company_ssh_setup.sh and authorize its public key first.' >&2; exit 1; }
if [ -e "$cf_dir" ]; then echo 'Destination exists; refusing to replace it.' >&2; exit 1; fi
mkdir -p "$HOME/Projects"
GIT_LFS_SKIP_SMUDGE=1 GIT_SSH_COMMAND="ssh -F $cf_config" git clone git@github-creativefactory-desktop:krahenruf-boop/CreativeFactory-Desktop.git "$cf_dir"
git -C "$cf_dir" config core.sshCommand "ssh -F $cf_config"
bash "$cf_dir/scripts/company_lfs_setup.sh"
cd "$cf_dir"
PATH="$HOME/.local/share/creativefactory-tools/git-lfs-3.7.1:$PATH" git lfs pull
bash scripts/setup_company_mac.sh
