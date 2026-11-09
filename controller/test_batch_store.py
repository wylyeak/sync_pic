"""Migration and persistence regression tests on isolated MySQL databases."""
import json
import os
import unittest
from unittest.mock import patch
from batch_store import BatchStore, split_files, dumps
import test_storage as storage_tests


class SplitTests(unittest.TestCase):
    def test_file_details_are_kept_out_of_batch_summary(self):
        batch = {'members':{'a':[1,2,3,4]},'candidates':[{'source_path':'a','size_bytes':3}],
                 'backup_confirmed':{'a':[1,2,3,4]},'deferred':['a'],
                 'cleanup_results':[{'file':'a','action':'changed_kept'}]}
        data = split_files(batch)
        self.assertEqual(set(data), {'a'})
        self.assertEqual(data['a']['member'], [1,2,3,4])
        self.assertTrue(data['a']['deferred'])


@unittest.skipUnless(os.environ.get('FILE_SYNC_TEST_MYSQL') == '1', 'MySQL integration tests are opt-in')
class BatchStoreTest(unittest.TestCase):
    setUp = storage_tests.InventoryTest.setUp
    cleanup_database = storage_tests.InventoryTest.cleanup_database

    def batch(self):
        return dict(id='a'*32,phase='observe',started_at=1700000000.123456,
            phase_started_at=1700000001.25,next_check=1700000300.5,observations=2,
            members={'a.jpg':[1,2,3,4],'b.jpg':[5,6,7,8]},
            candidates=[dict(source_path='b.jpg',source_dev=5,source_ino=6,size_bytes=7,mtime_ns=8)],
            backup_confirmed={'a.jpg':[1,2,3,4]},deferred=['b.jpg'],
            mapping_skipped={'b.jpg':'changed'},phase_seconds={'sync':21.5,'backup':6},
            release={'action':'nothing_to_free'},removed_count=0,
            cleanup_results=[{'file':'b.jpg','action':'changed_kept'},{'file':'a.jpg','action':'already_missing'}])

    def legacy(self, batch):
        self.db.execute('''CREATE TABLE IF NOT EXISTS controller_rounds (
            id VARCHAR(32) PRIMARY KEY, phase VARCHAR(32), data LONGTEXT, updated_at VARCHAR(40))''')
        self.db.execute('INSERT INTO controller_rounds VALUES (%s,%s,%s,%s)',
                        (batch['id'],batch['phase'],dumps(batch),'2026-11-08T00:00:00+00:00'))

    def test_migration_is_lossless_idempotent_and_preserves_legacy(self):
        batch = self.batch()
        self.legacy(batch)
        store = BatchStore(self.db)
        store.migrate()
        self.assertEqual(store.load(batch['id']), batch)
        store.migrate()
        self.assertEqual(BatchStore(self.db).active(), batch)
        self.assertEqual(json.loads(self.db.query('SELECT data FROM controller_rounds')[0]['data']), batch)
        row = self.db.query('SELECT * FROM backup_batches')[0]
        self.assertNotIn('members', json.loads(row['extra']))
        self.assertNotIn('candidates', json.loads(row['extra']))
        self.assertLess(len(row['extra']), 500)

    def test_unchanged_files_are_not_rewritten_and_mutations_survive_restart(self):
        store=BatchStore(self.db);store.migrate();batch=self.batch();store.save(batch)
        with patch.object(self.db,'many',wraps=self.db.many) as many:
            batch['observations']=3
            store.save(batch)
        writes=[c.args[1] for c in many.call_args_list if '(source_key,source_path,batch_id,state,' in c.args[0]]
        self.assertEqual(writes,[[]])
        batch['members']['a.jpg'][2]=99
        store.save(batch)
        self.assertEqual(BatchStore(self.db).load(batch['id']),batch)

    def test_failed_member_write_rolls_back_batch_and_ownership(self):
        store=BatchStore(self.db);store.migrate();batch=self.batch()
        with patch.object(self.db,'many',side_effect=RuntimeError('failed write')):
            with self.assertRaises(RuntimeError):store.save(batch)
        self.assertIsNone(store.load(batch['id']))
        self.assertEqual(self.db.batch_owners(),{})
        store.save(batch)
        self.assertEqual(store.load(batch['id']),batch)

    def test_legacy_duplicate_assignment_aborts_entire_migration(self):
        a=self.batch();b=dict(a,id='b'*32)
        self.legacy(a);self.legacy(b)
        store=BatchStore(self.db)
        with self.assertRaises(ValueError):store.migrate()
        self.assertEqual(self.db.query('SELECT * FROM backup_batches'),())
        self.assertEqual(self.db.batch_owners(),{})
        self.assertEqual(self.db.query('SELECT * FROM schema_migrations'),())

    def test_unknown_backup_and_empty_optional_fields_are_not_invented(self):
        store=BatchStore(self.db);store.migrate()
        batch={'id':'a'*32,'phase':'sync','members':{'a':[1,2,3,4]}}
        store.save(batch)
        self.assertEqual(store.load(batch['id']),batch)
        self.assertIsNone(self.db.query('SELECT backed_up FROM backup_batches')[0]['backed_up'])

    def test_retired_tables_and_field_stay_absent_after_restart(self):
        store=BatchStore(self.db);store.migrate();batch=self.batch();store.save(batch)
        self.legacy(batch)
        self.db.execute('CREATE TABLE sync_batches (id INT PRIMARY KEY)')
        self.db.execute('ALTER TABLE files ADD COLUMN link_batch_id BIGINT NULL, ADD INDEX files_link_batch(link_batch_id)')
        self.db.execute('DROP TABLE controller_rounds,sync_batches')
        self.db.execute('ALTER TABLE files DROP INDEX files_link_batch,DROP COLUMN link_batch_id')
        from storage import Inventory
        restored=Inventory(self.database,**self.settings)
        try:
            reopened=BatchStore(restored);reopened.migrate()
            self.assertEqual(reopened.load(batch['id']),batch)
            self.assertFalse(restored.query("SHOW TABLES LIKE 'controller_rounds'"))
            self.assertFalse(restored.query("SHOW TABLES LIKE 'sync_batches'"))
            self.assertFalse(restored.query("SHOW COLUMNS FROM files LIKE 'link_batch_id'"))
            path=os.path.join(self.source,'test.jpg')
            with open(path,'wb') as f:f.write(b'test')
            restored.scan_source(self.source,self.target,os.walk)
            restored.record_results([('linked','2026-11-08T00:00:00+00:00',None,'test.jpg')])
            self.assertEqual(restored.list_files()[0]['status'],'linked')
        finally:restored.close()
