"""Release on Pixel, wait for synced removals, then verify unchanged survivors."""
import json
import os
from pathlib import Path
import shlex
import time
import uuid

from storage import now, file_path
from syncthing_state import SyncthingState

REPORT_DIRECTORY = Path(__file__).resolve().parent / 'logs' / 'cleanup-runs'


def pixel_files(phone, root):
    text = phone.adb('shell', shlex.join(['find', root, '-type', 'f', '-print0']))
    prefix = root.rstrip('/') + '/'
    return {p[len(prefix):] for p in text.split('\0') if p.startswith(prefix)}

def retained(before, pixel_after, target, fingerprint):
    """New arrivals and changed replacements never become retention candidates."""
    result = []
    for relative, expected in before.items():
        if relative not in pixel_after:
            continue
        try:
            if list(fingerprint(file_path(target, relative))) == expected:
                result.append(relative)
        except FileNotFoundError:
            pass
    return result


def wait_settled(sync, phone, args, before=None, clock=time.monotonic, sleep=time.sleep, progress=None):
    deadline, previous, stable = clock() + args.sync_timeout, None, 0
    while True:
        state = sync.settled_state()
        present = pixel_files(phone, args.pixel_root) if before is not None and state['ready'] else set()
        # A missing Pixel file still present on NAS is an in-flight deletion,
        # not a file retained by Photos; never fall through to manual cleanup.
        pending = [] if before is None else [name for name in before
            if name not in present and os.path.lexists(file_path(args.target, name))]
        signature = (state['token'], sorted(present))
        if state['ready'] and not pending:
            stable = stable + 1 if signature == previous else 1
            if stable >= args.stable_samples:
                return present, state
            previous = signature
        else:
            stable, previous = 0, None
        if progress:
            progress(dict(state, stable_samples=stable, pending_deletions=len(pending)))
        if clock() + args.poll_interval > deadline:
            raise TimeoutError('Syncthing did not settle; manual cleanup is prohibited')
        sleep(args.poll_interval)


def run_workflow(args, db, phone, process_files, fingerprint, emit):
    run_id = uuid.uuid4().hex
    directory = REPORT_DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (run_id + '.json')
    report = {'run_id': run_id, 'created_at': now(), 'stage': 'started', 'target': args.target,
              'pixel_root': args.pixel_root,
              'execute': args.execute, 'before': {}, 'results': [], 'manual_candidates': []}

    def save():
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        temporary.replace(path)

    def record(result):
        report['results'].append(result)
        if result['action'] in ('manual_candidate', 'deleted'):
            if result['file'] not in report['manual_candidates']:
                report['manual_candidates'].append(result['file'])
        save()
        emit(result)

    save()
    emit({'action': 'workflow_started', 'run_id': run_id, 'report': str(path)})
    try:
        sync = SyncthingState(args.syncthing_config, args.syncthing_folder, args.pixel_device)
        if not args.execute:
            # Preview neither clicks release nor assigns a retention reason.
            report['sync_before'] = sync.settled_state()
            report['release'] = phone.release_space(execute=False)
            report['stage'] = 'preview_only'
            report['finished_at'] = now()
            save()
            emit({'action': 'preview', 'message': '未执行相册释放；尚无 manual 候选', 'report': str(path)})
            return 0
        report['stage'] = 'waiting_before'
        save()
        def progress(state):
            report['last_sync_check'] = dict(state, checked_at=now())
            save()
            emit(dict(action='waiting_for_sync', stage=report['stage'], **state))
        _, report['sync_before'] = wait_settled(sync, phone, args, progress=progress)
        present = pixel_files(phone, args.pixel_root)
        rows = db.query("""SELECT target_path FROM files WHERE status='linked' AND source_present=1
                            ORDER BY COALESCE(backup_checked_at,''),mtime_ns,source_key""")
        for row in rows:
            relative = row['target_path']
            if relative in present:
                try:
                    report['before'][relative] = list(fingerprint(file_path(args.target, relative)))
                except FileNotFoundError:
                    pass
        report['snapshot_at'] = now()
        report['stage'] = 'releasing'
        save()
        report['release'] = phone.release_space(execute=True)
        save()
        if report['release']['action'] not in ('freed', 'nothing_to_free'):
            raise RuntimeError('Photos release not confirmed; manual cleanup is prohibited')
        report['stage'] = 'waiting_after'
        save()
        present, report['sync_after'] = wait_settled(sync, phone, args, before=report['before'], progress=progress)
        report['settled_at'] = now()
        report['pixel_remaining'] = sorted(set(report['before']) & present)
        report['removed_from_pixel'] = sorted(set(report['before']) - present)
        survivors = retained(report['before'], present, args.target, fingerprint)
        report['unchanged_both_after'] = list(survivors)
        report['changed_or_missing_target'] = sorted(set(report['pixel_remaining']) - set(survivors))
        if args.file:
            selected = set(args.file)
            survivors = [name for name in survivors if name in selected]
        survivors = survivors[:args.limit]
        report['survivors_to_check'] = survivors
        report['stage'] = 'checking_survivors'
        save()
        proof = {'state': 'retained_after_release', 'run_id': run_id,
                 'release_action': report['release']['action'], 'settled_at': report['settled_at']}
        code = process_files(args, db, phone, survivors, retention=(proof, report['before']), output=record)
        report['stage'] = 'complete' if code == 0 else 'check_failed'
        report['finished_at'] = now()
        save()
        emit({'action': report['stage'], 'run_id': run_id,
              'removed_after_release': len(report['removed_from_pixel']),
              'manual_candidates': len(report['manual_candidates']), 'report': str(path)})
        return code
    except Exception as exc:
        report['failed_stage'] = report['stage']
        report['stage'] = 'error'
        report['finished_at'] = now()
        report['error'] = str(exc)
        save()
        raise
