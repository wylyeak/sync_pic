import json
from pathlib import Path
import tempfile
import threading
import unittest
from telemetry import Telemetry

class TelemetryTests(unittest.TestCase):
    def test_main_and_scanner_updates_are_atomic(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root,'runtime.json')
            t=Telemetry(path)
            def update(section):
                for i in range(10):
                    t.update(section,count=i)
                    json.loads(path.read_text())
            threads=[threading.Thread(target=update,args=(s,)) for s in ('controller','scan')]
            for thread in threads: thread.start()
            for thread in threads: thread.join()
            result=json.loads(path.read_text())
            self.assertEqual(result['controller']['count'],9)
            self.assertEqual(result['scan']['count'],9)
    def test_events_are_bounded_and_previous_state_retained(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root,'runtime.json')
            t=Telemetry(path)
            t.update('photos',state='complete')
            for i in range(55): t.event('error',str(i),'batch')
            restored=Telemetry(path)
            self.assertEqual(restored.data['photos']['state'],'complete')
            self.assertEqual(len(restored.data['events']),50)
            self.assertEqual(restored.data['events'][0]['message'],'5')

if __name__=='__main__': unittest.main()
