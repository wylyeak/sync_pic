import unittest
from unittest.mock import Mock
from syncthing_state import SyncthingState

class ProgressTests(unittest.TestCase):
    def state(self,completion=76.7,deletes=0,connected=True):
        s=SyncthingState.__new__(SyncthingState)
        s.folder='folder';s.remote_id='pixel'
        s.get=Mock(side_effect=[{'state':'idle','needTotalItems':0,'pullErrors':0,'globalBytes':1000,'needBytes':0,'version':1,'globalTotalItems':20},
            {'completion':completion,'globalBytes':1000,'globalItems':10,'needBytes':233 if completion<100 else 0,'needItems':2 if completion<100 else 0,'needDeletes':deletes,'remoteState':'valid','sequence':1},
            {'connections':{'pixel':{'connected':connected}}}])
        return s.settled_state()
    def test_reports_actual_api_progress_and_remaining(self):
        result=self.state()
        self.assertEqual(result['completion'],76.7)
        self.assertEqual(result['need_remote_bytes'],233)
        self.assertEqual(result['need_remote'],2)
        self.assertFalse(result['ready'])
    def test_full_bytes_with_pending_deletion_is_not_ready(self):
        result=self.state(100,deletes=1)
        self.assertEqual(result['completion'],100)
        self.assertEqual(result['need_remote_deletes'],1)
        self.assertFalse(result['ready'])
    def test_disconnected_full_progress_is_not_ready(self):
        self.assertFalse(self.state(100,connected=False)['ready'])

if __name__=='__main__':unittest.main()
