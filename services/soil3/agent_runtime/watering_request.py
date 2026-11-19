"""Durable, non-actuating arbitration of one soil3 decision request.

The worker may reply/fail; only Phase3 may claim and execute. A claim is never
automatically retried after a crash. No sensor or actuator is imported here.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

_THREAD_LOCK = threading.RLock()


def _boot_id():
    path = Path('/proc/sys/kernel/random/boot_id')
    if path.exists():
        return path.read_text().strip()
    # Windows: monotonic clocks share the system boot epoch. A conservative
    # clock-origin difference after restart cancels, rather than extends a wait.
    return 'clock-origin'


@contextmanager
def _locked(root):
    root.mkdir(parents=True, exist_ok=True)
    with _THREAD_LOCK, (root / 'requests.lock').open('a+b') as handle:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b'0'); handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == 'nt':
                handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _write(path, value):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as out:
            json.dump(value, out, ensure_ascii=False, allow_nan=False)
            out.flush(); os.fsync(out.fileno())
        os.replace(temporary, path)
        if os.name != 'nt':
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


class RequestStore:
    def __init__(self, root, *, wall=time.time, monotonic=time.monotonic, boot_id=None):
        self.root = Path(root)
        self.wall, self.monotonic = wall, monotonic
        self.boot_id = _boot_id() if boot_id is None else boot_id

    def _path(self, decision_id):
        return self.root / (str(uuid.UUID(decision_id)) + '.json')

    def _load(self):
        pointer = self.root / 'active.json'
        if not pointer.exists():
            return None
        decision_id = json.loads(pointer.read_text(encoding='utf-8'))['decision_id']
        return json.loads(self._path(decision_id).read_text(encoding='utf-8'))

    def _save(self, item):
        _write(self._path(item['decision_id']), item)
        return copy.deepcopy(item)

    def _tick(self, item):
        if item and item['status'] in {'waiting', 'ai_ready'}:
            old_status = item['status']
            wall_elapsed = self.wall() - item['created_at']
            mono_elapsed = self.monotonic() - item['created_monotonic']
            if (item['boot_id'] != self.boot_id or wall_elapsed < 0 or mono_elapsed < 0
                    or abs(wall_elapsed - mono_elapsed) > 5):
                item.update(status='cancelled', reason='clock_or_boot_changed')
            elif max(wall_elapsed, mono_elapsed) >= 300:
                item.update(status='fallback_ready', reason='deadline_exceeded')
            if item['status'] != old_status:
                self._save(item)
        return item

    def poll(self):
        with _locked(self.root):
            return copy.deepcopy(self._tick(self._load()))

    def create(self, model_input, *, model_key, process_id):
        # Validate before touching disk. Canonical hash fixes the actual input.
        encoded = json.dumps(model_input, sort_keys=True, ensure_ascii=False,
                             separators=(',', ':'), allow_nan=False).encode('utf-8')
        with _locked(self.root):
            current = self._tick(self._load())
            if current and current['status'] not in {'finished', 'cancelled'}:
                return copy.deepcopy(current)
            item = dict(schema_version='watering_request.v1', device_code='soil3',
                decision_id=str(uuid.uuid4()), care_process_id=process_id,
                model_key=model_key, input=copy.deepcopy(model_input),
                input_sha256=hashlib.sha256(encoded).hexdigest(),
                created_at=self.wall(), created_monotonic=self.monotonic(),
                boot_id=self.boot_id, deadline_at=self.wall() + 300,
                status='waiting', owner=None, reason=None, result=None,
                worker_started=False, events=[])
            self._save(item)
            _write(self.root / 'active.json', {'decision_id': item['decision_id']})
            return copy.deepcopy(item)

    def start_worker(self, decision_id):
        with _locked(self.root):
            item = self._tick(self._load())
            if (not item or item['decision_id'] != decision_id or
                    item['status'] != 'waiting' or item['worker_started']):
                return False
            item['worker_started'] = True
            self._save(item)
            return True

    def attach_context(self, decision_id, context):
        """Set-once model input, fixed before the provider is invoked."""
        with _locked(self.root):
            item = self._tick(self._load())
            if (not item or item['decision_id'] != decision_id or
                    item['status'] != 'waiting' or 'model_input' in item):
                return False
            encoded = json.dumps(context, sort_keys=True, separators=(',', ':'),
                                 ensure_ascii=False, allow_nan=False).encode('utf-8')
            item['model_input'] = copy.deepcopy(context)
            item['model_input_sha256'] = hashlib.sha256(encoded).hexdigest()
            self._save(item)
            return True

    def due_for_review(self):
        item = self.poll()
        if (not item or item['status'] != 'finished' or item.get('owner') != 'ai'
                or item.get('outcome', {}).get('model_proposal_accepted') is not True):
            return False
        after = (item.get('result') or {}).get('output', {}).get('next_check_after_seconds')
        return isinstance(after, (int, float)) and self.wall() >= item['finished_at'] + after

    def review_delay(self):
        item = self.poll()
        if (not item or item['status'] != 'finished' or item.get('owner') != 'ai'
                or item.get('outcome', {}).get('model_proposal_accepted') is not True):
            return None
        after = (item.get('result') or {}).get('output', {}).get('next_check_after_seconds')
        if not isinstance(after, (int, float)):
            return None
        return max(0, item['finished_at'] + after - self.wall())

    def reply(self, decision_id, input_hash, result):
        with _locked(self.root):
            item = self._tick(self._load())
            if not item or item['decision_id'] != decision_id:
                return False
            if item['status'] != 'waiting' or input_hash != item['input_sha256']:
                item['events'].append({'type': 'reply_ignored', 'at': self.wall()})
                self._save(item)
                return False
            item.update(status='ai_ready', result=copy.deepcopy(result))
            self._save(item)
            return True

    def fail(self, decision_id, reason):
        with _locked(self.root):
            item = self._tick(self._load())
            if item and item['decision_id'] == decision_id and item['status'] == 'waiting':
                item.update(status='fallback_ready', reason=reason)
                self._save(item)

    def claim(self, decision_id, owner):
        if owner not in {'ai', 'script'}:
            raise ValueError('invalid execution owner')
        with _locked(self.root):
            item = self._tick(self._load())
            required = 'ai_ready' if owner == 'ai' else 'fallback_ready'
            if not item or item['decision_id'] != decision_id or item['status'] != required:
                return False
            item.update(status='claimed', owner=owner, claimed_at=self.wall())
            self._save(item)
            return True

    def finish(self, decision_id, outcome):
        with _locked(self.root):
            item = self._load()
            if not item or item['decision_id'] != decision_id or item['status'] != 'claimed':
                raise ValueError('request not claimed')
            item.update(status='finished', outcome=copy.deepcopy(outcome), finished_at=self.wall())
            return self._save(item)

    def cancel(self, reason, decision_id=None):
        with _locked(self.root):
            item = self._load()
            if (item and (decision_id is None or item['decision_id'] == decision_id)
                    and item['status'] in {'waiting', 'ai_ready', 'fallback_ready'}):
                item.update(status='cancelled', reason=reason)
                self._save(item)
