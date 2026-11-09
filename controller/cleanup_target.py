#!/usr/bin/env python3
"""Verify individual Pixel backup labels, then optionally unlink the NAS target."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import sys
import tempfile
import xml.etree.ElementTree as ET

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / '.deps'))
sys.path.insert(0, str(HERE.parent / 'phone'))
from storage import Inventory, file_path, now, path_key, validate_relative
from pixel_photos import Phone, labels, PKG
from syncthing_state import SyncthingState
from target_guard import TargetDirectory, PROTECTED_SOURCE, ALLOWED_TARGET


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def fingerprint(path):
    value = os.lstat(path)
    if not stat.S_ISREG(value.st_mode):
        raise ValueError('Expected a regular file, not a symlink or directory')
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)


def inspect_backup(phone, target_root, relative, pixel_root):
    """Return fresh per-file proof. An overall backup-complete label is insufficient."""
    relative = validate_relative(relative)
    target_root = Path(target_root).resolve()
    target = target_root / relative
    if not target.parent.resolve().is_relative_to(target_root):
        raise ValueError('Target escapes configured root')
    before = fingerprint(target)
    digest = sha256_file(target)
    pixel_path = file_path(pixel_root, relative)
    where = "_data='" + pixel_path.replace("'", "''") + "'"
    query = ['content', 'query', '--uri', 'content://media/external/file',
             '--projection', '_id:mime_type', '--where', where]
    text = phone.adb('shell', shlex.join(['sha256sum', pixel_path]) + ' && ' + shlex.join(query))
    matches = re.findall(r'^([a-fA-F0-9]{64})\s', text, re.M)
    if matches != [digest]:
        raise ValueError('Pixel content hash does not match NAS target')
    media = re.findall(r'_id=(\d+), mime_type=([^\r\n]+)', text)
    if len(media) != 1:
        raise ValueError('Expected exactly one Android MediaStore row for the exact path')
    media_id, mime = media[0]
    collection = 'images' if mime.startswith('image/') else 'video' if mime.startswith('video/') else None
    if collection is None:
        raise ValueError('Unsupported media MIME type')
    uri = 'content://media/external/{}/media/{}'.format(collection, media_id)
    root = phone.dump(before=[['input', 'keyevent', 'KEYCODE_WAKEUP'],
                             ['am', 'start', '-a', 'android.intent.action.VIEW', '-d', uri,
                              '-t', mime, '-p', PKG, '--grant-read-uri-permission']], settle=0.5)
    texts = labels(root)
    if os.path.basename(relative) not in texts:
        root = phone.dump(before=[['input', 'swipe', '720', '1850', '720', '650', '400']], settle=0.3)
        texts = labels(root)
    # Require an opened details pane and exact filename as well as the cloud label.
    details_open = any(n.get('resource-id', '').endswith('/collapse_info_panel_fab')
                       for n in root.iter('node'))
    same_name = os.path.basename(relative) in texts
    backed_up = any(t.strip() in ('已备份', 'Backed up') for t in texts)
    blockers = any(re.search(r'未备份|无法备份|备份失败|正在备份|等待备份|Not backed up|Backing up|Unable to back up', t, re.I)
                   for t in texts)
    if fingerprint(target) != before:
        raise ValueError('NAS target changed during verification')
    # Verify the Pixel bytes again after the UI check; an in-flight replacement
    # must never authorize deletion of a different NAS file.
    after = phone.adb('shell', shlex.join(['sha256sum', pixel_path]))
    if not after.startswith(digest + ' '):
        raise ValueError('Pixel file changed during UI verification')
    confirmed = details_open and same_name and backed_up and not blockers
    failed = details_open and same_name and any(re.search(r'无法备份|备份失败|Unable to back up|Backup failed', t, re.I) for t in texts)
    evidence = {'state': 'confirmed' if confirmed else 'failed' if failed else 'not_confirmed',
                'file': relative, 'sha256': digest, 'content_verified': True,
                'pixel_path': pixel_path, 'media_uri': uri, 'checked_at': now(),
                'filename_visible': same_name, 'details_open': details_open,
                'backup_label': '已备份' if backed_up else None,
                'ui_sha256': hashlib.sha256(ET.tostring(root)).hexdigest()}
    return evidence, before


def load_connection(config_file):
    from configuration import load
    config = load(config_file)['mysql']
    return config['database'], {key: config[key] for key in ('host', 'port', 'user', 'password')}


def emit(data):
    print(json.dumps(data, ensure_ascii=False), flush=True)


def process_files(args, db, phone, relative_files, retention=None, output=emit):
    failed = False
    for relative in relative_files:
        evidence = None
        try:
            rows = db.query('SELECT status,target_path FROM files WHERE source_key=%s AND BINARY source_path=BINARY %s',
                            (path_key(relative), relative))
            if not rows or rows[0]['status'] != 'linked' or rows[0]['target_path'] != relative:
                raise ValueError('File is not an exact linked inventory entry')
            evidence, expected = inspect_backup(phone, args.target, relative, args.pixel_root)
            if retention:
                proof, before = retention
                if list(expected) != before[relative]:
                    raise ValueError('Target changed since the pre-release snapshot')
                evidence['retention'] = dict(proof, file=relative, before_fingerprint=before[relative])
            db.record_backup_check(relative, evidence)
            if evidence['state'] != 'confirmed':
                output({'file': relative, 'action': 'kept', 'backup_state': evidence['state']})
                continue
            if not args.execute:
                output({'file': relative, 'action': 'preview', 'backup_state': 'confirmed',
                      'deletion_origin': 'manual'})
                continue
            if retention:
                output({'file': relative, 'action': 'manual_candidate', 'backup_state': 'confirmed',
                        'run_id': retention[0]['run_id']})
            event = db.delete_confirmed_target(args.source, args.target, relative, evidence,
                                               expected, reason='photos_retained' if retention else 'manual_after_backup')
            output({'file': relative, 'action': 'deleted', 'deletion_origin': 'manual', 'event_id': event})
        except Exception as exc:
            failed = True
            # Never record an error as backup confirmation or remove this file.
            if evidence is None:
                evidence = {'state': 'unknown', 'file': relative, 'checked_at': now(),
                            'error_type': type(exc).__name__}
                try:
                    db.record_backup_check(relative, evidence)
                except Exception:
                    pass
            output({'file': relative, 'action': 'error', 'error_type': type(exc).__name__, 'message': str(exc)})
            # A changed foreground or broken ADB can invalidate later UI checks.
            break
    return 2 if failed else 0


def run(args):
    # Validate roots before any Photos UI action (including device free-up).
    with TargetDirectory(args.source, args.target):
        pass
    database, connection = load_connection(args.config)
    db = Inventory(database, **connection)
    try:
        if args.classify_deleted:
            sync = SyncthingState(args.syncthing_config, args.syncthing_folder, args.pixel_device)
            db.classify_deletions(args.target, sync.remote_deletion, limit=args.limit)
            emit({'action': 'classified', 'limit': args.limit})
            return 0
        phone = Phone(args)
        if args.relaxed:
            from relaxed_cleanup import run_relaxed
            return run_relaxed(args, db, phone, fingerprint, emit)
        if args.reason != 'manual_after_backup':
            from retention_workflow import run_workflow
            return run_workflow(args, db, phone, process_files, fingerprint, emit)
        if args.file:
            relative_files = list(dict.fromkeys(validate_relative(name) for name in args.file))
        else:
            relative_files = [r['target_path'] for r in db.query("""SELECT target_path FROM files
                WHERE status='linked' AND source_present=1
                ORDER BY COALESCE(backup_checked_at,''),mtime_ns,source_key LIMIT %s""", (args.limit,))]
        return process_files(args, db, phone, relative_files)
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--file', action='append', help='Relative file path; repeat for several files')
    parser.add_argument('--source', default=PROTECTED_SOURCE)
    parser.add_argument('--target', default=ALLOWED_TARGET)
    parser.add_argument('--pixel-root', default='/storage/emulated/0/DCIM')
    parser.add_argument('--config', default=str(HERE / 'config.toml'))
    parser.add_argument('--container', default='pixel-web')
    parser.add_argument('--adb-backend', choices=['host', 'container'], default='host')
    parser.add_argument('--adb-path', help='ADB 客户端路径；默认按后端选择')
    parser.add_argument('--serial', default=os.environ.get('PICSYNC_SERIAL', ''))
    parser.add_argument('--adb-timeout', type=int, default=40)
    parser.add_argument('--timings', action='store_true')
    parser.add_argument('--limit', type=int, default=20)
    parser.add_argument('--relaxed', action='store_true', help='两端同步完成+相册备份完成+无可释放内容后，直接清理目标快照，不逐张核验；limit 不限制此模式')
    parser.add_argument('--execute', action='store_true', help='执行相册释放和残留兜底清理；manual_after_backup 模式直接核验后清理')
    parser.add_argument('--reason', choices=['photos_retained', 'manual_after_backup'], default='photos_retained')
    parser.add_argument('--sync-timeout', type=int, default=900, help='每阶段等待同步稳定的最大秒数')
    parser.add_argument('--poll-interval', type=int, default=10)
    parser.add_argument('--stable-samples', type=int, default=3)
    parser.add_argument('--classify-deleted', action='store_true', help='Read Syncthing tombstones and label observed deletions')
    parser.add_argument('--syncthing-config', default=os.environ.get('PICSYNC_SYNCTHING_CONFIG', '/etc/syncthing/config.xml'))
    parser.add_argument('--syncthing-folder', default='syncthingPic')
    parser.add_argument('--pixel-device', default=os.environ.get('PICSYNC_SYNCTHING_DEVICE', ''))
    args = parser.parse_args()
    if not args.serial or not args.pixel_device:
        parser.error("Explicit ADB serial and Syncthing device ID are required")
    if args.relaxed and args.reason == 'manual_after_backup':
        parser.error('--relaxed cannot be combined with manual_after_backup')
    if args.limit < 1 or args.adb_timeout < 1 or args.sync_timeout < 1 or args.poll_interval < 1 or args.stable_samples < 2:
        parser.error('limit, timeouts and poll interval must be positive; stable-samples must be >= 2')
    args.file = [validate_relative(name) for name in args.file] if args.file else None
    if os.path.abspath(args.target) != ALLOWED_TARGET or os.path.abspath(args.source) != PROTECTED_SOURCE:
        parser.error('Source and target must match the configured immutable safety roots')
    lock_id = hashlib.sha256((args.container + args.serial).encode()).hexdigest()[:16]
    with open(os.path.join(tempfile.gettempdir(), 'file-sync-controller.lock'), 'a') as controller_lock, open(os.path.join(tempfile.gettempdir(), 'phone-' + lock_id + '.lock'), 'a') as lock:
        fcntl.flock(controller_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return run(args)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        emit({'action': 'error', 'error_type': type(exc).__name__, 'message': str(exc)})
        sys.exit(1)
