import threading
import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch
from file_sync import Controller, Observation, PhoneUnavailable
from configuration import load

class ManualReleaseTests(unittest.TestCase):
    def controller(self, phase='backup'):
        c = Controller.__new__(Controller)
        c.round = dict(id='batch', phase=phase, members={'large.mp4': [1,2,3,4]})
        c.manual_action_pending = threading.Event()
        c.database, c.connection = 'isolated', {}
        c.observation = Observation()
        c.c = load()
        c.source, c.target = '/source', '/target'
        c.publish = Mock()
        c.db = Mock()
        c.sync_status = Mock(return_value=dict(ready=True, token=[1]))
        c.reconcile_batch = Mock(return_value=c.round['members'])
        c.batch_snapshot = Mock(return_value=c.round['members'].copy())
        c.device = Mock(side_effect=nullcontext)
        c.phone_call = Mock(return_value=dict(action='freed'))
        c.flush_batch_reports = Mock()
        def save(phase=None):
            if phase:
                c.round['phase'] = phase
                c.observation.reset()
        c.save = Mock(side_effect=save)
        return c

    def tick(self, c):
        with patch('file_sync.ensure_storage'), patch('file_sync.TargetDirectory'):
            return c.tick()

    def test_request_commits_with_own_connection_before_acknowledging(self):
        c=self.controller()
        requested=dict(c.round,phase='manual_release_requested',manual_command={'id':'request','kind':'release','requested_at':1})
        with patch('file_sync.Inventory') as factory,patch('file_sync.BatchStore') as store:
            store.return_value.request_manual_action.return_value=requested
            response,status=c.request_release('batch')
            self.assertEqual(status,202)
            self.assertTrue(response['persisted'])
            self.assertEqual(response['phase'],'manual_release_requested')
            factory.assert_called_once_with('isolated',initialize=False)
            store.return_value.request_manual_action.assert_called_once_with('batch','release',None,None)
            factory.return_value.close.assert_called_once()
            self.assertEqual(c.round['phase'],'backup')
            self.assertTrue(c.manual_action_pending.is_set())

    def test_in_progress_release_rejects_duplicate_without_database_write(self):
        c=self.controller('manual_release_uncertain')
        c.release_running=threading.Event();c.release_running.set()
        with patch('file_sync.Inventory') as factory:
            self.assertEqual(c.request_release('batch')[1],409)
            factory.assert_not_called()

    def test_main_adopts_committed_command_and_acknowledges_current_stage(self):
        c=self.controller()
        c.manual_action_pending.set()
        requested=dict(c.round,phase='manual_release_requested',manual_command={'id':'new','requested_at':1})
        with patch('file_sync.BatchStore') as store:
            store.return_value.active.return_value=requested
            c.reload_manual_action()
        self.assertEqual(c.round['phase'],'manual_release_requested')
        self.assertFalse(c.manual_action_pending.is_set())
        self.assertEqual(c.publish.call_args.kwargs['manual_command_id'],'new')

    def test_requested_release_waits_for_sync_and_survives_restart(self):
        c=self.controller('manual_release_requested')
        c.sync_status.return_value['ready']=False
        self.tick(c)
        self.assertEqual(c.round['phase'],'manual_release_requested')
        c.phone_call.assert_not_called()
        c.sync_status.return_value['ready']=True
        self.tick(c)
        self.assertEqual(c.round['phase'],'manual_release_sync')

    def test_partial_release_waits_for_user_selection_and_never_cleans(self):
        c = self.controller('manual_release_requested')
        self.tick(c)
        c.phone_call.assert_called_once_with('release_space', execute=True, allow_incomplete=True)
        self.assertEqual(c.round['phase'], 'manual_release_sync')
        for stamp in (1000, 1300, 1600):
            with patch('file_sync.time.time', return_value=stamp):
                self.tick(c)
        self.assertEqual(c.round['phase'], 'manual_review')
        self.assertEqual(c.round['manual_remaining'], {'large.mp4': [1,2,3,4]})
        self.assertIn('manual_review_revision', c.round)
        self.assertNotIn('backup_confirmed', c.round)
        c.db.delete_relaxed_targets.assert_not_called()
        c.flush_batch_reports.assert_not_called()

    def test_ambiguous_release_is_not_replayed_after_restart(self):
        c = self.controller('manual_release_uncertain')
        c.phone_call.return_value={'state':'blocked','message':'设备过热'}
        with self.assertRaises(PhoneUnavailable):
            self.tick(c)
        c.phone_call.assert_called_once_with('status')
        c.publish.assert_any_call('photos', state='blocked', message='设备过热', observed_at=unittest.mock.ANY, read_error=None)

    def test_click_failure_leaves_persistent_uncertainty(self):
        c = self.controller('manual_release_requested')
        c.phone_call.side_effect = RuntimeError('lost response')
        with self.assertRaises(RuntimeError):
            self.tick(c)
        self.assertEqual(c.round['phase'], 'manual_release_uncertain')
        self.assertEqual(c.publish.call_args.kwargs, {'release_running': False})

    def test_phone_read_error_is_separate_from_release_error(self):
        c = self.controller()
        c.phone = Mock()
        c.phone.status.side_effect = RuntimeError('read failed')
        c.phone.release_space.side_effect = RuntimeError('release failed')
        with self.assertRaises(PhoneUnavailable):
            Controller.phone_call(c, 'status')
        c.publish.assert_called_once_with('photos', read_error='RuntimeError')
        c.publish.reset_mock()
        with self.assertRaises(PhoneUnavailable):
            Controller.phone_call(c, 'release_space', execute=True)
        c.publish.assert_not_called()

    def test_explicit_retained_file_does_not_block_next_batch_or_change_owner(self):
        c = self.controller()
        c.scan_target = Mock()
        c.snapshot = Mock(return_value={'large.mp4': [1,2,3,4]})
        c.db.batch_owners.return_value = {'large.mp4': 'old'}
        c.db.manual_retained_targets.return_value = {'large.mp4': [1,2,3,4]}
        c.db.pending.return_value = []
        self.assertFalse(c.new_round())
        c.db.claim_batch_paths.assert_not_called()
        c.db.pending.assert_called_once()
        c.snapshot.return_value = {'large.mp4': [1,2,30,4]}
        with self.assertRaisesRegex(ValueError, 'earlier batch'):
            c.new_round()

    def review(self):
        c = self.controller('manual_review')
        c.round.update(manual_remaining={'large.mp4': [1,2,3,4]}, manual_review_revision='revision', manual_review_sync_token=[1])
        return c

    def test_selected_deletion_is_journaled_and_waits_for_phone_sync(self):
        c = self.review()
        c.db.delete_user_selected_targets.return_value = [{'file':'large.mp4','action':'deleted'}]
        c.round.update(phase='manual_delete_requested',manual_delete_selection={'large.mp4':[1,2,3,4]})
        self.tick(c)
        self.assertEqual(c.round['phase'], 'manual_delete_sync')
        c.db.delete_user_selected_targets.assert_called_once()
        self.assertEqual(c.db.delete_user_selected_targets.call_args.args[3]['state'], 'user_selected_delete')
        self.assertNotIn('backup_confirmed', c.round)
        c.sync_status.return_value['ready'] = False
        self.tick(c)
        self.assertEqual(c.round['phase'], 'manual_delete_sync')
        c.sync_status.return_value['ready'] = True
        c.batch_snapshot.return_value = {}
        self.tick(c)
        self.assertEqual(c.round['phase'], 'complete')
        c.flush_batch_reports.assert_called_once()

    def test_restart_does_not_replay_selected_unlink(self):
        c = self.controller('manual_delete')
        c.reconcile_intents = Mock()
        self.tick(c)
        c.db.delete_user_selected_targets.assert_not_called()
        self.assertEqual(c.round['phase'], 'manual_release_sync')

    def test_changed_selected_file_is_kept(self):
        c = self.review()
        c.round.update(phase='manual_delete_requested',manual_delete_selection={'large.mp4':[1,2,3,4]})
        c.batch_snapshot.return_value = {'large.mp4':[1,2,30,4]}
        with self.assertRaises(PhoneUnavailable):self.tick(c)
        c.db.delete_user_selected_targets.assert_not_called()
        self.assertEqual(c.round['phase'],'manual_release_sync')
