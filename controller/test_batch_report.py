import json
import threading
import unittest
from unittest.mock import Mock, patch
from batch_report import transition, summary
from configuration import load
from file_sync import Controller, Observation, ensure_storage, StorageUnavailable, SyncthingUnavailable, PhoneUnavailable


def controller():
    c = Controller.__new__(Controller)
    c.c = load()
    c.c['bark']['enabled'] = True
    c.db = Mock()
    c.key = 'test-only'
    c.observation = Observation()
    c.round = {'id': 'batch', 'phase': 'mapping', 'members': {'a': [1], 'b': [2]},
               'started_at': 100, 'phase_started_at': 100, 'phase_seconds': {}}
    return c

class BatchReportTests(unittest.TestCase):
    def test_timing_accumulates_revisited_phases_and_survives_restart(self):
        batch = controller().round
        transition(batch, 'sync', 110)
        transition(batch, 'backup', 130)
        transition(batch, 'observe', 150)
        batch = json.loads(json.dumps(batch))
        transition(batch, 'final_sync', 750)
        transition(batch, 'observe', 760)
        transition(batch, 'complete', 1060)
        report = summary(batch, 1)
        self.assertEqual(report['phase_seconds']['mapping'], 10)
        self.assertEqual(report['phase_seconds']['sync'], 20)
        self.assertEqual(report['phase_seconds']['observe'], 900)
        self.assertEqual(sum(report['phase_seconds'].values()), 960)
        self.assertEqual(report['ended_at'], 1060)
    def test_backup_count_excludes_changed_and_unconfirmed_members(self):
        c = controller()
        c.batches = Mock()
        c.confirm_batch_backup({'a': [1], 'b': [99], 'outside': [3]})
        self.assertEqual(set(c.round['backup_confirmed']), {'a'})
        c.round['deferred'] = ['a']
        transition(c.round, 'complete', 200)
        self.assertEqual(summary(c.round, 0)['backed_up'], 0)
    def test_legacy_timing_and_backup_are_not_invented(self):
        batch = {'id': 'old', 'phase': 'observe', 'members': {'a': [1]}, 'started_at': 10}
        transition(batch, None, 1000)
        transition(batch, 'complete', 1300)
        report = summary(batch, 0)
        self.assertEqual(report['phase_seconds']['observe'], 300)
        self.assertIsNone(report['backed_up'])
        self.assertIn('升级前', report['body'])
    def test_completion_persists_report_and_counts_successful_batch_audit(self):
        c = controller()
        c.batches = Mock()
        c.db.query.return_value = [{'source_path': 'a'}, {'source_path': 'a'}, {'source_path': 'outside'}]
        c.round['backup_confirmed'] = {'a': [1], 'b': [2]}
        with patch('file_sync.time.time', return_value=200):
            c.save('complete')
        stored = c.batches.save.call_args.args[0]
        self.assertEqual(stored['summary']['manual_deleted'], 1)
        self.assertEqual(stored['summary']['backed_up'], 2)
        self.assertIn("state='deleted'", c.db.query.call_args.args[0])
        self.assertEqual(c.db.query.call_args.args[1], ('batch',))
    def test_failed_completion_commit_restores_active_phase(self):
        c = controller()
        c.batches = Mock()
        c.batches.save.side_effect = RuntimeError('database unavailable')
        c.db.query.return_value = []
        with self.assertRaises(RuntimeError):
            c.save('complete')
        self.assertEqual(c.round['phase'], 'mapping')
        self.assertNotIn('summary', c.round)
        self.assertNotIn('ended_at', c.round)

    def test_pending_report_can_be_retried_after_next_batch_started(self):
        c = controller()
        c.batches = Mock()
        c.db.query.return_value = [{'id': 'previous', 'summary': json.dumps({'body': 'saved report'})}]
        c.notify = Mock()
        c.flush_batch_reports()
        c.flush_batch_reports()
        self.assertEqual(c.notify.call_count, 2)
        c.notify.assert_called_with('previous-summary', 'saved report')
    def test_normal_waiting_has_no_exception_alert(self):
        c = controller()
        c.batches = Mock()
        c.sync = Mock()
        c.sync.settled_state.return_value = {'ready': False, 'connected': True, 'local_state': 'scanning'}
        self.assertFalse(c.sync_status()['ready'])
        self.assertEqual(c.photos_status({'state': 'syncing'})['state'], 'syncing')
    def test_syncthing_offline_and_pull_errors_are_reportable_immediately(self):
        c = controller()
        c.batches = Mock()
        c.sync = Mock()
        for state in ({'connected': False}, {'connected': True, 'pull_errors': 2}):
            c.sync.settled_state.return_value = state
            with self.assertRaises(SyncthingUnavailable):
                c.sync_status()
    def test_phone_blocked_or_unknown_are_reportable(self):
        c = controller()
        c.batches = Mock()
        for state in ('blocked', 'unknown'):
            with self.assertRaises(PhoneUnavailable):
                c.photos_status({'state': state})
    def test_missing_nas_mount_is_detected_without_scanning(self):
        from unittest.mock import mock_open
        with patch('builtins.open', mock_open(read_data='20 1 0:1 / / rw - ext4 /dev/root rw\n')):
            with self.assertRaises(StorageUnavailable):
                ensure_storage('/photos/source')
    def test_alert_identifies_batch_and_phase(self):
        c = controller()
        c.batches = Mock()
        c.notify = Mock()
        c.alert('error-PhoneUnavailable', '手机异常')
        args = c.notify.call_args
        self.assertEqual(args.args[0], 'batch-error-PhoneUnavailable')
        self.assertIn('批次：batch', args.args[1])
        self.assertIn('阶段：建立硬链接', args.args[1])
        self.assertTrue(args.kwargs['repeat'])

class ConsecutiveAlertTests(unittest.TestCase):
    def run_steps(self, steps, threshold=2, delivered=True, source_events=()):
        import queue
        c = controller()
        c.c['bark']['error_threshold'] = threshold
        c.batches = Mock()
        c.stop = Mock()
        c.stop.is_set.return_value = False
        c.stop.wait.side_effect = [False] * (len(steps)-1) + [True]
        c.events = queue.SimpleQueue()
        for event in source_events:
            c.events.put(event)
        c.refresh_battery = Mock()
        c.flush_batch_reports = Mock()
        c.tick = Mock(side_effect=steps)
        c.notify = Mock(return_value=delivered)
        c.connect = Mock(return_value=Mock())
        with patch('file_sync.threading.Thread'):
            c.run()
        return c

    def test_single_error_and_recovery_are_silent(self):
        c = self.run_steps([PhoneUnavailable('ADB 连接失败'), 60])
        c.notify.assert_not_called()

    def test_second_error_notifies_and_recovery_follows(self):
        c = self.run_steps([PhoneUnavailable('first'), PhoneUnavailable('second'), 60])
        self.assertEqual([x.args[0] for x in c.notify.call_args_list],
                         ['batch-error-PhoneUnavailable', 'batch-recovered-PhoneUnavailable'])
        self.assertIn('second', c.notify.call_args_list[0].args[1])

    def test_success_resets_count(self):
        c = self.run_steps([PhoneUnavailable('first'), 60, PhoneUnavailable('again'), 60])
        c.notify.assert_not_called()

    def test_configurable_threshold_and_different_error_types(self):
        c = self.run_steps([PhoneUnavailable('one'), SyncthingUnavailable('two'),
                            StorageUnavailable('three')], threshold=3)
        c.notify.assert_called_once()
        self.assertEqual(c.notify.call_args.args[0], 'batch-error-StorageUnavailable')

    def test_threshold_one_restores_immediate_notification(self):
        c = self.run_steps([PhoneUnavailable('first')], threshold=1)
        c.notify.assert_called_once()

    def test_failed_push_does_not_generate_recovery_push(self):
        c = self.run_steps([PhoneUnavailable('one'), PhoneUnavailable('two'), 60], delivered=False)
        c.notify.assert_called_once()

    def test_source_recovery_requires_delivered_or_already_reported_error(self):
        events = [('source-error-OSError', 'source failed'), ('source-recovered', 'source recovered')]
        c = self.run_steps([60], delivered=False, source_events=events)
        c.notify.assert_called_once()
        c = self.run_steps([60], delivered=True, source_events=events)
        self.assertEqual([x.args[0] for x in c.notify.call_args_list],
                         ['batch-source-error-OSError', 'batch-source-recovered'])

if __name__ == '__main__':
    unittest.main()
