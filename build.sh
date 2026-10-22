#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v python3 >/dev/null 2>&1; then
  printf '%s\n' "Python 3 is required by the current Unix bootstrap. Windows downloads its managed runtime automatically; portable macOS/Linux tool downloads have not yet been verified." >&2
  exit 1
fi

exec python3 "$repo_root/tools/build.py" "$@"
