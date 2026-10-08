#!/bin/bash
set -euo pipefail
umask 077
# Official pinned Git LFS client, private to Creative Factory; no global Git/Homebrew changes.
cf_tools="$HOME/.local/share/creativefactory-tools"
cf_lfs="$cf_tools/git-lfs-3.7.1/git-lfs"
mkdir -p "$cf_tools"
if [ ! -x "$cf_lfs" ]; then
  cf_tmp="$(mktemp -d)"
  trap 'rm -rf "$cf_tmp"' EXIT
  curl --fail --location --proto '=https' --tlsv1.2 --retry 2 https://github.com/git-lfs/git-lfs/releases/download/v3.7.1/git-lfs-darwin-arm64-v3.7.1.zip -o "$cf_tmp/lfs.zip"
  cf_hash="$(shasum -a 256 "$cf_tmp/lfs.zip" | awk '{print $1}')"
  [ "$cf_hash" = 76260fb34f4ee622ff0a66b857e5954aa49c7e343a92e57a1ec4a760618c94b2 ] || { echo 'Git LFS checksum mismatch' >&2; exit 1; }
  unzip -q "$cf_tmp/lfs.zip" -d "$cf_tmp/unpacked"
  cp -R "$cf_tmp/unpacked/git-lfs-3.7.1" "$cf_tools/"
fi
export PATH="$cf_tools/git-lfs-3.7.1:$PATH"
git lfs install --local
