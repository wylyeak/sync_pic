import queue
import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch
from file_sync import Controller, Observation, PhoneUnavailable
from configuration import load

class ManualReleaseTests(unittest.TestCase):
    def controller(self, phase='backup'):
        c = Controller.__new__(Controller)
        c.round = dict(id='batch', phase=phase, members={'large.mp4': [1,2,3,4]})
        c.release_requests = queue.Queue(maxsize=1)
        c.delete_requests = queue.Queue(maxsize=1)
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

    def test_request_validation_duplicate_and_incomplete_sync(self):
        c = self.controller()
        self.assertEqual(c.request_release('stale')[1], 409)
        self.assertEqual(c.request_release('batch')[1], 202)
        self.assertEqual(c.request_release('batch')[1], 409)
        c.sync_status.return_value['ready'] = False
        with self.assertRaisesRegex(Exception, '同步尚未完成'):
            self.tick(c)
        c.phone_call.assert_not_called()
        self.assertEqual(c.round['phase'], 'backup')

    def test_partial_release_waits_for_user_selection_and_never_cleans(self):
        c = self.controller()
        c.request_release('batch')
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
        with self.assertRaises(PhoneUnavailable):
            self.tick(c)
        c.phone_call.assert_not_called()
        self.assertEqual(c.request_release('batch')[1], 202)

    def test_click_failure_leaves_persistent_uncertainty(self):
        c = self.controller()
        c.request_release('batch')
        c.phone_call.side_effect = RuntimeError('lost response')
        with self.assertRaises(RuntimeError):
            self.tick(c)
        self.assertEqual(c.round['phase'], 'manual_release_uncertain')

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

    def test_selection_validates_revision_members_and_duplicate_request(self):
        c = self.review()
        self.assertEqual(c.request_selected_delete('batch', 'stale', ['large.mp4'])[1], 409)
        self.assertEqual(c.request_selected_delete('batch', 'revision', ['other.mp4'])[1], 409)
        self.assertEqual(c.request_selected_delete('batch', 'revision', [1])[1], 400)
        self.assertEqual(c.request_selected_delete('batch', 'revision', [])[1], 400)
        self.assertEqual(c.request_selected_delete('batch', 'revision', ['large.mp4'])[1], 202)
        self.assertEqual(c.request_selected_delete('batch', 'revision', ['large.mp4'])[1], 409)

    def test_selected_deletion_is_journaled_and_waits_for_phone_sync(self):
        c = self.review()
        c.db.delete_user_selected_targets.return_value = [{'file':'large.mp4','action':'deleted'}]
        c.request_selected_delete('batch','revision',['large.mp4'])
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
        c.request_selected_delete('batch','revision',['large.mp4'])
        c.batch_snapshot.return_value = {'large.mp4':[1,2,30,4]}
        with self.assertRaises(PhoneUnavailable):self.tick(c)
        c.db.delete_user_selected_targets.assert_not_called()
        self.assertEqual(c.round['phase'],'manual_release_sync')
