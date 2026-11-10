import queue
import unittest
from unittest.mock import Mock, patch
from configuration import load
from file_sync import Controller


class ScannerAlertThresholdTests(unittest.TestCase):
    def scan(self, steps, threshold=2):
        c = Controller.__new__(Controller)
        c.c = load()
        c.c['bark']['error_threshold'] = threshold
        c.source, c.target = '/source', '/target'
        c.events = queue.SimpleQueue()
        c.stop = Mock()
        c.stop.is_set.return_value = False
        c.stop.wait.side_effect = [False] * (len(steps)-1) + [True]
        c.wait_scan = c.stop.wait
        db = Mock()
        db.scan_source.side_effect = steps
        c.connect = Mock(return_value=db)
        c.publish = Mock()
        with patch('file_sync.os.listdir', return_value=[]):
            c.scanner()
        result = []
        while not c.events.empty():
            result.append(c.events.get()[0])
        return result

    def test_scanner_single_failure_and_recovery_are_silent(self):
        self.assertEqual(self.scan([OSError('temporary'), None]), [])

    def test_scanner_repeated_failure_and_recovery(self):
        self.assertEqual(self.scan([OSError('first'), OSError('second'), None]),
                         ['source-error-OSError', 'source-recovered'])

    def test_scanner_success_resets_streak(self):
        self.assertEqual(self.scan([OSError('first'), None, OSError('again'), None]), [])
