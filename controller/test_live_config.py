import copy
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from configuration import DEFAULT_VALUES
import live_config as live
from file_sync import Controller, Observation

class LiveConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name, 'controller.toml')
        self.env = patch.dict(os.environ, PICSYNC_CONFIG=str(self.path))
        self.env.start()
        self.addCleanup(self.env.stop)
        self.base = copy.deepcopy(DEFAULT_VALUES)
        self.path.write_text('')

    def test_atomic_persistence_restart_and_no_credentials(self):
        self.path.write_text('# original comment\n[mysql]\npassword = "private-password" # keep password\n[bark]\nkey = "private-key"\n[scan]\ninterval = 3600 # preserve comment\n')
        values, revision = live.save({'mapping': {'max_bytes': 12345}}, live.read()[1], self.base)
        self.assertEqual(live.read(), (values, revision))
        self.assertEqual(live.merge(self.base, live.read()[0])['mapping']['max_bytes'], 12345)
        text = self.path.read_text()
        self.assertIn('password = "private-password" # keep password', text)
        self.assertIn('key = "private-key"', text)
        self.assertIn('# original comment', text)
        self.assertIn('interval = 3600 # preserve comment', text)
        self.assertNotIn('private', str(values))
        self.assertNotIn('directories', text)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_invalid_changes_leave_file_unchanged(self):
        live.save({}, live.read()[1], self.base)
        before = self.path.read_bytes()
        for values in ({'directories': {'source': '/tmp'}}, {'mysql': {'password': 'x'}},
                       {'mapping': {'min_battery_percent': 30}}, {'scan': {'interval': True}},
                       {'observation': {'confirmations': 2}}, {'observation': {'interval': 299}},
                       {'bark': {'enabled': 1}}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                live.save(values, live.read()[1], self.base)
            self.assertEqual(self.path.read_bytes(), before)

    def test_conflict_does_not_overwrite_newer_values(self):
        version = live.read()[1]
        live.save({'scan': {'interval': 50}}, version, self.base)
        with self.assertRaises(live.Conflict):
            live.save({'scan': {'interval': 100}}, version, self.base)
        self.assertEqual(live.read()[0]['scan']['interval'], 50)

    def test_failed_replace_preserves_previous_file(self):
        live.save({'scan': {'interval': 25}}, live.read()[1], self.base)
        before = self.path.read_bytes()
        with patch('live_config.os.replace', side_effect=OSError('disk')), self.assertRaises(OSError):
            live.save({'scan': {'interval': 123}}, live.read()[1], self.base)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(list(self.path.parent.glob('.config-*')))

    def controller(self):
        c = Controller.__new__(Controller)
        c.config_path = self.path
        c.base_config = copy.deepcopy(self.base)
        c.c = copy.deepcopy(self.base)
        c.config_revision = live.read()[1]
        c.observation = Observation()
        c.observation.count = 2
        c.round = {'observations': 2}
        c.save = Mock()
        c.publish = Mock()
        c.stop = threading.Event()
        return c

    def test_reload_wakes_wait_and_resets_observation_without_changing_roots(self):
        c = self.controller()
        live.save({'mapping': {'min_battery_percent': 15, 'resume_battery_percent': 25}}, live.read()[1], self.base)
        self.assertFalse(c.wait_settings(60))
        c.reload_settings()
        self.assertEqual(c.c['mapping']['min_battery_percent'], 15)
        self.assertEqual(c.c['directories'], self.base['directories'])
        self.assertEqual(c.c['mysql'], self.base['mysql'])
        self.assertEqual(c.observation.count, 0)
        c.save.assert_called_once()
        self.assertEqual(c.config_revision, live.read()[1])

    def test_malformed_external_file_preserves_effective_config(self):
        c = self.controller()
        self.path.write_text('[mapping\n')
        c.reload_settings()
        self.assertEqual(c.c, self.base)
        self.assertIn('配置未应用', c.publish.call_args.kwargs['error'])

    def test_save_only_changes_submitted_keys_and_preserves_everything_else(self):
        before = '# keep this\n[scan]\ninterval = 30 # inline comment\nprogress_every = 19\n[mysql]\npassword = "keep-me"\n'
        self.path.write_text(before)
        live.save({'scan': {'interval': 60}}, live.read()[1])
        self.assertEqual(self.path.read_text(), before.replace('interval = 30', 'interval = 60'))
        self.assertEqual(live.read()[0]['scan']['progress_every'], 19)

    def test_noop_save_does_not_add_defaults_or_replace_inode(self):
        before = '# no defaults added\n[scan]\ninterval = 45\n'
        self.path.write_text(before)
        inode = self.path.stat().st_ino
        live.save(live.read()[0], live.read()[1])
        self.assertEqual(self.path.read_text(), before)
        self.assertEqual(self.path.stat().st_ino, inode)

    def test_direct_original_file_edit_is_hot_loaded(self):
        self.path.write_text('[scan]\ninterval = 35\n[mysql]\npassword = "unchanged-at-runtime"\n')
        c = self.controller()
        self.path.write_text(self.path.read_text().replace('interval = 35', 'interval = 70'))
        c.reload_settings()
        self.assertEqual(c.c['scan']['interval'], 70)
        self.assertEqual(c.c['mysql'], self.base['mysql'])

    def test_parse_diagnostics_do_not_quote_private_contents(self):
        self.path.write_text('[mysql]\npassword = private-secret-value')
        with self.assertRaises(ValueError) as raised:
            live.read()
        self.assertNotIn('private-secret-value', str(raised.exception))

    def test_missing_original_file_is_not_created(self):
        self.path.unlink()
        with self.assertRaises(FileNotFoundError):
            live.save({'scan': {'interval': 45}}, 'old-revision')
        self.assertFalse(self.path.exists())
