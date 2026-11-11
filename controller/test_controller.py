import tempfile
from pathlib import Path
import unittest
from configuration import load, redacted
from file_sync import Observation, walk, Stopped
import threading

class ConfigTests(unittest.TestCase):
    def config(self, text):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, 'config.toml')
            path.write_text(text)
            return load(path)
    def test_battery_threshold_configuration(self):
        self.assertEqual(self.config('')['mapping']['min_battery_percent'], 10)
        self.assertEqual(self.config('[mapping]\nmin_battery_percent=20')['mapping']['min_battery_percent'], 20)
        for value in ('0', '-1', '101', 'true', '10.5'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.config('[mapping]\nmin_battery_percent=' + value)

    def test_resume_battery_threshold(self):
        self.assertEqual(self.config('')['mapping']['resume_battery_percent'], 20)
        for value in ('0', '9', '101', 'true', '20.5'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.config('[mapping]\nresume_battery_percent=' + value)
        self.assertEqual(self.config('[mapping]\nresume_battery_percent=30')['mapping']['resume_battery_percent'], 30)

    def test_defaults(self):
        self.assertEqual(self.config('')['mapping']['max_bytes'], 15 * 1024**3)
        self.assertEqual(self.config('')['bark']['error_threshold'], 2)
    def test_error_threshold_is_configurable_and_positive_integer(self):
        self.assertEqual(self.config('[bark]\nerror_threshold=3')['bark']['error_threshold'], 3)
        for value in ('0', '-1', 'true', '2.5'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.config('[bark]\nerror_threshold=' + value)
    def test_inline_secrets_are_redacted(self):
        config = self.config('[mysql]\npassword="test-password"\n[bark]\nkey="test-bark-key"')
        self.assertEqual(config['mysql']['password'], 'test-password')
        safe = redacted(config)
        self.assertNotIn('test-password', str(safe))
        self.assertNotIn('test-bark-key', str(safe))
        self.assertEqual(config['bark']['key'], 'test-bark-key')
    def test_legacy_env_reference_rejected(self):
        with self.assertRaises(ValueError):
            self.config('[mysql]\npassword_env="OLD_ENV"')
    def test_fixed_roots(self):
        with self.assertRaises(ValueError):
            self.config('[directories]\ntarget="/photos/source"')
    def test_protection_cannot_be_disabled(self):
        with self.assertRaises(ValueError):
            self.config('[directories]\nprotect_source=false')
    def test_short_observation_rejected(self):
        with self.assertRaises(ValueError):
            self.config('[observation]\ninterval=10')
    def test_invalid_type(self):
        with self.assertRaises(ValueError):
            self.config('[scan]\ninterval=true')

class ObservationTests(unittest.TestCase):
    def test_three_samples_at_zero_five_ten_minutes(self):
        obs = Observation()
        self.assertEqual([obs.sample(True, 'same', t) for t in (0, 60, 299, 300, 599, 600)], [1, 1, 1, 2, 2, 3])
    def test_changed_sync_or_file_resets(self):
        obs = Observation()
        obs.sample(True, ['version1', {'a': 1}], 0)
        obs.sample(True, ['version1', {'a': 1}], 300)
        self.assertEqual(obs.sample(True, ['version2', {'a': 1}], 600), 1)
        self.assertEqual(obs.sample(False, None, 900), 0)
        self.assertEqual(obs.sample(True, 'new', 1200), 1)
    def test_restart_resets(self):
        obs = Observation()
        obs.sample(True, 'token', 0)
        self.assertEqual(Observation().sample(True, 'token', 300), 1)
    def test_stop_aborts_traversal(self):
        stop = threading.Event()
        stop.set()
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(Stopped):
            list(walk(folder, stop))


class ControllerTests(unittest.TestCase):
    def controller(self):
        from unittest.mock import Mock
        from file_sync import Controller
        value = Controller.__new__(Controller)
        value.scan_lock = __import__("threading").Lock()
        value.scan_requested = __import__("threading").Event()
        value.scan_running = False
        value.mapping_allowed = Mock(return_value=True)
        value.c = load()
        value.events = __import__('queue').SimpleQueue()
        value.source, value.target = '/source', '/target'
        value.db = Mock()
        value.db.batch_owners.return_value = {}
        from contextlib import nullcontext
        value.db.transaction.side_effect = nullcontext
        value.save = Mock()
        value.scan_target = Mock()
        value.reconcile_batch = Mock()
        value.observation = Observation()
        value.round = None
        value.flush_batch_reports = Mock()
        return value
    def test_existing_targets_freeze_without_pending_query(self):
        from unittest.mock import Mock
        value = self.controller()
        value.snapshot = Mock(return_value={'old.jpg': [1, 2, 3, 4]})
        self.assertTrue(value.new_round())
        self.assertEqual(value.round['members'], {'old.jpg': [1, 2, 3, 4]})
        value.db.pending.assert_not_called()
    def test_candidates_frozen_before_mapping(self):
        from unittest.mock import Mock
        value = self.controller()
        value.snapshot = Mock(return_value={})
        value.db.pending.side_effect = [[dict(source_path='first.jpg', mtime_ns=1,
            source_dev=2, source_ino=3, size_bytes=4)], []]
        self.assertTrue(value.new_round())
        self.assertEqual([r['source_path'] for r in value.round['candidates']], ['first.jpg'])
        self.assertEqual(value.round['phase'], 'mapping')
        self.assertTrue(value.db.pending.call_args.kwargs['unassigned_only'])
        value.save.assert_called_once()

    def test_owned_target_is_never_adopted_again_even_if_changed(self):
        from unittest.mock import Mock
        value = self.controller()
        value.snapshot = Mock(return_value={'old.jpg': [9, 8, 7, 6]})
        value.db.batch_owners.return_value = {'old.jpg': 'original-batch'}
        with self.assertRaisesRegex(ValueError, 'earlier batch'):
            value.new_round()
        value.save.assert_not_called()
        value.db.pending.assert_not_called()

    def test_failed_ownership_claim_cannot_resume_uncommitted_batch(self):
        from unittest.mock import Mock
        value = self.controller()
        value.snapshot = Mock(return_value={'old.jpg': [1, 2, 3, 4]})
        value.save.side_effect = ValueError('ownership conflict')
        with self.assertRaises(ValueError):
            value.new_round()
        self.assertIsNone(value.round)
    def test_source_scanner_uses_own_connection(self):
        from unittest.mock import Mock
        from file_sync import Stopped
        value = self.controller()
        value.stop = threading.Event()
        scanner_db = Mock()
        scanner_db.scan_source.side_effect = Stopped()
        value.connect = Mock(return_value=scanner_db)
        from unittest.mock import patch
        with patch('file_sync.os.listdir', return_value=[]):
            value.scanner()
        scanner_db.scan_source.assert_called_once()
        value.db.scan_source.assert_not_called()
        scanner_db.close.assert_called_once()

    def test_cleanup_only_after_three_stable_samples(self):
        from contextlib import nullcontext
        from unittest.mock import Mock, patch
        value = self.controller()
        value.round = {'id': 'test-round', 'phase': 'observe', 'members': {'a.jpg': [1, 2, 3, 4]}}
        value.batch_snapshot = Mock(return_value={'a.jpg': [1, 2, 3, 4]})
        value.sync = Mock()
        value.sync.settled_state.return_value = {'ready': True, 'token': [1, 2, 3]}
        value.phone = Mock()
        value.phone.free_page.return_value = (None, None)
        value.device_lock = 'unused'
        value.notify = Mock()
        value.db.delete_relaxed_targets.return_value = [{'action': 'deleted'}]
        with patch('file_sync.TargetDirectory', return_value=nullcontext()), \
                patch('file_sync.lock', return_value=nullcontext()), \
                patch('file_sync.backup_status', return_value={'state': 'complete'}):
            for stamp in (1000, 1300):
                with patch('file_sync.time.time', return_value=stamp):
                    value.tick()
                value.db.delete_relaxed_targets.assert_not_called()
            with patch('file_sync.time.time', return_value=1600):
                value.tick()
        value.db.delete_relaxed_targets.assert_called_once()
        self.assertEqual(value.db.delete_relaxed_targets.call_args.args[2], {'a.jpg': [1, 2, 3, 4]})

    def test_closed_connection_does_not_prevent_reconnect(self):
        from unittest.mock import Mock, patch
        value = self.controller()
        value.reload_settings = Mock()
        value.tick = Mock(side_effect=RuntimeError('database failed'))
        value.db.close.side_effect = RuntimeError('already closed')
        replacement = Mock()
        value.connect = Mock(return_value=replacement)
        value.notify = Mock()
        value.stop = threading.Event()
        with patch('file_sync.threading.Thread'):
            value.run(once=True)
        value.connect.assert_called_once()
        self.assertIs(value.batches.db, replacement)
        replacement.close.assert_called_once()

    def test_phone_backup_is_polled_while_sync_incomplete_without_releasing(self):
        from contextlib import nullcontext
        from unittest.mock import Mock, patch
        for phone_state in ('syncing', 'complete'):
            value = self.controller()
            value.round = {'id':'batch','phase':'sync','members':{'a.jpg':[1,2,3,4]}}
            value.sync_status = Mock(return_value={'ready':False})
            value.phone = Mock()
            value.phone.status.return_value = {'state':phone_state,'message':phone_state,'details':['正在备份']}
            value.device = Mock(side_effect=nullcontext)
            value.publish = Mock()
            value.confirm_batch_backup = Mock()
            with patch('file_sync.ensure_storage'),patch('file_sync.TargetDirectory',side_effect=lambda *a:nullcontext()):
                value.tick()
            value.phone.status.assert_called_once()
            value.phone.release_space.assert_not_called()
            value.confirm_batch_backup.assert_not_called()
            value.reconcile_batch.assert_not_called()
            self.assertEqual(value.round['phase'],'sync')
            self.assertTrue(any(c.args[0]=='photos' and c.kwargs['state']==phone_state for c in value.publish.call_args_list))

    def test_changed_members_are_deferred_to_next_round(self):
        from contextlib import nullcontext
        from unittest.mock import Mock, patch
        value = self.controller()
        value.round = {'id': 'round', 'phase': 'final_sync', 'members': {'a.jpg': [1, 2, 3, 4]}, 'deferred': ['a.jpg']}
        value.batch_snapshot = Mock(return_value={'a.jpg': [1, 2, 5, 6]})
        value.sync = Mock()
        value.sync.settled_state.return_value = {'ready': True}
        value.notify = Mock()
        with patch('file_sync.TargetDirectory', return_value=nullcontext()):
            value.tick()
        value.save.assert_called_with('complete')
        value.snapshot = Mock(return_value={'a.jpg': [1, 2, 5, 6]})
        value.new_round()
        self.assertEqual(value.round['members'], {'a.jpg': [1, 2, 5, 6]})
        self.assertNotIn('deferred', value.round)

    def test_missing_frozen_source_does_not_stall_mapping(self):
        from unittest.mock import patch, Mock
        value = self.controller()
        value.stop = threading.Event()
        value.round = {'id': 'round', 'members': {}, 'candidates': [{'source_path': 'gone.jpg'}]}
        value.notify = Mock()
        with patch('file_sync.os.lstat', side_effect=FileNotFoundError):
            value.map_round()
        value.save.assert_called_with('complete')
        self.assertEqual(value.round['mapping_skipped'], {'gone.jpg': 'source_missing'})
        value.db.record_results.assert_not_called()

if __name__ == '__main__':
    unittest.main()
