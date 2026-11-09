#!/bin/sh
set -eu
project_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_root"
if [ -f .env ]; then
    set -a
    . ./.env
    set +a
fi
python_binary="${PICSYNC_PYTHON:-$project_root/.venv/bin/python}"
exec "$python_binary" controller/file_sync.py "$@"
