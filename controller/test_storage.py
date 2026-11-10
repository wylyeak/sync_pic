"""MySQL integration tests; opt in with FILE_SYNC_TEST_MYSQL=1.

Each test creates and drops its own file_sync_test_<uuid> database.
"""
import os
import json
import subprocess
import sys
import runpy
import tempfile
import unittest
import uuid
from unittest.mock import patch, Mock

from storage import Inventory, path_key, HashCollisionError, relative_path, file_path, now


class RelativePathTest(unittest.TestCase):
    def test_hash_is_readable_hex(self):
        key = path_key('照片/file.jpg')
        self.assertIsInstance(key, str)
        self.assertRegex(key, r'^[0-9a-f]{64}$')
        self.assertEqual(len(bytes.fromhex(key)), 32)

    def test_join_and_validation(self):
        self.assertEqual(relative_path('/source/sub/file', '/source'), 'sub/file')
        self.assertEqual(file_path('/new-root', 'sub/file'), '/new-root/sub/file')
        for path in ('/absolute', '../outside', 'sub/../../outside', 'sub/../file', '.'):
            with self.assertRaises(ValueError):
                file_path('/root', path)
        with self.assertRaises(ValueError):
            relative_path('/elsewhere/file', '/source')


@unittest.skipUnless(os.environ.get('FILE_SYNC_TEST_MYSQL') == '1', 'MySQL integration tests are opt-in')
class InventoryTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.source, self.target = [os.path.join(self.root.name, n) for n in ('source', 'target')]
        os.mkdir(self.source)
        os.mkdir(self.target)
        self.database = 'file_sync_test_' + uuid.uuid4().hex
        self.settings = dict(host=os.environ.get('FILE_SYNC_MYSQL_HOST', '127.0.0.1'),
                             port=int(os.environ.get('FILE_SYNC_MYSQL_PORT', '3306')),
                             user=os.environ.get('FILE_SYNC_MYSQL_USER', 'root'),
                             password=os.environ['FILE_SYNC_MYSQL_PASSWORD'])
        self.db = Inventory(self.database, **self.settings)
        self.addCleanup(self.cleanup_database)

    def cleanup_database(self):
        try:
            self.db.execute('DROP DATABASE `{}`'.format(self.database))
        finally:
            self.db.close()

    def create(self, name, contents=b'abc', stamp=None):
        path = os.path.join(self.source, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(contents)
        if stamp is not None:
            os.utime(path, (stamp, stamp))
        return path

    def scan(self):
        return self.db.scan_source(self.source, self.target, os.walk)

    def row(self, path):
        return self.db.query('SELECT * FROM files WHERE source_key=%s', (path_key(os.path.relpath(path, self.source)),))[0]

    def test_capture_metadata_backfill_cache_and_changed_file(self):
        path = self.create('photo.jpg')
        with patch('storage.capture_time', return_value='2020-01-02T03:04:05') as read, patch('storage.creation_time', return_value='2021-02-03T04:05:06+00:00'):
            self.scan()
            self.assertEqual(self.row(path)['captured_at'], '2020-01-02T03:04:05')
            self.scan()
            self.assertEqual(read.call_count, 1)
            self.db.execute('UPDATE files SET capture_fingerprint=NULL')
            self.scan()
            self.assertEqual(read.call_count, 2)
        with open(path, 'ab') as stream:
            stream.write(b'changed')
        with patch('storage.capture_time', return_value=None), patch('storage.creation_time', return_value='2021-02-03T04:05:06+00:00'):
            self.scan()
        row = self.row(path)
        self.assertIsNone(row['captured_at'])
        self.assertEqual(row['file_created_at'], '2021-02-03T04:05:06+00:00')

    def test_lifecycle_and_record_deletion(self):
        path = self.create('file')
        self.scan()
        self.assertEqual(self.db.pending(self.source)[0]['size_bytes'], 3)
        dest = os.path.join(self.target, 'file')
        os.link(path, dest)
        self.db.linked(os.path.relpath(path, self.source))
        stamp = self.row(path)['linked_at']
        os.unlink(dest)
        self.db.scan_target(self.source, self.target, os.walk)
        row = self.row(path)
        self.assertEqual(row['status'], 'deleted')
        self.assertIsNotNone(row['deleted_at'])
        self.assertEqual(row['linked_at'], stamp)
        self.assertEqual(self.db.pending(self.source), ())
        os.unlink(path)
        self.scan()
        self.assertEqual(self.row(path)['source_present'], 0)
        rows = self.db.list_files(keyword='file', status='deleted')
        self.assertEqual(len(rows), 1)
        self.db.delete_records(rows)
        self.assertEqual(len(self.db.list_files()), 0)

    def test_mtime_order_and_independent_connections(self):
        for name, stamp in [('new', 300), ('old', 100), ('middle', 200)]:
            self.create(name, stamp=stamp)
        self.scan()
        rows = self.db.pending(self.source, limit=2)
        self.assertEqual([os.path.basename(r['source_path']) for r in rows], ['old', 'middle'])
        rows = self.db.pending(self.source, (rows[-1]['mtime_ns'], rows[-1]['source_path']))
        self.assertEqual(os.path.basename(rows[0]['source_path']), 'new')
        other = Inventory(self.database, **self.settings)
        self.addCleanup(other.close)
        path = os.path.join(self.source, 'old')
        os.link(path, os.path.join(self.target, 'old'))
        other.scan_target(self.source, self.target, os.walk)
        self.assertEqual(self.row(path)['status'], 'linked')
        self.assertIsNone(self.row(path)['linked_at'])
        os.unlink(os.path.join(self.target, 'old'))
        self.scan()
        self.assertEqual(self.row(path)['status'], 'linked')
        other.scan_target(self.source, self.target, os.walk)
        self.assertEqual(self.row(path)['status'], 'deleted')

    def test_source_change_notifications_only_report_new_pending(self):
        events = []
        path = self.create('first')
        self.db.scan_source(self.source, self.target, os.walk, on_changes=events.append)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['new_pending'], 1)
        self.assertEqual(events[0]['new_pending_bytes'], 3)
        self.assertEqual(events[0]['pending_files'], 1)
        events.clear()
        self.db.scan_source(self.source, self.target, os.walk, on_changes=events.append)
        self.assertEqual(events, [])
        os.unlink(path)
        self.scan()
        self.create('first')
        self.db.scan_source(self.source, self.target, os.walk, on_changes=events.append)
        self.assertEqual(events[0]['new_pending'], 1)
        self.assertEqual(events[0]['new'], 0)  # Reappeared pending file.
        events.clear()
        self.create('partial', b'12345')
        def failed(top):
            yield top, [], ['partial']
            raise OSError('unavailable')
        with self.assertRaises(OSError):
            self.db.scan_source(self.source, self.target, failed, on_changes=events.append)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['new_pending_bytes'], 5)
        self.assertNotIn('pending_files', events[0])

    def controller(self):
        from file_sync import Controller, Observation
        from configuration import load
        value = Controller.__new__(Controller)
        value.mapping_allowed = Mock(return_value=True)
        value.c = load()
        value.c['bark']['enabled'] = False
        value.source, value.target = self.source, self.target
        value.db = self.db
        value.round = None
        value.observation = Observation()
        value.stop = __import__('threading').Event()
        value.sync = Mock()
        value.sync.remote_deletion = None
        value.key = 'test-key'
        self.db.execute('CREATE TABLE IF NOT EXISTS controller_rounds (id VARCHAR(32) PRIMARY KEY, phase VARCHAR(32), data LONGTEXT, updated_at VARCHAR(40))')
        self.db.execute('CREATE TABLE IF NOT EXISTS controller_notifications (event_key VARCHAR(180) PRIMARY KEY, sent_at DOUBLE)')
        from batch_store import BatchStore
        value.batches = BatchStore(self.db)
        value.batches.migrate()
        return value

    def test_batch_summary_retry_deduplication_and_successful_audit_counts(self):
        value = self.controller()
        value.c['bark']['enabled'] = True
        value.c['bark']['retries'] = 0
        batch_id = uuid.uuid4().hex
        value.round = dict(id=batch_id, phase='backup', members={'a': [1], 'b': [2]},
            started_at=100, phase_started_at=100, phase_seconds={}, backup_confirmed={'a': [1], 'b': [2]})
        for name, state in (('a', 'deleted'), ('b', 'error')):
            self.db.execute("INSERT INTO deletion_events (source_key,source_path,target_path,origin,state,evidence,created_at) "
                "VALUES (%s,%s,%s,'manual',%s,%s,%s)",
                (path_key(name), name, name, state, json.dumps({'run_id': batch_id}), now()))
        with patch('file_sync.time.time', return_value=200):
            value.save('complete')
        stored = value.batches.load(batch_id)
        self.assertEqual(stored['summary']['manual_deleted'], 1)
        self.assertEqual(stored['summary']['backed_up'], 2)
        self.assertEqual(stored['summary']['phase_seconds']['backup'], 100)
        import requests
        with patch('file_sync.requests.post', side_effect=requests.ConnectionError):
            value.flush_batch_reports()
        self.assertEqual(self.db.query('SELECT * FROM controller_notifications'), ())
        restored = self.controller()
        restored.c['bark']['enabled'] = True
        response = Mock()
        response.json.return_value = {'code': 200}
        with patch('file_sync.requests.post', return_value=response) as post:
            restored.flush_batch_reports()
            restored.flush_batch_reports()
        self.assertEqual(post.call_count, 1)
        body = post.call_args.kwargs['json']['body']
        self.assertIn('手动删除：1 个', body)
        self.assertIn('备份完成：2 个', body)

    def test_bark_messages_for_changes_and_no_duplicates(self):
        value = self.controller()
        value.c['bark']['enabled'] = True
        response = Mock()
        response.json.return_value = {'code': 200}
        with patch('file_sync.requests.post', return_value=response) as post:
            value.notify('round-mapped', 'mapped')
            value.notify('round-mapped', 'mapped')
            self.assertEqual(post.call_count, 1)
        restored = self.controller()
        restored.c['bark']['enabled'] = True
        with patch('file_sync.requests.post') as post:
            restored.notify('round-mapped', 'mapped')
            post.assert_not_called()

    def test_unresolved_intent_recovery_never_retries_unlink(self):
        source = self.create('photo')
        target = os.path.join(self.target, 'photo')
        os.link(source, target)
        self.scan()
        value = self.controller()
        for present in (True, False):
            self.db.execute("UPDATE files SET status='deleted',deletion_origin='manual',deleted_at=%s", (now(),))
            self.db.execute("INSERT INTO deletion_events (source_key,source_path,target_path,origin,state,created_at) "
                "VALUES (%s,'photo','photo','manual','intent',%s)", (path_key('photo'), now()))
            if not present:
                os.unlink(target)
            value.reconcile_intents()
            row = self.row(source)
            self.assertEqual(row['deletion_origin'], 'unknown')
            self.assertEqual(row['status'], 'linked' if present else 'deleted')
            self.assertTrue(os.path.isfile(source))
            self.assertEqual(os.path.exists(target), present)
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM deletion_events WHERE state='intent'")[0]['n'], 0)

    def test_batch_reconciliation_does_not_mark_other_targets_missing(self):
        for name in ('current', 'other'):
            source = self.create(name)
            os.link(source, os.path.join(self.target, name))
        self.scan()
        self.db.scan_target(self.source, self.target, os.walk)
        value = self.controller()
        from cleanup_target import fingerprint
        value.round = {'members': {'current': list(fingerprint(os.path.join(self.target, 'current')))}}
        value.sync.remote_deletion = Mock(return_value=None)
        os.unlink(os.path.join(self.target, 'current'))
        self.assertEqual(value.reconcile_batch(), {})
        self.assertEqual(self.row(os.path.join(self.source, 'current'))['status'], 'deleted')
        self.assertEqual(self.row(os.path.join(self.source, 'other'))['status'], 'linked')

    def test_stopped_scan_does_not_mark_missing(self):
        import threading
        self.create('retained')
        self.scan()
        stop = threading.Event()
        stop.set()
        with self.assertRaises(InterruptedError):
            self.db.scan_source(self.source, self.target, lambda root: [], stop_event=stop)
        self.assertEqual(self.row(os.path.join(self.source, 'retained'))['source_present'], 1)

    def test_scoped_deletion_classification_does_not_check_history(self):
        for name in ('historical', 'current'):
            source = self.create(name)
            os.link(source, os.path.join(self.target, name))
        self.scan()
        self.db.scan_target(self.source, self.target, os.walk)
        for name in ('historical', 'current'):
            os.unlink(os.path.join(self.target, name))
        self.db.scan_target(self.source, self.target, os.walk)
        classifier = Mock(return_value=None)
        self.db.classify_deletions(self.target, classifier, relatives={'current'})
        classifier.assert_called_once_with('current')

    def test_progress_and_failed_scan(self):
        for n in range(105):
            self.create(str(n))
        self.scan()
        for n in range(105, 205):
            self.create(str(n))
        events = []
        observer = Inventory(self.database, **self.settings)
        self.addCleanup(observer.close)
        def report(stats, done):
            # Every reported file is already visible to a separate connection.
            self.assertEqual(len(observer.list_files()), 105 + stats['new'])
            events.append((stats, done))
        stats = self.db.scan_source(self.source, self.target, os.walk,
                                   progress=report)
        self.assertEqual([s['files'] for s, done in events if not done], [100, 200])
        self.assertEqual(stats, {'files': 205, 'size_bytes': 615, 'existing': 105, 'new': 100, 'conflicts': 0,
                                 'new_pending': 100, 'new_pending_bytes': 300,
                                 'pending_files': 205, 'pending_bytes': 615})
        self.assertTrue(events[-1][1])
        self.create('extra')
        def failed(top):
            yield top, [], ['extra']
            self.assertEqual(len(observer.list_files()), 206)
            yield top, [], [str(n) for n in range(99)]
            raise OSError('unavailable')
        with self.assertRaises(OSError):
            self.db.scan_source(self.source, self.target, failed)
        self.assertEqual(len(self.db.list_files()), 206)
        self.assertTrue(all(r['source_present'] for r in self.db.list_files()))

    def test_source_collision_never_overwrites_or_mutates(self):
        original = self.create('original')
        self.scan()
        original_row = self.row(original)
        other = self.create('other', contents=b'different')
        real_key = path_key
        def collide(path):
            return real_key('original') if path == 'other' else real_key(path)
        with patch('storage.path_key', side_effect=collide):
            self.create('after_collision')
            stats = self.scan()
            self.assertEqual(stats['conflicts'], 1)
            self.db.linked('other')
            self.db.delete_records([{'source_path': 'other', 'source_key': real_key('original')}])
        row = self.row(original)
        for field in ('source_path', 'target_path', 'size_bytes', 'status', 'first_seen_at'):
            self.assertEqual(row[field], original_row[field])
        self.assertEqual(len(self.db.list_files()), 2)
        conflicts = self.db.query('SELECT * FROM hash_conflicts')
        self.assertEqual(len(conflicts), 3)
        for conflict in conflicts:
            self.assertEqual(conflict['conflict_type'], 'source')
            self.assertEqual(conflict['existing_path'], 'original')
            self.assertEqual(conflict['incoming_path'], 'other')
            self.assertIsNotNone(conflict['detected_at'])

    def test_relative_paths_survive_root_changes(self):
        original = self.create('nested/file')
        self.scan()
        self.db.linked('nested/file')
        before = self.row(original)
        self.assertEqual(before['source_path'], 'nested/file')
        self.assertEqual(before['target_path'], 'nested/file')
        self.assertEqual(before['source_key'], path_key('nested/file'))
        self.assertEqual(before['target_key'], path_key('nested/file'))
        self.assertEqual(before['source_root'], '.')
        self.assertEqual(before['root_key'], path_key('.'))
        moved_source, moved_target = self.source + '_moved', self.target + '_moved'
        os.rename(self.source, moved_source)
        os.rename(self.target, moved_target)
        self.db.scan_source(moved_source, moved_target, os.walk)
        after = self.db.list_files()[0]
        for column in ('source_key', 'target_key', 'root_key', 'source_path', 'target_path',
                       'source_root', 'status', 'linked_at', 'first_seen_at'):
            self.assertEqual(after[column], before[column])
        self.assertEqual(len(self.db.list_files()), 1)

    def test_legacy_migration_preserves_history(self):
        original = self.create('nested/file')
        self.scan()
        self.db.linked('nested/file')
        before = self.row(original)
        dest = os.path.join(self.target, 'nested/file')
        self.db.execute("""UPDATE files SET source_key=%s,source_path=%s,root_key=%s,
            source_root=%s,target_key=%s,target_path=%s,last_error=%s""",
            (path_key(original), original, path_key(self.source), self.source,
             path_key(dest), dest, 'Cannot access ' + original))
        self.assertEqual(self.db.migrate_relative(self.source, self.target), 1)
        after = self.row(original)
        for column in ('source_path', 'target_path', 'source_key', 'root_key', 'target_key',
                       'source_root', 'status', 'linked_at', 'first_seen_at', 'mtime_ns'):
            self.assertEqual(after[column], before[column])
        self.assertEqual(after['last_error'], 'Cannot access nested/file')
        self.assertEqual(self.db.migrate_relative(self.source, self.target), 0)

    def test_migration_conflicts_preserve_snapshot_and_continue(self):
        for name in ('original', 'other', 'after'):
            self.create(name)
        self.scan()
        rows = self.db.list_files()
        for row in rows:
            path = os.path.join(self.source, row['source_path'])
            dest = os.path.join(self.target, row['target_path'])
            self.db.execute("""UPDATE files SET source_key=%s,source_path=%s,
                source_root=%s,root_key=%s,target_path=%s,target_key=%s WHERE source_key=%s""",
                (path_key(path), path, self.source, path_key(self.source), dest, path_key(dest), row['source_key']))
        real_key = path_key
        def collide(path):
            return real_key('original') if path == 'other' else real_key(path)
        with patch('storage.path_key', side_effect=collide):
            self.assertEqual(self.db.migrate_relative(self.source, self.target), 2)
        retained = self.db.list_files()
        self.assertIn('after', [row['source_path'] for row in retained])
        conflicts = self.db.query("SELECT * FROM hash_conflicts WHERE operation='migration'")
        self.assertEqual(len(conflicts), 1)
        snapshot = json.loads(conflicts[0]['details'])
        self.assertIn(snapshot['source_path'], ('original', 'other'))
        self.assertEqual(snapshot['source_root'], '.')
        self.assertEqual(snapshot['status'], 'pending')
        self.assertEqual(snapshot['size_bytes'], 3)
        self.assertFalse(os.path.isabs(conflicts[0]['incoming_path']))

    def test_legacy_migration_rejects_wrong_roots_without_changes(self):
        original = self.create('file')
        self.scan()
        self.db.execute('UPDATE files SET source_key=%s,source_path=%s,source_root=%s',
                        (path_key(original), original, self.source))
        before = self.db.list_files()[0]
        with self.assertRaises(ValueError):
            self.db.migrate_relative(self.source + '_wrong', self.target)
        self.assertEqual(self.db.list_files()[0], before)

    def test_target_collision_stops_source_insert_and_target_updates(self):
        original = self.create('original')
        self.scan()
        dest = os.path.join(self.target, 'original')
        other = self.create('other')
        other_dest = os.path.join(self.target, 'other')
        real_key = path_key
        def collide(path):
            return real_key('original') if path == 'other' else real_key(path)
        with patch('storage.path_key', side_effect=collide):
            self.assertEqual(self.scan()['conflicts'], 1)
        self.assertEqual(len(self.db.list_files()), 1)
        self.db.linked('original')
        before = self.row(original)
        os.link(other, other_dest)
        with patch('storage.path_key', side_effect=collide):
            self.db.scan_target(self.source, self.target, os.walk)
        self.assertEqual(self.row(original), before)
        self.create('unrelated')
        self.scan()
        os.link(os.path.join(self.source, 'unrelated'), os.path.join(self.target, 'unrelated'))
        # Colliding targets are skipped while unrelated entries still update.
        os.link(original, dest)
        with patch('storage.path_key', side_effect=collide):
            self.db.scan_target(self.source, self.target, os.walk)
        for column in ('status', 'linked_at', 'deleted_at'):
            self.assertEqual(self.row(original)[column], before[column])
        self.assertEqual(self.row(os.path.join(self.source, 'unrelated'))['status'], 'linked')
        target_conflicts = self.db.query("SELECT * FROM hash_conflicts WHERE conflict_type='target'")
        self.assertTrue(target_conflicts)
        self.assertTrue(all(not os.path.isabs(r['incoming_path']) for r in target_conflicts))

    def test_frozen_batch_does_not_take_new_scan_records(self):
        self.create('first', stamp=100)
        self.scan()
        value = self.controller()
        self.assertTrue(value.new_round())
        self.create('later', stamp=200)
        self.scan()
        value.map_round()
        self.assertEqual(set(value.round['members']), {'first'})
        self.assertEqual(self.row(os.path.join(self.source, 'later'))['status'], 'pending')

    def test_capacity_and_existing_target_takeover(self):
        self.create('file')
        self.scan()
        value = self.controller()
        value.c['mapping']['max_bytes'] = 2
        self.assertFalse(value.new_round())
        os.link(os.path.join(self.source, 'file'), os.path.join(self.target, 'file'))
        self.assertTrue(value.new_round())
        self.assertEqual(set(value.round['members']), {'file'})
        self.assertEqual(value.round['candidates'], [])

    def test_frozen_round_is_persisted_before_linking(self):
        self.create('file')
        self.scan()
        value = self.controller()
        self.assertTrue(value.new_round())
        stored = value.batches.active()
        self.assertEqual(stored['phase'], 'mapping')
        self.assertEqual(stored['candidates'][0]['source_path'], 'file')
        self.assertEqual(os.listdir(self.target), [])

    def test_old_files_table_gets_id_without_losing_records(self):
        self.create('file')
        self.scan()
        before = self.db.list_files()[0]
        self.db.execute("""ALTER TABLE files DROP PRIMARY KEY, DROP INDEX files_source_key,
            DROP COLUMN id, ADD PRIMARY KEY(source_key)""")
        upgraded = Inventory(self.database, **self.settings)
        try:
            after = upgraded.list_files()[0]
            self.assertGreater(after['id'], 0)
            self.assertEqual({k: v for k, v in after.items() if k != 'id'},
                             {k: v for k, v in before.items() if k != 'id'})
            upgraded.scan_source(self.source, self.target, os.walk)
            self.assertEqual(upgraded.list_files()[0]['id'], after['id'])
            self.create('second')
            upgraded.scan_source(self.source, self.target, os.walk)
            rows = upgraded.list_files()
            self.assertEqual(len({row['id'] for row in rows}), 2)
            for table in ('files', 'hash_conflicts'):
                id_column = upgraded.query("SHOW COLUMNS FROM `{}` LIKE 'id'".format(table))[0]
                self.assertIn('auto_increment', id_column['Extra'])
            self.assertEqual(len(upgraded.query('SELECT id FROM pending_files')), 2)
        finally:
            upgraded.close()

    def test_binary_hash_upgrade_preserves_data_and_indexes(self):
        name = next('zero-' + str(n) for n in range(10000)
                    if b'\x00' in bytes.fromhex(path_key('zero-' + str(n))))
        path = self.create(name)
        self.scan()
        self.db.record_results([('linked', '2026-11-08T00:00:00+00:00', None, name)])
        self.db.collision('source', path_key(name), name, 'other', 'test')
        before_file = self.row(path)
        before_conflicts = self.db.query('SELECT * FROM hash_conflicts')
        # Build a populated legacy schema with byte hashes, including a NUL byte.
        for table, keys in (('files', ('source_key', 'root_key', 'target_key')),
                            ('hash_conflicts', ('hash_key',))):
            old = table + '_old_binary'
            self.db.execute('CREATE TABLE `{}` LIKE `{}`'.format(old, table))
            self.db.execute('ALTER TABLE `{}` '.format(old) + ','.join(
                'MODIFY COLUMN `{}` BINARY(32) NOT NULL'.format(key) for key in keys))
            columns = [row['Field'] for row in self.db.query('SHOW COLUMNS FROM `{}`'.format(table))]
            expressions = [('UNHEX(`{}`)'.format(c) if c in keys else '`{}`'.format(c)) for c in columns]
            self.db.execute('INSERT INTO `{}` SELECT {} FROM `{}`'.format(old, ','.join(expressions), table))
            self.db.execute('RENAME TABLE `{0}` TO `{0}_hex_copy`, `{1}` TO `{0}`'.format(table, old))
        # Simulate an interrupted preparation; migration must refill and reuse it.
        self.db.execute('ALTER TABLE files ADD COLUMN root_key_hex CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL')
        upgraded = Inventory(self.database, **self.settings)
        try:
            self.assertEqual(upgraded.list_files()[0], before_file)
            self.assertEqual(upgraded.query('SELECT * FROM hash_conflicts'), before_conflicts)
            for table, keys in (('files', ('source_key', 'root_key', 'target_key')),
                                ('hash_conflicts', ('hash_key',))):
                columns = {r['Field']: r for r in upgraded.query('SHOW FULL COLUMNS FROM `{}`'.format(table))}
                for key in keys:
                    self.assertEqual(columns[key]['Type'], 'char(64)')
                    self.assertEqual(columns[key]['Collation'], 'ascii_bin')
                self.assertFalse(any(c.endswith('_hex') for c in columns))
            indexes = {r['Key_name'] for r in upgraded.query('SHOW INDEX FROM files')}
            self.assertTrue({'PRIMARY', 'files_pending_order', 'files_target'} <= indexes)
            upgraded.migrate_hash_columns()  # Repeated upgrades must be harmless.
            self.assertEqual(upgraded.list_files()[0], before_file)
            upgraded.scan_source(self.source, self.target, os.walk)
        finally:
            upgraded.close()

    def test_unicode_long_paths_and_equal_times(self):
        names = ['照片.jpg', 'A.jpg', 'a.jpg', '😀.jpg',
                 os.path.join(*(['long_' + 'x' * 135] * 6), 'photo.jpg')]
        for name in names:
            self.create(name, stamp=100)
        self.scan()
        paths, after = [], None
        while True:
            rows = self.db.pending(self.source, after, limit=1)
            if not rows:
                break
            paths.append(rows[0]['source_path'])
            after = (rows[0]['mtime_ns'], rows[0]['source_path'])
        self.assertEqual(len(paths), len(names))
        self.assertEqual(len(set(paths)), len(names))
        self.assertEqual(len(self.db.list_files(keyword='照片')), 1)

    def confirmed_fixture(self):
        path = self.create('confirmed.jpg')
        self.scan()
        dest = os.path.join(self.target, 'confirmed.jpg')
        os.link(path, dest)
        self.db.linked('confirmed.jpg')
        s = os.stat(dest)
        evidence = {'state': 'confirmed', 'file': 'confirmed.jpg', 'sha256': 'a'*64,
                    'content_verified': True, 'checked_at': now()}
        return path, dest, evidence, (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)

    def test_manual_cleanup_keeps_source_and_survives_rescan(self):
        path, dest, evidence, fingerprint = self.confirmed_fixture()
        event = self.db.delete_confirmed_target(self.source, self.target, 'confirmed.jpg', evidence, fingerprint)
        self.assertTrue(os.path.isfile(path))
        self.assertFalse(os.path.lexists(dest))
        self.db.scan_target(self.source, self.target, os.walk,
                            deletion_classifier=lambda _: {'modifiedBy': 'remote'})
        self.scan()
        row = self.row(path)
        self.assertEqual((row['status'], row['deletion_origin']), ('deleted', 'manual'))
        self.assertEqual(len(self.db.list_files(status='manual_deleted')), 1)
        self.assertFalse(self.db.pending(self.source))
        self.assertEqual(self.db.query('SELECT state FROM deletion_events WHERE id=%s', (event,))[0]['state'], 'deleted')

    def test_batch_path_owner_survives_content_changes(self):
        path = self.create('same.jpg')
        self.scan()
        with self.db.transaction():
            self.db.claim_batch_paths('a'*32, ['same.jpg'])
            self.db.claim_batch_paths('a'*32, ['same.jpg'])
        with open(path, 'wb') as stream:
            stream.write(b'changed content')
        self.scan()
        self.assertTrue(self.db.pending(self.source))
        self.assertFalse(self.db.pending(self.source, unassigned_only=True))
        self.assertEqual(self.db.batch_owners(), {'same.jpg': 'a'*32})
        with self.assertRaises(ValueError), self.db.transaction():
            self.db.claim_batch_paths('b'*32, ['same.jpg'])
        self.assertEqual(self.db.batch_owners()['same.jpg'], 'a'*32)

    def test_batch_claim_conflict_rolls_back_all_new_paths(self):
        with self.db.transaction():
            self.db.claim_batch_paths('a'*32, ['z.jpg'])
        with self.assertRaises(ValueError), self.db.transaction():
            self.db.claim_batch_paths('b'*32, ['new.jpg', 'z.jpg'])
        self.assertEqual(self.db.batch_owners(), {'z.jpg': 'a'*32})

    def test_different_paths_get_independent_owners(self):
        with self.db.transaction():
            self.db.claim_batch_paths('a'*32, ['A.jpg'])
            self.db.claim_batch_paths('b'*32, ['a.jpg'])
        self.assertEqual(self.db.batch_owners(), {'A.jpg':'a'*32, 'a.jpg':'b'*32})

    def test_photos_retained_requires_successful_run_evidence(self):
        path, dest, evidence, fingerprint = self.confirmed_fixture()
        with self.assertRaises(ValueError):
            self.db.delete_confirmed_target(self.source, self.target, 'confirmed.jpg', evidence,
                                            fingerprint, reason='photos_retained')
        self.assertTrue(os.path.isfile(dest))
        evidence['retention'] = {'state': 'retained_after_release', 'run_id': 'test-run',
            'release_action': 'verification_required', 'file': 'confirmed.jpg',
            'before_fingerprint': list(fingerprint)}
        with self.assertRaises(ValueError):
            self.db.delete_confirmed_target(self.source, self.target, 'confirmed.jpg', evidence,
                                            fingerprint, reason='photos_retained')
        evidence['retention']['release_action'] = 'nothing_to_free'
        self.db.delete_confirmed_target(self.source, self.target, 'confirmed.jpg', evidence,
                                        fingerprint, reason='photos_retained')
        self.assertFalse(os.path.lexists(dest))
        self.assertTrue(os.path.isfile(path))
        row = self.row(path)
        self.assertEqual(row['deletion_reason'], 'photos_retained')
        self.assertEqual(json.loads(row['backup_evidence'])['retention']['run_id'], 'test-run')

    def test_relaxed_batch_keeps_sources_and_labels_aggregate_trust(self):
        path, dest, _, fingerprint = self.confirmed_fixture()
        proof = {'state': 'aggregate_confirmed', 'run_id': uuid.uuid4().hex,
            'backup_complete': True, 'nothing_to_free': True, 'sync_ready': True,
            'sync_token_before': [1], 'sync_token_after': [1], 'checked_at': now()}
        snapshots = {'confirmed.jpg': list(fingerprint)}
        with self.assertRaises(ValueError):
            self.db.delete_relaxed_targets(self.source, self.target, snapshots, dict(proof, nothing_to_free=False))
        self.assertTrue(os.path.isfile(dest))
        result = self.db.delete_relaxed_targets(self.source, self.target, snapshots, proof)
        self.assertEqual(result[0]['action'], 'deleted')
        self.assertFalse(os.path.exists(dest))
        self.assertTrue(os.path.isfile(path))
        row = self.row(path)
        self.assertEqual((row['deletion_origin'], row['backup_state'], row['deletion_reason']),
                         ('manual', 'aggregate_confirmed', 'relaxed_no_free_space'))
        self.db.scan_target(self.source, self.target, os.walk)
        self.assertEqual(self.row(path)['deletion_origin'], 'manual')

    def test_relaxed_untracked_files_are_audited_and_replacements_kept(self):
        path, dest, _, fingerprint = self.confirmed_fixture()
        proof = {'state': 'aggregate_confirmed', 'run_id': uuid.uuid4().hex,
            'backup_complete': True, 'nothing_to_free': True, 'sync_ready': True,
            'sync_token_before': [1], 'sync_token_after': [1], 'checked_at': now()}
        with open(dest, 'ab') as stream:
            stream.write(b'changed')
        result = self.db.delete_relaxed_targets(self.source, self.target, {'confirmed.jpg': fingerprint}, proof)
        self.assertEqual(result[0]['action'], 'changed_kept')
        extra = os.path.join(self.target, 'untracked.jpg')
        with open(extra, 'wb') as stream:
            stream.write(b'other')
        s = os.stat(extra)
        result = self.db.delete_relaxed_targets(self.source, self.target,
            {'untracked.jpg': (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns)}, proof)
        self.assertEqual(result[0]['action'], 'deleted')
        self.assertTrue(os.path.isfile(dest))
        self.assertEqual(self.db.query('SELECT source_path,state FROM deletion_events')[0],
                         {'source_path': 'untracked.jpg', 'state': 'deleted'})
        record = self.db.query('SELECT status,deletion_origin,source_present FROM files WHERE source_path=%s',
                               ('untracked.jpg',))[0]
        self.assertEqual(record, {'status': 'deleted', 'deletion_origin': 'manual', 'source_present': 0})

    def test_relaxed_failed_unlink_restores_inventory_and_keeps_error_audit(self):
        path, dest, _, fingerprint = self.confirmed_fixture()
        extra = os.path.join(self.target, 'extra.jpg')
        with open(extra, 'wb') as stream:
            stream.write(b'extra')
        s = os.stat(extra)
        proof = {'state': 'aggregate_confirmed', 'run_id': uuid.uuid4().hex,
            'backup_complete': True, 'nothing_to_free': True, 'sync_ready': True,
            'sync_token_before': [1], 'sync_token_after': [1], 'checked_at': now()}
        with patch('storage.os.unlink', side_effect=PermissionError('denied')):
            result = self.db.delete_relaxed_targets(self.source, self.target,
                {'confirmed.jpg': fingerprint, 'extra.jpg': (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns)}, proof)
        self.assertTrue(all(row['action'] == 'error' for row in result))
        self.assertEqual(self.row(path)['status'], 'linked')
        self.assertEqual(self.row(path)['deletion_origin'], 'unknown')
        self.assertEqual(len(self.db.list_files()), 1)
        self.assertTrue(os.path.exists(dest))
        self.assertTrue(os.path.exists(extra))
        self.assertTrue(all(row['state'] == 'error' for row in self.db.query('SELECT state FROM deletion_events')))

    def test_cleanup_rejects_unconfirmed_and_changed_files(self):
        path, dest, evidence, fingerprint = self.confirmed_fixture()
        evidence['state'] = 'not_confirmed'
        with self.assertRaises(ValueError):
            self.db.delete_confirmed_target(self.source, self.target, 'confirmed.jpg', evidence, fingerprint)
        evidence['state'] = 'confirmed'
        with open(dest, 'ab') as stream:
            stream.write(b'changed')
        with self.assertRaises(ValueError):
            self.db.delete_confirmed_target(self.source, self.target, 'confirmed.jpg', evidence, fingerprint)
        self.assertTrue(os.path.exists(dest))
        self.assertEqual(self.row(path)['status'], 'linked')
        self.assertFalse(self.db.query('SELECT * FROM deletion_events'))

    def test_missing_target_is_unknown_without_remote_proof(self):
        path, dest, _, _ = self.confirmed_fixture()
        os.unlink(dest)
        self.db.scan_target(self.source, self.target, os.walk,
                            deletion_classifier=lambda _: None)
        self.assertEqual(self.row(path)['deletion_origin'], 'unknown')
        self.assertEqual(len(self.db.list_files(status='deleted_unknown')), 1)
        self.db.classify_deletions(self.target, lambda name: {'file': name, 'modifiedBy': 'PIXEL'})
        self.assertEqual(self.row(path)['deletion_origin'], 'syncthing')
        self.assertEqual(len(self.db.list_files(status='synced_deleted')), 1)

    def test_failed_unlink_is_journaled_without_guessing_remote_origin(self):
        path, dest, evidence, fingerprint = self.confirmed_fixture()
        with patch('storage.os.unlink', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                self.db.delete_confirmed_target(self.source, self.target, 'confirmed.jpg', evidence, fingerprint)
        self.assertTrue(os.path.exists(dest))
        self.assertEqual(self.db.query('SELECT state FROM deletion_events')[0]['state'], 'error')
        self.assertEqual(self.row(path)['deletion_origin'], 'unknown')
        self.assertEqual(self.row(path)['status'], 'linked')


if __name__ == '__main__':
    unittest.main()
