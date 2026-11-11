import threading
import unittest
from unittest.mock import Mock
from file_sync import Controller

class ScanControlTests(unittest.TestCase):
    def controller(self):
        c = Controller.__new__(Controller)
        c.scan_lock = threading.Lock()
        c.scan_requested = threading.Event()
        c.scan_running = False
        c.stop = threading.Event()
        c.c = {'scan': {'interval': 60}}
        c.publish = Mock()
        return c

    def test_request_wakes_wait_and_duplicates_are_rejected(self):
        c = self.controller()
        self.assertEqual(c.request_scan()[1], 202)
        self.assertEqual(c.request_scan()[1], 409)
        self.assertFalse(c.wait_scan())
        self.assertTrue(c.scan_requested.is_set())
        self.assertIn('next_started_at', c.publish.call_args.kwargs)

    def test_running_scan_rejects_new_request(self):
        c = self.controller()
        c.scan_running = True
        self.assertEqual(c.request_scan()[1], 409)
        self.assertFalse(c.scan_requested.is_set())

    def test_stop_interrupts_wait(self):
        c = self.controller()
        c.stop.set()
        self.assertTrue(c.wait_scan())
