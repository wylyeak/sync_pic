import json
from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import app as web

class DashboardTests(unittest.TestCase):
    def setUp(self):
        web.cache.clear()
        self.client = web.app.test_client()
    def test_ui_and_mutation_routes(self):
        with self.client.get('/') as response:
            self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get('/static/../config.toml').status_code, 404)
        self.assertEqual(self.client.post('/api/files').status_code, 405)
        self.assertEqual(self.client.post('/api/cleanup').status_code, 404)
    def test_no_secrets_in_database_error(self):
        with patch('app.db', side_effect=web.pymysql.OperationalError('private-password')):
            response = self.client.get('/api/overview')
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('private-password', response.get_data(as_text=True))

    def test_release_identity_visible_without_database_and_not_cached(self):
        info = dict(version='20261110171230', revision='a'*40, timezone='Asia/Shanghai')
        with patch('app.build_info', return_value=info), patch('app.db', side_effect=AssertionError('must not query database')):
            response = self.client.get('/')
            html = response.get_data(as_text=True)
            self.assertIn('版本 <span>20261110171230</span>', html)
            self.assertIn(' · aaaaaaa</span>', html)
            self.assertNotIn('v1.0', html)
            self.assertNotIn('__BUILD_', html)
            self.assertEqual(response.headers['Cache-Control'], 'no-cache')
            for _ in range(2):
                response = self.client.get('/api/version')
                self.assertEqual(response.get_json(), info)
                self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_build_info_uses_baked_file_or_explicit_development(self):
        with tempfile.TemporaryDirectory() as folder, patch('app.ROOT', Path(folder)):
            web.build_info.cache_clear()
            self.addCleanup(web.build_info.cache_clear)
            self.assertEqual(web.build_info()['version'], 'development')
            info = dict(version='20261110171230', revision='b'*40, timezone='Asia/Shanghai')
            Path(folder, 'build-info.json').write_text(json.dumps(info))
            web.build_info.cache_clear()
            self.assertEqual(web.build_info(), info)
    def test_runtime_marks_old_controller_heartbeat_stale(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, 'runtime.json')
            path.write_text(json.dumps({'controller': {'checked_at': 1}}))
            with patch('app.settings', return_value={'dashboard': {'telemetry': str(path)}}):
                self.assertTrue(web.runtime()['controller_stale'])
    def test_file_filter_is_parameterized_and_pagination_bounded(self):
        cursor = Mock()
        cursor.fetchone.return_value = {'total': 0}
        cursor.fetchall.return_value = []
        @contextmanager
        def database():
            yield cursor
        term="' OR 1=1 --"
        with patch('app.db', database):
            response=self.client.get('/api/files', query_string={'q': term, 'page': 100000, 'size': 999})
        self.assertEqual(response.status_code, 200)
        query, params=cursor.execute.call_args.args
        self.assertNotIn(term, query)
        self.assertIn(term, params)
        self.assertEqual(params[-2:], [100, 999900])
    def test_invalid_filters_and_ids(self):
        for url in ('/api/files?status=drop', '/api/files?batch=../../config.toml', '/api/batches/not-valid', '/api/audits?origin=root'):
            self.assertEqual(self.client.get(url).status_code, 400)

    def test_deleted_breakdown_counts_and_bytes_reconcile_with_total(self):
        rows=[dict(status='deleted',deletion_origin='manual',files=2,bytes=100),
              dict(status='deleted',deletion_origin='syncthing',files=3,bytes=200),
              dict(status='deleted',deletion_origin='unknown',files=4,bytes=300),
              dict(status='deleted',deletion_origin=None,files=1,bytes=50)]
        for inventory_rows in (rows, []):
            web.cache.clear()
            cursor=Mock()
            cursor.fetchall.side_effect=[inventory_rows, []]
            cursor.fetchone.side_effect=[None, dict(total=0,completed=0)]
            @contextmanager
            def database():
                yield cursor
            with patch('app.db',database),patch('app.runtime',return_value={}),patch('app.settings',return_value={'dashboard':{}}):
                response=self.client.get('/api/overview')
            self.assertEqual(response.status_code,200)
            data=response.get_json();groups=data['deletion_breakdown']
            self.assertEqual(set(groups),{'manual','syncthing','unknown'})
            self.assertEqual(sum(g['files'] for g in groups.values()),data['inventory']['deleted']['files'])
            self.assertEqual(sum(g['bytes'] for g in groups.values()),data['inventory']['deleted']['bytes'])
            if inventory_rows:
                self.assertEqual(groups['unknown'],dict(files=5,bytes=350))

    def test_batch_files_use_unique_path_ownership(self):
        cursor = Mock()
        cursor.fetchone.return_value = {'total': 0}
        cursor.fetchall.return_value = []
        @contextmanager
        def database():
            yield cursor
        batch_id = 'a'*32
        with patch('app.db', database), patch('app.cached_batch', return_value={'id':batch_id}):
            response = self.client.get('/api/files?batch='+batch_id)
        self.assertEqual(response.status_code, 200)
        sql, params = cursor.execute.call_args.args
        self.assertIn('controller_file_batches WHERE batch_id=%s', sql)
        self.assertIn(batch_id, params)

    def test_file_detail_exposes_one_owning_batch(self):
        cursor = Mock()
        cursor.fetchone.side_effect = [dict(id=1, source_key='b'*64, source_path='photo.jpg'),
                                       dict(batch_id='a'*32)]
        cursor.fetchall.return_value = []
        @contextmanager
        def database():
            yield cursor
        with patch('app.db', database):
            response = self.client.get('/api/files/1')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['batch_id'], 'a'*32)
    def test_batch_summary_provides_confirmed_counts(self):
        row={'phase':'complete','phase_seconds':'{"observe":600}', 'summary':'{"backed_up":9}', 'id':'a'*32}
        result=web.round_row(row)
        self.assertEqual(result['backed_up'], 9)
        self.assertEqual(result['phase_seconds']['observe'],600)
    def test_active_backup_count_excludes_deferred_members(self):
        result=web.round_row({'phase':'observe','id':'a'*32,'summary':None,
            'confirmed_names':'["a","b"]','deferred_names':'["b"]','backed_up':2})
        self.assertEqual(result['backed_up'],1)
        self.assertNotIn('confirmed_names',result)
        self.assertNotIn('deferred_names',result)
    def test_raw_batch_json_is_decoded_once_and_not_returned(self):
        batch={'members': {'private/photo.jpg': [1]}, 'backup_confirmed': {'private/photo.jpg': [1]},
               'started_at':100,'phase_seconds':{'sync':10},'summary':None}
        result=web.round_row({'id':'a'*32,'phase':'backup','data':json.dumps(batch)})
        self.assertEqual(result['file_count'],1)
        self.assertEqual(result['backed_up'],1)
        self.assertNotIn('data',result)
        self.assertNotIn('private/photo.jpg',str(result))
    def test_headers_block_external_scripts(self):
        response=self.client.get('/health')
        self.assertIn("script-src 'self'", response.headers['Content-Security-Policy'])
        self.assertEqual(response.headers['X-Content-Type-Options'],'nosniff')

if __name__=='__main__':
    unittest.main()
