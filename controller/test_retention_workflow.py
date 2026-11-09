import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from cleanup_target import fingerprint, process_files
from retention_workflow import retained, wait_settled, run_workflow


class RetentionTests(unittest.TestCase):
    def test_new_changed_and_removed_files_are_excluded(self):
        with tempfile.TemporaryDirectory() as root:
            for name in ['retained', 'changed', 'new']:
                Path(root, name).write_bytes(b'abc')
            before = {name: list(fingerprint(Path(root, name))) for name in ['retained', 'changed']}
            Path(root, 'changed').write_bytes(b'different')
            self.assertEqual(retained(before, {'retained', 'changed', 'new'}, root, fingerprint), ['retained'])
            self.assertEqual(retained(before, set(), root, fingerprint), [])

    def args(self, root):
        return SimpleNamespace(sync_timeout=3, poll_interval=1, stable_samples=2,
            target=root, pixel_root='/storage/emulated/0/DCIM', execute=True,
            syncthing_config='unused', syncthing_folder='test', pixel_device='PIXEL',
            file=None, limit=2, source='/source', reason='photos_retained')

    def test_pending_remote_deletion_cannot_be_retention(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, 'photo').write_text('still on NAS')
            args = self.args(root)
            sync = Mock()
            sync.settled_state.return_value = {'ready': True, 'token': [1]}
            clock = Mock(side_effect=[0, 0, 1, 2, 3])
            with patch('retention_workflow.pixel_files', return_value=set()):
                with self.assertRaises(TimeoutError):
                    wait_settled(sync, Mock(), args, before={'photo': [1]}, clock=clock, sleep=lambda _: None)

    def test_requires_consecutive_stable_samples(self):
        sync = Mock()
        sync.settled_state.side_effect = [{'ready': True, 'token': [1]},
            {'ready': False, 'token': [1]}, {'ready': True, 'token': [2]}, {'ready': True, 'token': [2]}]
        args = self.args('/unused')
        args.sync_timeout = 100
        wait_settled(sync, Mock(), args, clock=lambda: 0, sleep=lambda _: None)
        self.assertEqual(sync.settled_state.call_count, 4)

    def test_uncertain_release_never_checks_or_deletes_survivors(self):
        with tempfile.TemporaryDirectory() as root:
            args = self.args(root)
            db, phone, process = Mock(), Mock(), Mock()
            db.query.return_value = []
            phone.release_space.return_value = {'action': 'verification_required'}
            with patch('retention_workflow.REPORT_DIRECTORY', Path(root, 'reports')), \
                 patch('retention_workflow.SyncthingState'), \
                 patch('retention_workflow.wait_settled', return_value=(set(), {})), \
                 patch('retention_workflow.pixel_files', return_value=set()):
                with self.assertRaises(RuntimeError):
                    run_workflow(args, db, phone, process, fingerprint, lambda _: None)
            process.assert_not_called()

    def test_confirmed_release_passes_only_unchanged_survivors(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, 'kept.jpg').write_bytes(b'abc')
            args = self.args(root)
            db, phone, process = Mock(), Mock(), Mock(return_value=0)
            db.query.return_value = [{'target_path': 'kept.jpg'}]
            phone.release_space.return_value = {'action': 'nothing_to_free'}
            with patch('retention_workflow.REPORT_DIRECTORY', Path(root, 'reports')), \
                 patch('retention_workflow.SyncthingState'), \
                 patch('retention_workflow.wait_settled', return_value=({'kept.jpg'}, {})), \
                 patch('retention_workflow.pixel_files', return_value={'kept.jpg'}):
                self.assertEqual(run_workflow(args, db, phone, process, fingerprint, lambda _: None), 0)
            self.assertEqual(process.call_args.args[3], ['kept.jpg'])
            self.assertEqual(process.call_args.kwargs['retention'][0]['release_action'], 'nothing_to_free')

    def test_unconfirmed_photo_never_becomes_manual_candidate(self):
        args = self.args('/unused')
        db, output = Mock(), Mock()
        db.query.return_value = [{'status': 'linked', 'target_path': 'photo.jpg'}]
        proof = {'state': 'not_confirmed'}
        with patch('cleanup_target.inspect_backup', return_value=(proof, (1, 2, 3, 4))):
            process_files(args, db, Mock(), ['photo.jpg'],
                          retention=({'run_id': 'test'}, {'photo.jpg': [1, 2, 3, 4]}), output=output)
        db.delete_confirmed_target.assert_not_called()
        self.assertEqual(output.call_args.args[0]['action'], 'kept')

    def test_preview_never_executes_release_or_assigns_candidates(self):
        with tempfile.TemporaryDirectory() as root:
            args = self.args(root)
            args.execute = False
            phone, process = Mock(), Mock()
            phone.release_space.return_value = {'action': 'preview'}
            with patch('retention_workflow.REPORT_DIRECTORY', Path(root, 'reports')), \
                 patch('retention_workflow.SyncthingState') as factory:
                factory.return_value.settled_state.return_value = {'ready': True}
                run_workflow(args, Mock(), phone, process, fingerprint, lambda _: None)
            phone.release_space.assert_called_once_with(execute=False)
            process.assert_not_called()
