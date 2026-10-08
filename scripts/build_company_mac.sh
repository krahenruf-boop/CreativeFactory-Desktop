#!/bin/bash
set -euo pipefail
umask 077
export PATH="$HOME/.local/share/creativefactory-tools/git-lfs-3.7.1:$PATH"
cf_root="$(cd "$(dirname "$0")/.." && pwd)"
cf_python="$cf_root/staging/runtime/python/bin/python3.12"
if [ ! -x "$cf_python" ]; then cf_python="$(command -v python3)"; fi
exec "$cf_python" "$cf_root/scripts/company_mac.py" build "$@"
