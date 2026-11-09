#!/usr/bin/env python3
"""Frozen-batch photo backup controller; source entries are never removed."""
import argparse
import copy
from contextlib import contextmanager, suppress
import fcntl
import faulthandler
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import queue
from pathlib import Path
import signal
import stat
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import uuid

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE / '.deps'), str(HERE.parent / 'phone')]
from configuration import load, redacted
from batch_report import PHASES, transition, summary
from batch_store import BatchStore
from telemetry import Telemetry
from storage import Inventory, DatabaseError, now, file_path, path_key
from cleanup_target import fingerprint
from relaxed_cleanup import snapshot_targets
from target_guard import TargetDirectory, PROTECTED_SOURCE
from syncthing_state import SyncthingState
from pixel_photos import Phone, backup_status
import requests

log = logging.getLogger('filesync')

@contextmanager
def lock(name):
    # Same device lock as the independent cleanup command.
    with open(Path(tempfile.gettempdir()) / name, 'a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield

class Stopped(Exception):
    pass

class StorageUnavailable(RuntimeError):
    pass

class SyncthingUnavailable(RuntimeError):
    pass

class PhoneUnavailable(RuntimeError):
    pass

def ensure_storage(source):
    if source != PROTECTED_SOURCE:
        return  # Isolated tests operate only on temporary directories.
    mount = os.environ.get('PICSYNC_MEDIA_MOUNT', os.path.dirname(PROTECTED_SOURCE))
    fs_type = os.environ.get('PICSYNC_MEDIA_FS', '')
    with open('/proc/self/mountinfo') as stream:
        mounted = any(line.split(' - ', 1)[0].split()[4] == mount
                      and (not fs_type or line.split(' - ', 1)[1].split()[0] == fs_type)
                      for line in stream)
    if not mounted:
        raise StorageUnavailable('Required media mount unavailable: ' + mount)


def walk(root, stop=None):
    def error(exc):
        raise exc
    for directory, dirs, files in os.walk(root, onerror=error):
        if stop and stop.is_set():
            raise Stopped()
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('@eaDir', '$RECYCLE.BIN')
                   and not os.path.islink(os.path.join(directory, d))]
        yield directory, dirs, [f for f in files if not f.startswith('.')
            and f not in ('Thumbs.db', 'desktop.ini') and not os.path.islink(os.path.join(directory, f))]

class Observation:
    def __init__(self):
        self.reset()
    def reset(self):
        self.count, self.token, self.next = 0, None, 0
    def sample(self, valid, token, stamp, interval=300):
        if not valid:
            self.reset()
            return 0
        if self.token != token:
            self.reset()
            self.token = token
        if stamp >= self.next:
            self.count += 1
            self.next = stamp + interval
        return self.count

class FailureStreak:
    def __init__(self, threshold):
        self.threshold = threshold
        self.count = 0
        self.reported = False

    def failed(self):
        self.count += 1
        return self.count >= self.threshold

    def reset(self):
        reported = self.reported
        self.count = 0
        self.reported = False
        return reported


class Controller:
    def __init__(self, config, stop):
        self.c, self.stop = config, stop
        self.events = queue.SimpleQueue()
        self.source, self.target = config['directories']['source'], config['directories']['target']
        self.connection = {k: config['mysql'][k] for k in ('host', 'port', 'user')}
        self.connection['password'] = config['mysql']['password']
        self.database = config['mysql']['database']
        self.telemetry = Telemetry(Path(os.environ.get('PICSYNC_TELEMETRY', HERE.parent / 'state/status/runtime.json')))
        self.publish('controller', state='starting', started_at=time.time(), error=None)
        self.db = self.connect()
        self.db.execute('''CREATE TABLE IF NOT EXISTS controller_notifications (
            event_key VARCHAR(180) PRIMARY KEY, sent_at DOUBLE NOT NULL)''')
        self.batches = BatchStore(self.db)
        self.batches.migrate()
        self.key = config['bark']['key']
        self.sync = None
        self.phone = Phone(SimpleNamespace(**config['pixel']))
        self.device_lock = 'phone-' + hashlib.sha256(
            (config['pixel']['container'] + config['pixel']['serial']).encode()).hexdigest()[:16] + '.lock'
        self.observation = Observation()
        self.round = self.batches.active()
        if self.round:
            self.round['observations'] = 0
            self.save()
        self.needs_recovery = True

    def publish(self, section, **values):
        telemetry = getattr(self, 'telemetry', None)
        if telemetry is not None:
            try:
                telemetry.update(section, **values)
            except Exception:
                log.warning('Dashboard status export failed')

    def scan_progress(self, stats, complete):
        log.info('source scan complete=%s stats=%s', complete, stats)
        self.publish('scan', state='complete' if complete else 'scanning', stats=stats,
                     finished_at=time.time() if complete else None, error=None)

    def connect(self):
        return Inventory(self.database, **self.connection)

    def notify(self, event, body, repeat=False, db=None):
        db = db or self.db
        settings = self.c['bark']
        if not settings['enabled'] or not self.key:
            return
        key = event[:180]
        rows = db.query('SELECT sent_at FROM controller_notifications WHERE event_key=%s', (key,))
        if rows and (not repeat or time.time() - rows[0]['sent_at'] < settings['error_interval']):
            return True
        for attempt in range(settings['retries'] + 1):
            try:
                response = requests.post('https://api.day.app/' + self.key,
                    json={'title': '照片自动备份', 'body': body}, timeout=settings['timeout'])
                response.raise_for_status()
                if response.json().get('code') != 200:
                    raise ValueError('Push response')
                db.execute('REPLACE INTO controller_notifications VALUES (%s,%s)', (key, time.time()))
                return True
            except Exception:
                log.warning('Bark notification failed attempt=%d', attempt + 1)

    def alert(self, event, body):
        batch = self.round if self.round and self.round['phase'] != 'complete' else None
        prefix = f"批次：{batch['id']}\n阶段：{PHASES.get(batch['phase'], batch['phase'])}\n" if batch else '当前无活动批次\n'
        key = (batch['id'] + '-' if batch else '') + event
        telemetry = getattr(self, 'telemetry', None)
        if telemetry is not None:
            with suppress(Exception):
                telemetry.event(event, body, batch['id'] if batch else None)
        return self.notify(key, prefix + body, repeat=True)

    @contextmanager
    def device(self):
        try:
            with lock(self.device_lock):
                yield
        except BlockingIOError:
            raise PhoneUnavailable('手机正被其他操作占用') from None

    def phone_call(self, operation, *args, **kwargs):
        try:
            return getattr(self.phone, operation)(*args, **kwargs)
        except Exception as exc:
            raise PhoneUnavailable('手机连接或相册界面异常：' + type(exc).__name__) from None

    def refresh_battery(self):
        # Battery gates new links only; cleanup retains its independent evidence gates.
        if time.monotonic() < getattr(self, 'next_battery_check', 0):
            return
        self.next_battery_check = time.monotonic() + 60
        try:
            with self.device():
                state = self.phone_call('battery')
            level = state.get('level')
            self.mapping_battery = level if type(level) is int and 0 <= level <= 100 else None
            self.publish('battery', **state, state='ok', error=None, measured_at=time.time())
        except Exception as exc:
            self.mapping_battery = None
            self.publish('battery', state='error', error=type(exc).__name__)
        self.battery_gate_ready = False
        try:
            self.battery_notice()
        except Exception:
            log.warning('Battery notification failed; retry next battery check')

    def battery_notice(self):
        level = getattr(self, 'mapping_battery', None)
        threshold = self.c['mapping']['min_battery_percent']
        resume = self.c['mapping']['resume_battery_percent']
        if level is None:
            return
        if not hasattr(self, 'battery_paused'):
            # Include the old notification key when upgrading from the single threshold.
            rows = self.db.query('SELECT event_key FROM controller_notifications WHERE event_key IN (%s,%s)',
                                 ('mapping-battery-paused', 'mapping-low-battery'))
            self.battery_paused = bool(rows)
        if level < threshold:
            self.battery_paused = True
        if self.battery_paused and level < resume:
            # Persist the latch before notification, even if Bark is disabled or fails.
            self.db.execute('REPLACE INTO controller_notifications VALUES (%s,%s)',
                            ('mapping-battery-paused', time.time()))
            self.battery_gate_ready = True
            self.notify('mapping-low-battery',
                        f'手机电量低：{level}%，已触发低于 {threshold}% 的暂停。新增硬链接已暂停；'
                        f'电量达到 {resume}% 后自动继续（充电时同样适用）。')
        else:
            if self.battery_paused:
                self.db.execute('DELETE FROM controller_notifications WHERE event_key IN (%s,%s)',
                                ('mapping-battery-paused', 'mapping-low-battery'))
            self.battery_paused = False
            self.battery_gate_ready = True

    def photos_status(self, status):
        self.publish('photos', **status)
        if status['state'] not in ('complete', 'syncing'):
            detail = '；'.join(status.get('details', []))[:300]
            raise PhoneUnavailable('手机相册异常：' + status.get('message', status['state']) + ('；' + detail if detail else ''))
        return status

    def sync_status(self):
        try:
            if self.sync is None:
                cfg = self.c['syncthing']
                self.sync = SyncthingState(cfg['config'], cfg['folder'], cfg['device'], cfg['timeout'])
            state = self.sync.settled_state()
        except Exception as exc:
            self.publish('syncthing', ready=False, connected=None, error=type(exc).__name__)
            raise SyncthingUnavailable('Syncthing 接口或配置异常：' + type(exc).__name__) from None
        self.publish('syncthing', **state, progress_checked_at=time.time())
        if state.get('connected') is False:
            raise SyncthingUnavailable('Syncthing 与 Pixel 的连接已断开')
        if state.get('pull_errors') or state.get('error') or state.get('local_state') == 'error':
            raise SyncthingUnavailable('Syncthing 文件夹报错或拉取失败；已暂停处理')
        if state.get('remote_state') in ('invalid', 'error', 'paused'):
            raise SyncthingUnavailable('Syncthing Pixel 文件夹异常：' + state['remote_state'])
        return state

    def save(self, phase=None):
        previous = copy.deepcopy(self.round)
        observation = dict(self.observation.__dict__)
        try:
            transition(self.round, phase, time.time())
            if self.round['phase'] == 'complete' and 'summary' not in self.round:
                rows = self.db.query("SELECT source_path FROM deletion_events WHERE origin='manual' AND state='deleted' "
                    "AND JSON_UNQUOTE(JSON_EXTRACT(evidence,'$.run_id'))=%s", (self.round['id'],))
                manual = len({row['source_path'] for row in rows if row['source_path'] in self.round['members']})
                self.round['summary'] = summary(self.round, manual)
            if phase:
                self.observation.reset()
            self.round['observations'] = self.observation.count
            self.round['next_check'] = self.observation.next
            self.batches.save(self.round)
        except BaseException:
            self.round = previous
            self.observation.__dict__.update(observation)
            raise
        self.publish('controller', batch_id=self.round['id'], phase=self.round['phase'], observations=self.observation.count)
        log.info('round=%s phase=%s observations=%d', self.round['id'], self.round['phase'], self.observation.count)

    def flush_batch_reports(self):
        # Completion and its report are committed together. Failed delivery is
        # retried on later ticks or process restart, even after the next batch starts.
        if not self.c['bark']['enabled'] or not self.key:
            return
        rows = self.db.query("""SELECT r.id,r.summary FROM backup_batches r
            LEFT JOIN controller_notifications n ON n.event_key=CONCAT(r.id,'-summary')
            WHERE r.phase='complete' AND r.summary IS NOT NULL
            AND n.event_key IS NULL ORDER BY r.updated_at LIMIT 10""")
        for row in rows:
            report = json.loads(row['summary']) if isinstance(row['summary'], str) else row['summary']
            self.notify(row['id'] + '-summary', report['body'])

    def confirm_batch_backup(self, current):
        # Only unchanged frozen members are attributed to this batch's proof.
        self.round.setdefault('backup_confirmed', {}).update({
            name: value for name, value in current.items()
            if name in self.round['members'] and value == self.round['members'][name]
            and name not in self.round.get('deferred', [])})

    def reconcile_intents(self):
        # Never retry an old unlink. Existing entries return to inventory; absent
        # entries retain intent evidence, but absence cannot prove who unlinked.
        rows = self.db.query("SELECT id,source_path,target_path FROM deletion_events WHERE state='intent'")
        with TargetDirectory(self.source, self.target) as guard:
            for row in rows:
                try:
                    guard.stat(row['target_path'])
                    present = True
                except FileNotFoundError:
                    present = False
                self.db.execute("UPDATE deletion_events SET state=%s,error_message=%s,finished_at=%s WHERE id=%s",
                    ('error' if present else 'unknown', 'reconciled_after_restart', now(), row['id']))
                self.db.execute("UPDATE files SET status=%s,deletion_origin='unknown',deletion_reason=%s,"
                    "deleted_at=%s,last_error=%s WHERE source_key=%s AND BINARY source_path=BINARY %s",
                    ('linked' if present else 'deleted', None if present else 'unresolved_manual_intent',
                     None if present else now(), 'Recovered unresolved deletion intent',
                     path_key(row['source_path']), row['source_path']))

    def scanner(self):
        db = None
        failures = FailureStreak(self.c['bark']['error_threshold'])
        while not self.stop.is_set():
            try:
                db = db or self.connect()
                ensure_storage(self.source)
                os.listdir(self.source)
                scan_id = uuid.uuid4().hex
                self.publish('scan', state='scanning', scan_id=scan_id, started_at=time.time(), finished_at=None, stats={}, error=None)
                db.scan_source(self.source, self.target,
                    lambda root: walk(root, self.stop), stop_event=self.stop, progress_every=self.c['scan']['progress_every'],
                    progress=self.scan_progress,
                    on_changes=lambda stats: log.info('source scan=%s new_pending=%d', scan_id, stats['new_pending']))
                if failures.reset():
                    self.events.put(('source-recovered', '源目录扫描已恢复'))
            except Stopped:
                break
            except Exception as exc:
                if self.stop.is_set():
                    break
                self.publish('scan', state='error', error=type(exc).__name__)
                log.error('Source scan failed: %s; reconnect next interval', type(exc).__name__)
                if failures.failed():
                    if isinstance(exc, StorageUnavailable):
                        self.events.put(('source-error-StorageUnavailable', str(exc)))
                    else:
                        self.events.put(('source-error-' + type(exc).__name__, '源扫描异常：' + type(exc).__name__))
                    failures.reported = True
                if db:
                    with suppress(Exception):
                        db.close()
                    db = None
            if self.stop.wait(self.c['scan']['interval']):
                break
        if db:
            db.close()

    def snapshot(self):
        return snapshot_targets(self.target, set(), fingerprint)

    def batch_snapshot(self):
        result = {}
        with TargetDirectory(self.source, self.target) as guard:
            for name in self.round['members']:
                try:
                    value = guard.stat(name)
                    result[name] = [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns]
                except FileNotFoundError:
                    pass
        return result

    def scan_target(self):
        return self.db.scan_target(self.source, self.target, walk)

    def reconcile_batch(self):
        current = self.batch_snapshot()
        deferred = set(self.round.get('deferred', []))
        deferred.update(name for name, value in current.items() if value != self.round['members'][name])
        self.round['deferred'] = sorted(deferred)
        missing = set(self.round['members']) - set(current)
        for name in missing:
            self.db.execute("UPDATE files SET status='deleted',deleted_at=%s "
                "WHERE source_key=%s AND BINARY source_path=BINARY %s AND status='linked'",
                (now(), path_key(name), name))
        if missing:
            self.db.classify_deletions(self.target, self.sync.remote_deletion, relatives=missing)
        return current

    def new_round(self):
        self.scan_target()
        members = self.snapshot()
        owners = self.db.batch_owners()
        if any(name in owners for name in members):
            # Existing owned targets must be handled by their original round;
            # never adopt them into a new round, even if their bytes changed.
            raise ValueError('Target file already belongs to an earlier batch; requires review')
        candidates = []
        size = sum(value[2] for value in members.values())
        # Freeze all candidates BEFORE creating any link. Recovery never selects
        # additional files for this round.
        if not members:
            after = None
            while True:
                rows = self.db.pending(self.source, after, unassigned_only=True)
                if not rows:
                    break
                for row in rows:
                    after = (row['mtime_ns'], row['source_path'])
                    if size + row['size_bytes'] <= self.c['mapping']['max_bytes']:
                        candidates.append({k: row[k] for k in ('source_path', 'mtime_ns', 'source_dev', 'source_ino', 'size_bytes')})
                        size += row['size_bytes']
        if not members and not candidates:
            return False
        stamp = time.time()
        self.round = dict(id=uuid.uuid4().hex, phase='mapping', members=members, candidates=candidates,
                          started_at=stamp, phase_started_at=stamp, phase_seconds={})
        try:
            self.save()
        except BaseException:
            self.round = None
            raise
        return True

    def mapping_allowed(self):
        self.refresh_battery()
        level = getattr(self, 'mapping_battery', None)
        allowed = (level is not None and getattr(self, 'battery_gate_ready', False)
                   and not getattr(self, 'battery_paused', True))
        self.publish('mapping', paused=not allowed, battery_level=level,
                     min_battery_percent=self.c['mapping']['min_battery_percent'],
                     resume_battery_percent=self.c['mapping']['resume_battery_percent'],
                     reason=None if allowed else ('battery_unknown' if level is None else 'low_battery'))
        return allowed

    def map_round(self):
        for row in self.round['candidates']:
            if self.stop.is_set():
                raise Stopped()
            if not self.mapping_allowed():
                self.save()
                return False
            relative = row['source_path']
            source, target = file_path(self.source, relative), file_path(self.target, relative)
            try:
                value = os.lstat(source)
            except FileNotFoundError:
                self.round.setdefault('mapping_skipped', {})[relative] = 'source_missing'
                self.save()
                continue
            if not stat.S_ISREG(value.st_mode) or (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns) != (
                    row['source_dev'], row['source_ino'], row['size_bytes'], row['mtime_ns']):
                continue
            # Validate every existing ancestor before mkdir/link to reject symlinks.
            parent = Path(self.target)
            for part in Path(relative).parts[:-1]:
                parent = parent / part
                parent.mkdir(exist_ok=True)
                if parent.is_symlink() or not parent.is_dir():
                    raise ValueError('Unsafe link parent')
            if not os.path.lexists(target):
                with TargetDirectory(self.source, self.target) as guard:
                    guard.link_source(relative, (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns))
            if os.path.islink(target) or not os.path.samefile(source, target):
                raise ValueError('Target conflict')
            self.round['members'][relative] = [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns]
            self.db.record_results([('linked', now(), None, relative)])
            self.save()
        self.save('sync' if self.round['members'] else 'complete')
        if not self.round['members']:
            self.flush_batch_reports()
        return True

    def tick(self):
        ensure_storage(self.source)
        self.publish('storage', state='mounted', error=None)
        before = self.sync_status()
        if getattr(self, 'needs_recovery', False):
            self.reconcile_intents()
            self.needs_recovery = False
        with TargetDirectory(self.source, self.target):
            pass
        if not self.round or self.round['phase'] == 'complete':
            if not self.mapping_allowed():
                return min(60, self.c['monitor']['interval'])
            if not self.new_round():
                return self.c['monitor']['idle_interval']
        phase = self.round['phase']
        if phase == 'mapping':
            return 1 if self.map_round() else min(60, self.c['monitor']['interval'])
        if phase == 'sync' or (phase == 'backup' and not before['ready']):
            # Photos uploads received files while Syncthing is still delivering
            # the batch. This is telemetry only: never confirm the whole batch
            # or authorize release from this partial-device observation.
            with self.device():
                status = self.photos_status(self.phone_call('status'))
            log.info('Photos during transfer state=%s', status['state'])
        if phase == 'sync' and not before['ready']:
            # A full NAS member scan cannot authorize anything while transfer is
            # incomplete. Defer it until the ready gate, keeping device/status
            # polling responsive without weakening the later fingerprint check.
            self.observation.reset()
            self.save()
            return self.c['monitor']['interval']
        current_members = self.reconcile_batch()
        log.info('round=%s phase=%s sync=%s', self.round['id'], phase, before)
        if not before['ready']:
            self.observation.reset()
            self.save()
            return self.c['monitor']['interval']
        if phase == 'sync':
            self.save('backup')
        elif phase == 'backup':
            with self.device():
                status = self.photos_status(self.phone_call('status'))
            log.info('Photos state=%s', status['state'])
            if status['state'] == 'complete':
                self.confirm_batch_backup(current_members)
                self.save('release')
        elif phase == 'release':
            # Persist ambiguous UI operations before clicking. Restart waits for
            # fresh positive no-space evidence rather than clicking blindly.
            self.save('release_uncertain')
            with self.device():
                result = self.phone_call('release_space', execute=True)
            self.round['release'] = result
            self.save('deletions' if result['action'] in ('freed', 'nothing_to_free') else 'release_uncertain')
            if result['action'] == 'verification_required':
                raise PhoneUnavailable('相册释放结果不明确，已暂停清理，等待复核')
            if result.get('status'):
                self.photos_status(result['status'])
        elif phase == 'release_uncertain':
            with self.device():
                root = self.phone_call('menu')
                if self.photos_status(backup_status(root))['state'] == 'complete':
                    _, button = self.phone_call('free_page', root)
                    if button is None:
                        self.save('deletions')
        elif phase in ('deletions', 'observe'):
            current = self.batch_snapshot()
            remaining = {k: v for k, v in current.items() if k in self.round['members']}
            self.round['removed_count'] = len(self.round['members']) - len(remaining)
            if not remaining:
                self.save('final_sync')
                return 1
            with self.device():
                root = self.phone_call('menu')
                complete = self.photos_status(backup_status(root))['state'] == 'complete'
                button = True
                if complete:
                    _, button = self.phone_call('free_page', root)
                after = self.sync_status()
                fresh = self.batch_snapshot()
                valid = complete and button is None and after['ready'] and before['token'] == after['token'] and current == fresh
                token = [before['token'], current]
                if not valid or (self.observation.token is not None and self.observation.token != token):
                    log.info('Observation reset: aggregate gates failed or sync/file snapshot changed')
                count = self.observation.sample(valid, token, time.time(), self.c['observation']['interval'])
                if valid:
                    self.confirm_batch_backup(fresh)
                # Preserve the current samples when entering the observation phase.
                transition(self.round, 'observe', time.time())
                self.save()
                if count >= self.c['observation']['confirmations']:
                    allowed = {k: v for k, v in remaining.items()
                               if v == self.round['members'][k] and k not in self.round.get('deferred', [])}
                    evidence = dict(state='aggregate_confirmed', run_id=self.round['id'], checked_at=now(),
                        backup_complete=True, nothing_to_free=True, sync_ready=True,
                        sync_token_before=before['token'], sync_token_after=after['token'])
                    self.save('cleanup')
                    results = self.db.delete_relaxed_targets(self.source, self.target, allowed, evidence)
                    self.round['cleanup_results'] = results
                    self.save('final_sync')
        elif phase == 'cleanup':
            # Interrupted cleanup requires fresh observations, never replay evidence.
            self.reconcile_intents()
            self.save('observe')
        elif phase == 'final_sync':
            present = self.batch_snapshot()
            if any(name in present and name not in self.round.get('deferred', [])
                   for name in self.round['members']):
                self.save('observe')
            else:
                self.save('complete')
                self.flush_batch_reports()
        return self.c['monitor']['interval']

    def run(self, once=False):
        worker = threading.Thread(target=self.scanner, name='source-scanner', daemon=True)
        worker.start()
        failure = None
        failures = FailureStreak(self.c['bark']['error_threshold'])
        source_reported = False
        try:
            while not self.stop.is_set():
                tick_started = time.monotonic()
                try:
                    self.publish('controller', state='checking')
                    self.refresh_battery()
                    self.flush_batch_reports()
                    while not self.events.empty():
                        event, body = self.events.get_nowait()
                        if event == 'source-recovered':
                            if source_reported:
                                self.alert(event, body)
                            source_reported = False
                        else:
                            sent = self.alert(event, body)
                            if event.startswith('source-error-'):
                                source_reported = bool(sent) or source_reported
                    delay = self.tick()
                    self.publish('controller', state='waiting', error=None, next_check_at=time.time() + delay)
                    if failure:
                        if failures.reset():
                            self.alert('recovered-' + failure, '异常已恢复：' + failure)
                        log.info('Controller recovered from %s', failure)
                        failure = None
                except Stopped:
                    break
                except Exception as exc:
                    self.observation.reset()
                    failure = type(exc).__name__
                    notify_error = failures.failed()
                    log.error('Controller failed: %s', failure)
                    known = isinstance(exc, (StorageUnavailable, PhoneUnavailable, SyncthingUnavailable))
                    message = str(exc) if known else ('数据库异常：' if isinstance(exc, DatabaseError) else
                        '目录或挂载访问异常：' if isinstance(exc, OSError) else '控制器异常：') + failure
                    self.publish('controller', state='error', error=message, error_type=failure)
                    if isinstance(exc, StorageUnavailable):
                        self.publish('storage', state='error', error=message)
                    if isinstance(exc, PhoneUnavailable):
                        self.publish('photos', state='error', message=message)
                    try:
                        if not known:
                            with suppress(Exception):
                                self.db.close()
                            self.db = self.connect()
                            self.batches = BatchStore(self.db)
                        if notify_error:
                            failures.reported = bool(self.alert('error-' + failure, '异常：' + message)) or failures.reported
                    except Exception:
                        log.error('Exception reporting or database reconnect failed')
                    delay = self.c['monitor']['interval']
                # Keep the configured polling cadence inclusive of API time.
                delay = max(0, delay - (time.monotonic() - tick_started))
                if once or self.stop.wait(delay):
                    break
        finally:
            self.publish('controller', state='stopped')
            self.stop.set()
            worker.join(timeout=5)
            self.db.close()

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=str(os.environ.get('PICSYNC_CONFIG', HERE / 'config.toml')))
    sub = parser.add_subparsers(dest='command')
    sub.add_parser('once', help='Execute one controller step')
    sub.add_parser('dry-run', help='Read-only dependency and inventory checks')
    sub.add_parser('migrate', help='Migrate stored paths')
    query = sub.add_parser('query')
    query.add_argument('--status')
    query.add_argument('--filter')
    args = parser.parse_args()
    config = load(args.config)
    if args.command not in ('query', 'migrate'):
        if not config['pixel']['serial'] or not config['syncthing']['device']:
            parser.error('Configure the ADB serial and Syncthing device ID before running')
    path = Path(config['logging']['path'])
    if not path.is_absolute():
        path = HERE / path
    path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=config['logging']['level'], format='%(asctime)s %(threadName)s %(message)s',
        handlers=[RotatingFileHandler(path, maxBytes=config['logging']['max_bytes'], backupCount=config['logging']['backups'])])
    # HTTP debug request lines contain the Bark key in the URL path.
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('requests').setLevel(logging.WARNING)
    log.info('Effective configuration: %s', json.dumps(redacted(config)))
    stop = threading.Event()
    faulthandler.register(signal.SIGUSR1)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    with lock('file-sync-controller.lock'):
        if args.command == 'dry-run':
            with TargetDirectory(**config['directories']):
                pass
            sync = config['syncthing']
            print(json.dumps(SyncthingState(sync['config'], sync['folder'], sync['device'], sync['timeout']).settled_state()))
            return
        if args.command in ('query', 'migrate'):
            options = {key: config['mysql'][key] for key in ('host', 'port', 'user')}
            options['password'] = config['mysql']['password']
            db = Inventory(config['mysql']['database'], **options)
            try:
                if args.command == 'query':
                    print(json.dumps(db.list_files(args.filter, args.status), default=str, ensure_ascii=False))
                else:
                    print(db.migrate_relative(config['directories']['source'], config['directories']['target']))
            finally:
                db.close()
        else:
            controller = Controller(config, stop)
            controller.run(args.command == 'once')

if __name__ == '__main__':
    main()
