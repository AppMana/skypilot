"""This module helps generating timelines of an application.

The timeline follows the trace event format defined here:
https://docs.google.com/document/d/1CvAClvFfyA5R-PhYUmn5OOQtYMH4h6I0nSsKchNAySU/preview
"""  # pylint: disable=line-too-long
import atexit
import functools
import json
import os
import threading
import time
import traceback
from typing import Callable, Optional, Union

import filelock

from sky.utils import common_utils
from sky.utils import locks

_events = []


class Event:
    """Record an event.

    Args:
        name: The name of the event.
        message: The message attached to the event.
    """

    def __init__(self, name: str, message: Optional[str] = None):
        self._name = name
        self._message = message
        # See the module doc for the event format.
        self._event = {
            'name': self._name,
            'cat': 'event',
            'pid': str(os.getpid()),
            'tid': str(threading.current_thread().ident),
            'args': {
                'message': self._message
            }
        }
        if self._message is not None:
            self._event['args'] = {'message': self._message}

    def begin(self):
        event_begin = self._event.copy()
        event_begin.update({
            'ph': 'B',
            'ts': f'{time.time() * 10 ** 6: .3f}',
        })
        event_begin['args'] = {'stack': '\n'.join(traceback.format_stack())}
        if self._message is not None:
            event_begin['args']['message'] = self._message
        _events.append(event_begin)

    def end(self):
        event_end = self._event.copy()
        event_end.update({
            'ph': 'E',
            'ts': f'{time.time() * 10 ** 6: .3f}',
        })
        if self._message is not None:
            event_end['args'] = {'message': self._message}
        _events.append(event_end)

    def __enter__(self):
        self.begin()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.end()


def event(name_or_fn: Union[str, Callable], message: Optional[str] = None):
    return common_utils.make_decorator(Event, name_or_fn, message=message)


class DistributedLockEvent:
    """Serve both as a distributed lock and event for the lock."""

    def __init__(self, lock_id: str, timeout: Optional[float] = None):
        self._lock_id = lock_id
        self._lock = locks.get_lock(lock_id, timeout)
        self._hold_lock_event = Event(f'[DistributedLock.hold]:{lock_id}')

    def acquire(self):
        was_locked = self._lock.is_locked
        with Event(f'[DistributedLock.acquire]:{self._lock_id}'):
            self._lock.acquire()
        if not was_locked and self._lock.is_locked:
            # start holding the lock after initial acquiring
            self._hold_lock_event.begin()

    def release(self):
        was_locked = self._lock.is_locked
        self._lock.release()
        if was_locked and not self._lock.is_locked:
            # stop holding the lock after initial releasing
            self._hold_lock_event.end()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()

    def __call__(self, f):

        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            with self:
                return f(*args, **kwargs)

        return wrapper


class FileLockEvent:
    """Serve both as a file lock and event for the lock."""

    def __init__(self, lockfile: Union[str, os.PathLike], timeout: float = -1):
        self._lockfile = lockfile
        os.makedirs(os.path.dirname(os.path.abspath(self._lockfile)),
                    exist_ok=True)
        self._lock = filelock.FileLock(self._lockfile, timeout)
        self._hold_lock_event = Event(f'[FileLock.hold]:{self._lockfile}')

    def acquire(self):
        was_locked = self._lock.is_locked
        with Event(f'[FileLock.acquire]:{self._lockfile}'):
            self._lock.acquire()
        if not was_locked and self._lock.is_locked:
            # start holding the lock after initial acquiring
            self._hold_lock_event.begin()

    def release(self):
        was_locked = self._lock.is_locked
        self._lock.release()
        if was_locked and not self._lock.is_locked:
            # stop holding the lock after initial releasing
            self._hold_lock_event.end()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()

    def __call__(self, f):
        # Make this class callable as a decorator.
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            with self:
                return f(*args, **kwargs)

        return wrapper


def save_timeline():
    file_path = os.environ.get('SKYPILOT_TIMELINE_FILE_PATH')
    if not file_path:
        return
    json_output = {
        'traceEvents': _events,
        'displayTimeUnit': 'ms',
        'otherData': {
            'log_dir': os.path.dirname(os.path.abspath(file_path)),
        }
    }
    os.makedirs(os.path.dirname(os.path.abspath(file_path)), exist_ok=True)
    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(json_output, f)


if os.environ.get('SKYPILOT_TIMELINE_FILE_PATH'):
    atexit.register(save_timeline)
