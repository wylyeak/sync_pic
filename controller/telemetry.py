"""Non-blocking-to-business status export for the read-only dashboard."""
import json
import logging
from pathlib import Path
import threading
import time

class Telemetry:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        try:
            self.data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = {}

    def update(self, section, **values):
        with self.lock:
            self.data.setdefault(section, {}).update(values, checked_at=time.time())
            self._save()

    def event(self, kind, message, batch_id=None):
        with self.lock:
            events = self.data.setdefault('events', [])
            events.append(dict(time=time.time(), kind=kind, message=message, batch_id=batch_id))
            self.data['events'] = events[-50:]
            self._save()

    def _save(self):
        self.data['updated_at'] = time.time()
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.data, ensure_ascii=False) + '\n')
        temporary.replace(self.path)
