import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import config_service
import live_config

class ConfigServiceTests(unittest.TestCase):
    def test_scan_request_uses_existing_socket(self):
        from unittest.mock import Mock
        controller = SimpleNamespace(request_scan=Mock(return_value=({'accepted': True}, 202)))
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, PICSYNC_CONFIG_SOCKET=str(Path(root, 'config.sock'))):
            with config_service.serve(controller):
                data, status = config_service.call('POST', path='/scan')
                self.assertEqual(status, 202)
                self.assertTrue(data['accepted'])
                controller.request_scan.assert_called_once_with()

    def test_release_request_is_bound_to_batch_and_validated(self):
        from unittest.mock import Mock
        controller = SimpleNamespace(request_release=Mock(return_value=({'accepted': True}, 202)))
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, PICSYNC_CONFIG_SOCKET=str(Path(root, 'config.sock'))):
            with config_service.serve(controller):
                self.assertEqual(config_service.call('POST', {'batch_id': 'test'}, path='/release')[1], 202)
                controller.request_release.assert_called_once_with('test')
                self.assertEqual(config_service.call('POST', {'batch_id': []}, path='/release')[1], 400)

    def test_selected_delete_socket_binds_revision_and_paths(self):
        from unittest.mock import Mock
        controller = SimpleNamespace(request_selected_delete=Mock(return_value=({'accepted': True},202)))
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ,PICSYNC_CONFIG_SOCKET=str(Path(root,'config.sock'))):
            with config_service.serve(controller):
                payload={'batch_id':'a'*32,'revision':'review','files':['large.mp4']}
                self.assertEqual(config_service.call('POST',payload,path='/delete-selected')[1],202)
                controller.request_selected_delete.assert_called_once_with('a'*32,'review',['large.mp4'])
                self.assertEqual(config_service.call('POST',dict(payload,revision=[]),path='/delete-selected')[1],400)

    def test_socket_api_edits_original_and_never_returns_private_content(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, 'config.toml')
            path.write_text('[mysql]\npassword="private-secret"\n[scan]\ninterval=40 # keep me\n')
            controller = SimpleNamespace(config_path=path, config_revision=live_config.read(path)[1])
            with patch.dict(os.environ, PICSYNC_CONFIG_SOCKET=str(Path(root, 'config.sock'))):
                with config_service.serve(controller):
                    first, status = config_service.call('GET')
                    self.assertEqual(status, 200)
                    self.assertTrue(first['applied'])
                    self.assertNotIn('private-secret', str(first))
                    self.assertEqual(config_service.socket_path().stat().st_mode & 0o777, 0o600)
                    saved, status = config_service.call('PUT', {'values': {'scan': {'interval': 80}}, 'revision': first['revision']})
                    self.assertEqual(status, 200)
                    self.assertFalse(saved['applied'])
                    self.assertIn('interval=80 # keep me', path.read_text())
                    self.assertIn('private-secret', path.read_text())
                    controller.config_revision = saved['revision']
                    self.assertTrue(config_service.call('GET')[0]['applied'])
                    self.assertEqual(config_service.call('PUT', {'values': {}, 'revision': first['revision']})[1], 409)
                    self.assertEqual(config_service.call('PUT', {'values': {'mysql': {'password': 'change'}}, 'revision': saved['revision']})[1], 400)
                    self.assertEqual(config_service.call('PUT', {'values': {'scan': {'interval': True}}, 'revision': saved['revision']})[1], 400)
                self.assertFalse(config_service.socket_path().exists())

    def test_preview_socket_streams_original_and_supports_etag(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, 'photo.jpg')
            Image.new('RGB', (12, 8)).save(path)
            controller = SimpleNamespace(source=root)
            with patch.dict(os.environ, PICSYNC_CONFIG_SOCKET=str(Path(root, 'config.sock'))):
                with config_service.serve(controller):
                    client, response = config_service.preview('photo.jpg')
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), path.read_bytes())
                    etag = response.getheader('ETag')
                    client.close()
                    client, response = config_service.preview('photo.jpg', etag)
                    self.assertEqual(response.status, 304)
                    self.assertEqual(response.read(), b'')
                    client.close()
                    client, response = config_service.preview('../photo.jpg')
                    self.assertEqual(response.status, 400)
                    client.close()
