#!/bin/sh
# Creates private local examples/state directories only; starts nothing.
set -eu
project_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_root"
umask 077
[ -e .env ] || cp .env.example .env
[ -e controller/config.toml ] || cp controller/config.example.toml controller/config.toml
[ -e dashboard/config.toml ] || cp dashboard/config.example.toml dashboard/config.toml
mkdir -p state/settings state/logs state/status state/syncthing/config state/pixel state/adb-auth
echo 'Private configuration files prepared. Edit .env before starting Compose.'
