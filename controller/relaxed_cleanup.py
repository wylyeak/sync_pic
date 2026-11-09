"""Opt-in aggregate gates; no hashes or per-photo UI inspection."""
import json
import os
from pathlib import Path
import uuid

from pixel_photos import backup_status
from storage import now, validate_relative
from syncthing_state import SyncthingState

REPORT_DIRECTORY = Path(__file__).resolve().parent / 'logs' / 'cleanup-runs'


def snapshot_targets(root, selected, fingerprint):
    result = {}
    for directory, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d != '@eaDir'
                   and not os.path.islink(os.path.join(directory, d))]
        for name in files:
            if name.startswith('.'):
                continue
            path = Path(directory, name)
            relative = validate_relative(os.path.relpath(path, root))
            if selected and relative not in selected:
                continue
            if path.is_symlink():
                continue
            try:
                result[relative] = list(fingerprint(path))
            except FileNotFoundError:
                pass
    return result


def run_relaxed(args, db, phone, fingerprint, emit):
    run_id = uuid.uuid4().hex
    REPORT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIRECTORY / (run_id + '.json')
    report = {'run_id': run_id, 'mode': 'relaxed', 'created_at': now(),
              'execute': args.execute, 'target': args.target, 'stage': 'checking_sync'}

    def save():
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        temporary.replace(path)

    save()
    emit({'action': 'workflow_started', 'mode': 'relaxed', 'run_id': run_id, 'report': str(path)})
    try:
        sync = SyncthingState(args.syncthing_config, args.syncthing_folder, args.pixel_device)
        before = sync.settled_state()
        report['sync_before'] = before
        if not before['ready']:
            raise RuntimeError('Syncthing is not fully synchronized; relaxed cleanup skipped')
        snapshots = snapshot_targets(args.target, set(args.file or []), fingerprint)
        report['before'] = snapshots
        report['stage'] = 'checking_photos'
        save()
        root = phone.menu()
        status = backup_status(root)
        report['photos_status'] = status
        if status['state'] != 'complete':
            raise RuntimeError('Google Photos does not explicitly report backup complete')
        _, button = phone.free_page(root)
        report['release_button'] = button
        if button is not None:
            raise RuntimeError('Photos has releasable space; relaxed cleanup requires an explicit no-items message')
        after = sync.settled_state()
        report['sync_after'] = after
        if not after['ready'] or before['token'] != after['token']:
            raise RuntimeError('Sync state changed while checking Photos; retry with a fresh snapshot')
        evidence = {'state': 'aggregate_confirmed', 'run_id': run_id, 'mode': 'relaxed',
                    'checked_at': now(), 'backup_complete': True, 'nothing_to_free': True,
                    'sync_ready': True, 'sync_token_before': before['token'], 'sync_token_after': after['token']}
        report['evidence'] = evidence
        if not args.execute:
            report['stage'] = 'preview_only'
            emit({'action': 'preview', 'mode': 'relaxed', 'files': len(snapshots), 'report': str(path)})
        else:
            report['stage'] = 'deleting'
            save()
            report['results'] = db.delete_relaxed_targets(args.source, args.target, snapshots, evidence,
                progress=lambda done, total: emit({'action': 'deleting', 'done': done, 'total': total}))
            failures = [r for r in report['results'] if r['action'] == 'error']
            report['stage'] = 'complete' if not failures else 'partial_error'
            emit({'action': report['stage'], 'mode': 'relaxed',
                  'deleted': sum(r['action'] == 'deleted' for r in report['results']),
                  'errors': len(failures), 'report': str(path)})
        report['finished_at'] = now()
        save()
        return 2 if report['stage'] == 'partial_error' else 0
    except Exception as exc:
        report['failed_stage'], report['stage'] = report['stage'], 'error'
        report['error'], report['finished_at'] = str(exc), now()
        save()
        raise
