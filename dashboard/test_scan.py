import unittest
from unittest.mock import patch
import app

class ScanApiTests(unittest.TestCase):
    def test_scan_post_checks_origin_and_forwards(self):
        client = app.app.test_client()
        with patch.object(app, 'runtime', return_value={'available': True, 'controller_stale': False}), patch.object(app.config_service, 'call', return_value=({'accepted': True}, 202)) as call:
            self.assertEqual(client.post('/api/scan').status_code, 403)
            self.assertEqual(client.post('/api/scan', headers={'Origin': 'https://evil.example', 'X-PicSync-Settings': '1'}).status_code, 403)
            call.assert_not_called()
            self.assertEqual(client.post('/api/scan', headers={'X-PicSync-Settings': '1'}).status_code, 202)
            call.assert_called_once_with('POST', path='/scan')

    def test_offline_controller_rejected(self):
        with patch.object(app, 'runtime', return_value={'available': True, 'controller_stale': True}):
            self.assertEqual(app.app.test_client().post('/api/scan', headers={'X-PicSync-Settings': '1'}).status_code, 503)
