from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import gzip
import json
import threading
import time
import unittest
from unittest.mock import Mock, patch
from performance import TTLCache, ReadPool
import app as web

class CacheTests(unittest.TestCase):
    def test_expiration_and_bounded_storage(self):
        now=[0]
        cache=TTLCache(limit=2,clock=lambda:now[0])
        fetch=Mock(side_effect=[1,2,3,4])
        self.assertEqual(cache.get('a',10,fetch).value,1)
        self.assertEqual(cache.get('a',10,fetch).state,'hit')
        now[0]=11
        self.assertEqual(cache.get('a',10,fetch).value,2)
        cache.get('b',10,fetch);cache.get('c',10,fetch)
        self.assertEqual(len(cache.values),2)
        self.assertNotIn('a',cache.values)
    def test_concurrent_refresh_runs_one_query(self):
        cache=TTLCache();started=threading.Event();release=threading.Event()
        def fetch(): started.set();release.wait(2);return {'files':30}
        loader=Mock(side_effect=fetch)
        with ThreadPoolExecutor(max_workers=6) as executor:
            owner=executor.submit(cache.get,'inventory',15,loader)
            self.assertTrue(started.wait(1))
            others=[executor.submit(cache.get,'inventory',15,loader) for _ in range(5)]
            release.set()
            values=[owner.result()]+[future.result() for future in others]
        self.assertTrue(all(result.value=={'files':30} for result in values))
        loader.assert_called_once()
    def test_failed_queries_are_not_cached(self):
        cache=TTLCache();loader=Mock(side_effect=[RuntimeError('offline'),42])
        with self.assertRaises(RuntimeError): cache.get('query',10,loader)
        self.assertEqual(cache.get('query',10,loader).value,42)

class BackgroundCacheTests(unittest.TestCase):
    def test_expired_statistics_return_promptly_while_refreshing(self):
        now=[0];cache=TTLCache(clock=lambda:now[0])
        cache.get('stats',10,lambda:1)
        now[0]=11;started=threading.Event();release=threading.Event()
        def refresh():started.set();release.wait(2);return 2
        result=cache.get('stats',10,refresh,stale_for=20)
        self.assertEqual(result.value,1)
        self.assertEqual(result.state,'refreshing')
        self.assertTrue(started.wait(1))
        pending=cache.pending['stats'];release.set();pending.result(timeout=2)
        self.assertEqual(cache.get('stats',10,lambda:3).value,2)
    def test_very_old_statistics_are_never_silently_served(self):
        now=[0];cache=TTLCache(clock=lambda:now[0])
        cache.get('stats',10,lambda:1);now[0]=31
        with self.assertRaises(RuntimeError):
            cache.get('stats',10,lambda:(_ for _ in ()).throw(RuntimeError('offline')),stale_for=20)

class PoolTests(unittest.TestCase):
    def test_connection_is_reused_but_never_shared_concurrently(self):
        factory=Mock(side_effect=lambda:Mock())
        pool=ReadPool(factory,size=2);lock=threading.Lock();active=set()
        def work():
            with pool.connection() as conn:
                with lock:
                    self.assertNotIn(id(conn),active);active.add(id(conn))
                time.sleep(.01)
                with lock: active.remove(id(conn))
        with ThreadPoolExecutor(max_workers=6) as executor:
            for result in [executor.submit(work) for _ in range(12)]:result.result()
        self.assertLessEqual(factory.call_count,2)
    def test_failed_connections_are_discarded(self):
        first,second=Mock(),Mock();factory=Mock(side_effect=[first,second]);pool=ReadPool(factory)
        with self.assertRaises(RuntimeError):
            with pool.connection():raise RuntimeError('query failed')
        first.close.assert_called_once()
        with pool.connection() as conn:self.assertIs(conn,second)
    def test_factory_failure_does_not_leak_pool_capacity(self):
        conn=Mock();factory=Mock(side_effect=[RuntimeError('connect'),conn]);pool=ReadPool(factory,size=1)
        with self.assertRaises(RuntimeError):
            with pool.connection():pass
        with pool.connection() as actual:self.assertIs(actual,conn)

class EndpointPerformanceTests(unittest.TestCase):
    def setUp(self):
        web.cache.clear();self.client=web.app.test_client()
    def test_status_refresh_never_waits_for_database(self):
        with patch('app.db',side_effect=AssertionError('DB should not be used')),patch('app.runtime',return_value={'available':True}):
            for _ in range(2):
                response=self.client.get('/api/runtime')
                self.assertTrue(response.get_json()['runtime']['available'])
                self.assertIn('Server-Timing',response.headers)
    def test_repeated_pages_use_one_database_borrow(self):
        cursor=Mock();cursor.fetchone.return_value={'total':0};cursor.fetchall.return_value=[]
        calls=[]
        @contextmanager
        def database():calls.append(1);yield cursor
        with patch('app.db',database):
            first=self.client.get('/api/files')
            second=self.client.get('/api/files')
        self.assertEqual(first.get_json(),second.get_json())
        self.assertEqual(second.headers['X-Cache'],'hit')
        self.assertEqual(len(calls),1)
    def test_api_gzip_is_decodable_and_not_publicly_cached(self):
        data=[{'source_path':'a'*100,'id':i} for i in range(10)]
        cursor=Mock();cursor.fetchone.return_value={'total':10};cursor.fetchall.return_value=data
        @contextmanager
        def database():yield cursor
        with patch('app.db',database):
            response=self.client.get('/api/files',headers={'Accept-Encoding':'gzip'})
        self.assertEqual(response.headers['Content-Encoding'],'gzip')
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        self.assertEqual(json.loads(gzip.decompress(response.data))['items'],data)
    def test_static_assets_have_content_fingerprints(self):
        html=self.client.get('/').get_data(as_text=True)
        self.assertIn('/static/app.js?v=',html)
        with self.client.get('/static/app.js?v='+web.static_version('app.js')) as response:
            self.assertIn('immutable',response.headers['Cache-Control'])
    def test_connection_factory_sets_readonly_before_pooling(self):
        conn=Mock();cursor=Mock();conn.cursor.return_value.__enter__=Mock(return_value=cursor)
        conn.cursor.return_value.__exit__=Mock(return_value=False)
        with patch('app.pymysql.connect',return_value=conn):
            self.assertIs(web.connect_readonly(),conn)
        self.assertEqual(cursor.execute.call_args_list[0].args, ("SET time_zone='+00:00'",))
        self.assertEqual(cursor.execute.call_args_list[1].args, ('SET SESSION TRANSACTION READ ONLY',))

if __name__=='__main__':unittest.main()
