import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from configuration import DEFAULT_VALUES
import live_config
import app as web

class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, PICSYNC_LIVE_CONFIG=str(Path(self.tmp.name, 'controller.toml')))
        self.env.start()
        self.addCleanup(self.env.stop)
        self.runtime = {'controller_stale': False, 'settings': {'values': live_config.public(copy.deepcopy(DEFAULT_VALUES)), 'revision': live_config.read()[1]}}
        self.mock = patch('app.runtime', return_value=self.runtime)
        self.mock.start()
        self.addCleanup(self.mock.stop)
        self.client = web.app.test_client()

    def put(self, values, **kwargs):
        return self.client.put('/api/settings', json={'values': values, 'revision': live_config.read()[1]}, **kwargs)

    def test_save_pending_then_applied_and_secret_free(self):
        first = self.client.get('/api/settings').get_json()
        self.assertTrue(first['applied'])
        response = self.put({'scan': {'interval': 22}}, headers={'X-PicSync-Settings': '1'})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json['applied'])
        self.assertEqual(live_config.read()[0]['scan']['interval'], 22)
        self.runtime['settings']['revision'] = response.json['revision']
        self.assertTrue(self.client.get('/api/settings').json['applied'])
        self.assertNotIn('mysql', response.get_data(as_text=True))
        self.assertNotIn('key"', response.get_data(as_text=True))

    def test_cross_origin_or_missing_custom_header_cannot_write(self):
        for headers in ({}, {'Origin': 'https://other.example', 'X-PicSync-Settings': '1'}):
            self.assertEqual(self.put({}, headers=headers).status_code, 403)
        self.assertFalse(live_config.path().exists())

    def test_https_reverse_proxy_origin_is_accepted(self):
        self.assertEqual(self.put({}, headers={'Origin': 'https://localhost', 'X-PicSync-Settings': '1'}).status_code, 200)

    def test_stale_controller_and_invalid_protection_values(self):
        response = self.put({'directories': {'source': '/tmp'}}, headers={'X-PicSync-Settings': '1'})
        self.assertEqual(response.status_code, 400)
        self.runtime['controller_stale'] = True
        self.assertEqual(self.put({}, headers={'X-PicSync-Settings': '1'}).status_code, 503)
        self.assertFalse(live_config.path().exists())

    def test_conflict_reports_409(self):
        revision = live_config.read()[1]
        self.put({'scan': {'interval': 25}}, headers={'X-PicSync-Settings': '1'})
        response = self.client.put('/api/settings', json={'values': {}, 'revision': revision}, headers={'X-PicSync-Settings': '1'})
        self.assertEqual(response.status_code, 409)
