#!/bin/bash
set -euo pipefail
packaging_root="$(cd "$(dirname "$0")/.." && pwd)"
exec "$packaging_root/staging/runtime/python/bin/python3.12" "$packaging_root/scripts/build.py" "$@"
