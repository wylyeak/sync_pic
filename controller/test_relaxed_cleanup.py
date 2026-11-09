from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

from cleanup_target import fingerprint
from relaxed_cleanup import run_relaxed, snapshot_targets


class RelaxedTests(unittest.TestCase):
    def run_case(self, execute=False, ready=True, backup='备份已完成', button=None, changed=False):
        with tempfile.TemporaryDirectory() as root:
            Path(root, 'photo.jpg').write_text('data')
            args = SimpleNamespace(source='/source', target=root, file=None, execute=execute,
                syncthing_config='unused', syncthing_folder='folder', pixel_device='PIXEL')
            db, phone = Mock(), Mock()
            phone.menu.return_value = ET.fromstring('<hierarchy><node clickable="true"><node text="备份"/><node text="'+backup+'"/></node></hierarchy>')
            phone.free_page.return_value = (ET.fromstring('<hierarchy/>'), button)
            db.delete_relaxed_targets.return_value = [{'file': 'photo.jpg', 'action': 'deleted'}]
            with patch('relaxed_cleanup.REPORT_DIRECTORY', Path(root, 'reports')), \
                 patch('relaxed_cleanup.SyncthingState') as factory:
                factory.return_value.settled_state.side_effect = [
                    {'ready': ready, 'token': [1]}, {'ready': True, 'token': [2] if changed else [1]}]
                # Store reports outside the tree being cleaned in real use;
                # the test report subfolder is harmless and can be ignored.
                args.file = ['photo.jpg']
                try:
                    code = run_relaxed(args, db, phone, fingerprint, lambda _: None)
                except RuntimeError:
                    code = 1
            return code, db, phone

    def test_preview_does_not_delete(self):
        code, db, phone = self.run_case()
        self.assertEqual(code, 0)
        db.delete_relaxed_targets.assert_not_called()
        phone.release_space.assert_not_called()

    def test_three_aggregate_gates_authorize_one_batch(self):
        code, db, phone = self.run_case(execute=True)
        self.assertEqual(code, 0)
        db.delete_relaxed_targets.assert_called_once()
        proof = db.delete_relaxed_targets.call_args.args[3]
        self.assertEqual(proof['state'], 'aggregate_confirmed')
        self.assertEqual(set(db.delete_relaxed_targets.call_args.args[2]), {'photo.jpg'})
        phone.adb.assert_not_called()

    def test_each_failed_gate_prevents_delete(self):
        for options in [{'ready': False}, {'backup': '正在备份'}, {'button': '释放 1 GB 空间'}, {'changed': True}]:
            with self.subTest(options=options):
                code, db, _ = self.run_case(execute=True, **options)
                self.assertEqual(code, 1)
                db.delete_relaxed_targets.assert_not_called()

    def test_metadata_and_symlinks_are_excluded(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, '.stfolder').mkdir()
            Path(root, '.stfolder', 'marker').write_text('keep')
            Path(root, '.stignore').write_text('keep')
            Path(root, 'photo.jpg').write_text('photo')
            Path(root, 'link').symlink_to(Path(root, 'photo.jpg'))
            self.assertEqual(set(snapshot_targets(root, set(), fingerprint)), {'photo.jpg'})
