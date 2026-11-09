import os
import unittest
from unittest.mock import patch
import app as web


class ShortcutConfigurationTests(unittest.TestCase):
    def test_unconfigured_shortcuts_are_hidden(self):
        with patch.dict(os.environ, {'PICSYNC_PIXEL_URL': '', 'PICSYNC_SYNCTHING_URL': ''}):
            html = web.app.test_client().get('/').get_data(as_text=True)
        self.assertEqual(html.count('hidden href="#"'), 2)
        self.assertNotIn('__PIXEL_URL__', html)

    def test_url_is_configurable_and_html_escaped(self):
        with patch.dict(os.environ, {'PICSYNC_PIXEL_URL': 'https://phone.example.test/?a=1&b=2'}):
            html = web.app.test_client().get('/').get_data(as_text=True)
        self.assertIn('href="https://phone.example.test/?a=1&amp;b=2"', html)
        self.assertIn('rel="noopener noreferrer"', html)

    def test_script_url_is_rejected(self):
        with patch.dict(os.environ, {'PICSYNC_PIXEL_URL': 'javascript:alert(1)'}):
            with web.app.test_request_context('/'), self.assertRaises(ValueError):
                web.index()
