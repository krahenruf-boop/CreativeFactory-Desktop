#!/bin/bash
# Run only on the company Mac. Creates this repository's independent read-only identity.
set -euo pipefail
umask 077
cf_ssh="$HOME/.ssh"
cf_key="$cf_ssh/id_ed25519_creativefactory_desktop"
cf_config="$cf_ssh/creativefactory_desktop.conf"
cf_hosts="$cf_ssh/known_hosts_creativefactory_desktop"
mkdir -p "$cf_ssh"
chmod 700 "$cf_ssh"
if [ ! -f "$cf_key" ]; then
  ssh-keygen -t ed25519 -N '' -C 'CreativeFactory-Desktop company read-only deploy key' -f "$cf_key"
fi
chmod 600 "$cf_key"
cf_temp="$(mktemp)"
trap 'rm -f "$cf_temp"' EXIT
ssh-keyscan -t ed25519 github.com 2>/dev/null > "$cf_temp"
cf_fingerprint="$(ssh-keygen -lf "$cf_temp" -E sha256 | awk '{print $2}')"
if [ "$cf_fingerprint" != 'SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU' ]; then
  echo 'GitHub host fingerprint mismatch. No host key or SSH configuration changed.' >&2
  exit 1
fi
cp "$cf_temp" "$cf_hosts"
chmod 600 "$cf_hosts"
cat > "$cf_config" <<EOF
Host github-creativefactory-desktop
  HostName github.com
  User git
  IdentityFile $cf_key
  IdentitiesOnly yes
  StrictHostKeyChecking yes
  UserKnownHostsFile $cf_hosts
EOF
chmod 600 "$cf_config"
# Separate -F configuration: ~/.ssh/config and Order Pulse are never edited.
echo 'Add this PUBLIC key to krahenruf-boop/CreativeFactory-Desktop, read-only:'
cat "$cf_key.pub"
echo 'Private key remains on this Mac. Do not send it.'
