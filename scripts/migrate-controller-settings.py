#!/usr/bin/env python3
"""One-time migration from the former overlay into the original config file.

Stop controller and dashboard before running. Backups remain private and are never
loaded by the application. No source photos or business database are accessed.
"""
import argparse
from pathlib import Path
import shutil
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'controller'))
import live_config


def migrate(root, backup):
    original = root / 'controller/config.toml'
    legacy = root / 'state/settings/controller.toml'
    if not legacy.exists():
        return False
    if original.is_symlink() or legacy.is_symlink():
        raise ValueError('Refusing symlinked configuration')
    values = live_config.tomllib.loads(legacy.read_text())
    live_config.validate(values)
    backup.mkdir(parents=True, exist_ok=True, mode=0o700)
    for source, name in ((original, 'controller-before-migration.toml'), (legacy, 'legacy-settings.toml')):
        target = backup / name
        if target.exists():
            raise ValueError('Migration backup already exists')
        shutil.copy2(source, target)
        target.chmod(0o600)
    expected = live_config.public(live_config.merge(live_config.read(original)[0], values))
    live_config.save(values, live_config.read(original)[1], config_path=original)
    assert live_config.read(original)[0] == expected
    legacy.unlink()
    return True


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--backup', type=Path, required=True)
    args = parser.parse_args()
    print('Original config updated; legacy overlay removed.' if migrate(args.root, args.backup) else 'No legacy overlay found.')
