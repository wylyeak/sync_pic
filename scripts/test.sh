#!/bin/sh
set -eu
project_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_root"
export PICSYNC_CONFIG="$project_root/controller/config.example.toml"
export PICSYNC_DASHBOARD_CONFIG="$project_root/dashboard/config.example.toml"
# Deliberately do not source .env or contact any production database/device.
unset PICSYNC_CONFIG_SOCKET PICSYNC_LIVE_CONFIG
unset FILE_SYNC_TEST_MYSQL
unset PICSYNC_SOURCE PICSYNC_TARGET PICSYNC_MEDIA_MOUNT PICSYNC_MEDIA_FS
unset PICSYNC_MYSQL_HOST PICSYNC_MYSQL_PORT PICSYNC_MYSQL_USER PICSYNC_MYSQL_PASSWORD PICSYNC_MYSQL_DATABASE
unset PICSYNC_SERIAL PICSYNC_SYNCTHING_DEVICE PICSYNC_SYNCTHING_CONFIG PICSYNC_ADB_PATH PICSYNC_BARK_KEY
unset PICSYNC_PIXEL_URL PICSYNC_SYNCTHING_URL
export PYTHONPATH="$project_root/controller:$project_root/phone:$project_root/dashboard${PYTHONPATH:+:$PYTHONPATH}"
python3 -m unittest discover -s controller -p 'test_*.py'
python3 -m unittest discover -s phone -p 'test_*.py'
python3 -m unittest discover -s dashboard -p 'test_*.py'
python3 -m unittest discover -s tests -p 'test_*.py'
python3 scripts/check-portability.py
