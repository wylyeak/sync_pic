"""Bounded caches and exclusively borrowed read-only database connections."""
from collections import OrderedDict
from concurrent.futures import Future
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from queue import Empty, LifoQueue
import threading
import logging
import time

@dataclass(frozen=True)
class Cached:
    value: object
    created_at: float
    state: str

class TTLCache:
    def __init__(self, limit=256, clock=time.monotonic):
        self.limit, self.clock = limit, clock
        self.values = OrderedDict()
        self.pending = {}
        self.lock = threading.Lock()

    def get(self, key, ttl, loader, stale_for=0):
        with self.lock:
            entry = self.values.get(key)
            age = self.clock() - entry[1] if entry else None
            if entry and age < ttl:
                self.values.move_to_end(key)
                return Cached(entry[0], entry[1], 'hit')
            serve_stale = bool(entry and stale_for and age < ttl + stale_for)
            future = self.pending.get(key)
            owner = future is None
            if owner:
                future = self.pending[key] = Future()
        if serve_stale:
            if owner:
                threading.Thread(target=self._refresh, args=(key, loader, future), daemon=True).start()
            return Cached(entry[0], entry[1], 'refreshing')
        if not owner:
            value, stamp = future.result(timeout=25)
            return Cached(value, stamp, 'coalesced')
        value, stamp = self._load(key, loader, future)
        return Cached(value, stamp, 'miss')

    def _refresh(self, key, loader, future):
        try:
            self._load(key, loader, future)
        except Exception as exc:
            logging.warning('Dashboard background refresh failed: %s', type(exc).__name__)

    def _load(self, key, loader, future):
        try:
            value, stamp = loader(), self.clock()
            with self.lock:
                self.values[key] = (value, stamp)
                self.values.move_to_end(key)
                while len(self.values) > self.limit:
                    self.values.popitem(last=False)
            future.set_result((value, stamp))
            return value, stamp
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self.lock:
                self.pending.pop(key, None)

    def clear(self):
        with self.lock:
            self.values.clear()

class ReadPool:
    def __init__(self, factory, size=4):
        self.factory = factory
        self.available = LifoQueue(maxsize=size)
        self.slots = threading.BoundedSemaphore(size)

    @contextmanager
    def connection(self):
        if not self.slots.acquire(timeout=8):
            raise TimeoutError('Dashboard connection pool busy')
        conn = None
        healthy = False
        try:
            try:
                conn, returned_at = self.available.get_nowait()
                if time.monotonic() - returned_at > 30:
                    try:
                        conn.ping(reconnect=False)
                    except Exception:
                        with suppress(Exception):
                            conn.close()
                        conn = None
            except Empty:
                pass
            if conn is None:
                conn = self.factory()
            yield conn
            healthy = True
        finally:
            if conn is not None:
                if healthy:
                    self.available.put_nowait((conn, time.monotonic()))
                else:
                    try:
                        conn.close()
                    except Exception:
                        pass
            self.slots.release()
