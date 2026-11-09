import os
import tempfile
import unittest
from unittest.mock import Mock
import xml.etree.ElementTree as ET

from cleanup_target import inspect_backup, sha256_file
from syncthing_state import SyncthingState


class ProofTests(unittest.TestCase):
    def verify(self, xml, digest_override=None):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, 'photo.jpg')
            with open(path, 'wb') as stream:
                stream.write(b'photo bytes')
            digest = digest_override or sha256_file(path)
            phone = Mock()
            phone.adb.side_effect = [digest + '  /storage/emulated/0/DCIM/photo.jpg\nRow: 0 _id=42, mime_type=image/jpeg\n',
                                     digest + '  /storage/emulated/0/DCIM/photo.jpg\n']
            phone.dump.return_value = ET.fromstring(xml)
            return inspect_backup(phone, root, 'photo.jpg', '/storage/emulated/0/DCIM')[0]

    def test_individual_label_and_filename_required(self):
        xml = '<hierarchy><node resource-id="x/collapse_info_panel_fab"/><node text="photo.jpg"/><node text="已备份"/></hierarchy>'
        self.assertEqual(self.verify(xml)['state'], 'confirmed')
        self.assertEqual(self.verify(xml.replace('photo.jpg', 'other.jpg'))['state'], 'not_confirmed')
        self.assertEqual(self.verify(xml.replace('已备份', '备份已完成'))['state'], 'not_confirmed')
        self.assertEqual(self.verify(xml.replace('已备份', '未备份'))['state'], 'not_confirmed')

    def test_mismatched_phone_bytes_rejected(self):
        with self.assertRaises(ValueError):
            self.verify('<hierarchy/>', digest_override='0'*64)

    def test_remote_provenance_needs_matching_tombstones(self):
        client = SyncthingState.__new__(SyncthingState)
        client.remote = 'PIXEL'
        item = {'name': 'photo.jpg', 'deleted': True, 'modifiedBy': 'PIXEL'}
        client.file = Mock(return_value={'local': item, 'global': item})
        self.assertIsNotNone(client.remote_deletion('photo.jpg'))
        client.file.return_value = {'local': item, 'global': dict(item, modifiedBy='HOST')}
        self.assertIsNone(client.remote_deletion('photo.jpg'))
        client.file.return_value = {'local': item, 'global': dict(item, deleted=False)}
        self.assertIsNone(client.remote_deletion('photo.jpg'))

    def test_settled_requires_connected_and_complete_both_sides(self):
        client = SyncthingState.__new__(SyncthingState)
        client.folder, client.remote_id = 'folder', 'PIXEL'
        local = {'state': 'idle', 'needTotalItems': 0, 'pullErrors': 0}
        remote = {'needItems': 0, 'needDeletes': 0, 'completion': 100, 'remoteState': 'valid'}
        connections = {'connections': {'PIXEL': {'connected': True}}}
        client.get = Mock(side_effect=[local, remote, connections])
        self.assertTrue(client.settled_state()['ready'])
        for incomplete in [dict(remote, needItems=1), dict(remote, needDeletes=1), dict(remote, remoteState='paused')]:
            client.get = Mock(side_effect=[local, incomplete, connections])
            self.assertFalse(client.settled_state()['ready'])
