#!/bin/sh
set -eu
script_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python_bin=$(command -v python3.13 || true)
if [ -z "$python_bin" ]; then
  echo 'APPROVED_PYTHON_313_REQUIRED' >&2
  exit 78
fi
exec "$python_bin" "$script_root/station.py" "$@"
