#!/bin/sh
set -eu
unset ADB_SERVER_SOCKET
adb_binary="${ADB_REAL_BINARY:-adb}"
for argument in "$@"; do
    case "$argument" in
        kill-server) exit 0 ;;
        start-server) exec "$adb_binary" -H 127.0.0.1 -P 5037 devices ;;
        server|nodaemon|fork-server) echo 'ADB service lifecycle is managed by systemd' >&2; exit 1 ;;
    esac
done
exec "$adb_binary" -H 127.0.0.1 -P 5037 "$@"
