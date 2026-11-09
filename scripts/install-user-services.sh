#!/bin/sh
# Installs files only. Does NOT start any service or photo workflow.
set -eu
project_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
export PICSYNC_INSTALL_ROOT="$project_root"
python3 - <<'PY'
import os
from pathlib import Path
import shutil
root = Path(os.environ['PICSYNC_INSTALL_ROOT'])
if any(ch in str(root) for ch in '\n\r%"\\'):
    raise SystemExit('Install path contains unsupported systemd characters')
units = Path.home()/'.config/systemd/user'
units.mkdir(parents=True, exist_ok=True)
shutil.copy2(root/'deploy/adb/picsync-adb.service', units/'picsync-adb.service')
(units/'picsync-controller.service').write_text(f'''[Unit]
Description=PicSync photo backup controller
Wants=picsync-adb.service
After=picsync-adb.service
[Service]
Type=simple
WorkingDirectory="{root}"
ExecStart="{root}/scripts/run-controller.sh"
Restart=always
RestartSec=10
TimeoutStopSec=30
UMask=0077
[Install]
WantedBy=default.target
''')
print('Installed user service files. Review them and run systemctl --user daemon-reload.')
print('No services were started.')
PY
