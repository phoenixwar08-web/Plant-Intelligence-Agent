"""Non-actuating proposal verification and durable Phase3-owned reservations.

An upstream publication is a proposal, never authority. Only an explicitly
configured DecisionBrain may consume it after rechecking live safety facts.
No Trace v1 record is an input to this module; Bridge v1 stays verification-only.
"""
from __future__ import annotations

import copy
import json
import math
import os
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from services.soil3.cloud_strategy.validator import fingerprint, parse_timestamp
from services.soil3.phase3_bridge import Phase3Bridge


class PumpBudget:
    """Phase3-wide admission ledger; no MQTT, sensor or automatic history import.

    Reserve durably before ON. Uncertain commands block further admission until
    separately reconciled; a restart or the passage of 24h never clears them.
    Completed durations expire 24h after OFF, conservatively counting the entire
    command when its interval intersects the rolling window. These are command
    seconds, not a measurement of water flow.
    """
    def __init__(self, path):
        self.path = Path(path)

    @staticmethod
    def _number(value, *, positive=False):
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0 or (positive and value == 0)):
            raise ValueError('invalid budget number')
        return float(value)

    def bootstrap(self, now, coverage_start, entries, *, evidence):
        """Offline migration only: caller must attest COMPLETE all-source history.

        Never called by daemon startup. No reset/overwrite path is provided.
        Empty entries require evidence that there was no watering in that window.
        """
        now = self._number(now, positive=True)
        coverage_start = self._number(coverage_start)
        if coverage_start > now - 86400 or not isinstance(evidence, str) or not evidence.strip():
            raise ValueError('verified full-window coverage required')
        rows = []
        for item in entries:
            seconds = self._number(item['seconds'], positive=True)
            ended = self._number(item['completed_at'], positive=True)
            if not coverage_start <= ended <= now or not isinstance(item['source'], str) or not item['source']:
                raise ValueError('invalid initial watering history')
            rows.append((str(uuid.uuid4()), item['source'], seconds, ended, ended, 'completed'))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive filesystem creation prevents replacing an existing ledger.
        try:
            with self.path.open('xb'):
                pass
        except FileExistsError as error:
            raise ValueError('watering_budget_already_initialized') from error
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('CREATE TABLE metadata (singleton INTEGER PRIMARY KEY CHECK(singleton=1), last_seen REAL NOT NULL, evidence TEXT NOT NULL)')
            db.execute('CREATE TABLE commands (id TEXT PRIMARY KEY, source TEXT NOT NULL, seconds REAL NOT NULL, reserved_at REAL NOT NULL, completed_at REAL, status TEXT NOT NULL)')
            db.execute('INSERT INTO metadata VALUES (1, ?, ?)', (now, evidence))
            db.executemany('INSERT INTO commands VALUES (?,?,?,?,?,?)', rows)
            db.commit()

    def _connect(self):
        if not self.path.is_file():
            raise ValueError('watering_budget_history_uninitialized')
        db = sqlite3.connect(self.path.resolve().as_uri() + '?mode=rw', uri=True, timeout=10)
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
        except Exception:
            db.close()
            raise
        return db

    @staticmethod
    def _clock(db, now):
        row = db.execute('SELECT last_seen FROM metadata WHERE singleton=1').fetchone()
        if row is None or not math.isfinite(row[0]) or now < row[0]:
            raise ValueError('watering_budget_clock_or_metadata_invalid')
        db.execute('UPDATE metadata SET last_seen=? WHERE singleton=1', (now,))

    def reserve(self, source, seconds, limit, now):
        seconds = self._number(seconds, positive=True)
        limit = self._number(limit, positive=True)
        now = self._number(now, positive=True)
        if not isinstance(source, str) or not source:
            raise ValueError('watering_budget_source_missing')
        db = self._connect()
        try:
            self._clock(db, now)
            used = 0.
            for value, ended, status in db.execute('SELECT seconds, completed_at, status FROM commands'):
                value = self._number(value, positive=True)
                if status != 'completed' or ended is None:
                    raise ValueError('watering_budget_unresolved_command')
                ended = self._number(ended, positive=True)
                if ended > now:
                    raise ValueError('watering_budget_future_history')
                if ended > now - 86400:
                    used += value
            if used + seconds > limit:
                raise ValueError('watering_budget_24h_exceeded')
            key = str(uuid.uuid4())
            db.execute('INSERT INTO commands VALUES (?,?,?,?,NULL,?)', (key, source, seconds, now, 'reserved'))
            db.commit()
            return key
        finally:
            db.close()

    def complete(self, key, now):
        now = self._number(now, positive=True)
        db = self._connect()
        try:
            self._clock(db, now)
            row = db.execute('SELECT reserved_at,status FROM commands WHERE id=?', (key,)).fetchone()
            if row is None or row[1] != 'reserved' or now < row[0]:
                raise ValueError('watering_budget_invalid_completion')
            db.execute('UPDATE commands SET status=?, completed_at=? WHERE id=?', ('completed', now, key))
            db.commit()
        finally:
            db.close()


@dataclass(frozen=True)
class ProposalPolicy:
    min_seconds: float
    max_seconds: float
    max_seconds_24h: float
    max_age_seconds: float
    max_humidity_drift: float
    min_interval_seconds: float

    def __post_init__(self):
        values = (self.min_seconds, self.max_seconds, self.max_seconds_24h,
                  self.max_age_seconds, self.max_humidity_drift, self.min_interval_seconds)
        if any(isinstance(x, bool) or not isinstance(x, (int, float))
               or not math.isfinite(x) or x <= 0 for x in values):
            raise ValueError('proposal policy limits must be finite and positive')
        if self.min_seconds > self.max_seconds:
            raise ValueError('proposal policy bounds are reversed')


@dataclass(frozen=True)
class ProposalCheck:
    accepted: bool
    seconds: float
    strategy_id: str
    strategy_sha256: str
    reasons: tuple[str, ...]


def make_proposal(state, strategy, gate, runner):
    """Copy completed upstream facts without granting execution authority."""
    return copy.deepcopy(dict(schema_version='phase3_model_proposal.v1',
        state=state, strategy=strategy, gate=gate, runner=runner))


def check_proposal(bundle, policy, current, now: datetime, history):
    reasons = []
    strategy_id = ''
    strategy_hash = ''
    seconds = 0.0
    try:
        if set(bundle) != {'schema_version','state','strategy','gate','runner'}:
            raise ValueError('proposal shape')
        if bundle['schema_version'] != 'phase3_model_proposal.v1':
            raise ValueError('proposal version')
        state, strategy = bundle['state'], bundle['strategy']
        checked = Phase3Bridge().verify(state, strategy, bundle['gate'], bundle['runner'])
        if checked.get('accepted') is not True:
            reasons.append('upstream_binding_rejected')
        strategy_id = strategy['strategy_id']
        strategy_hash = fingerprint(strategy)
        observed = parse_timestamp(state['observed_at'])
        generated = parse_timestamp(strategy['created_at'])
        for timestamp in (observed, generated):
            if timestamp is None or not 0 <= (now-timestamp).total_seconds() <= policy.max_age_seconds:
                reasons.append('proposal_stale_or_future')
        actions = strategy['actions']
        waters = [a for a in actions if a['type']=='water']
        if any(a['type']=='wait' for a in actions) or len(waters)>1 or (
            waters and any(a['type']=='stop' for a in actions)):
            reasons.append('unsupported_action_sequence')
        # Only optional observe actions may surround one immediate water action.
        seconds = float(waters[0]['pump_seconds']) if len(waters)==1 else 0.
        if not math.isfinite(seconds) or seconds < 0 or (seconds and not policy.min_seconds <= seconds <= policy.max_seconds):
            reasons.append('duration_out_of_bounds')
        if seconds:
            last_water = current.get('last_water_at')
            if last_water is not None:
                if isinstance(last_water,bool) or not isinstance(last_water,(int,float)) or not math.isfinite(last_water):
                    reasons.append('invalid_last_watering_time')
                elif not policy.min_interval_seconds <= now.timestamp()-last_water:
                    reasons.append('recent_actual_watering')
            for name in ('sensor_stale','pump_active','pending_soak','sensor_fault',
                         'water_delivery_suspect','reservoir_empty_suspect','low_wet_recovery_suspect',
                         'predictor_circuit_open','cloud_protection','recent_response_guard'):
                if current.get(name) is not False:
                    reasons.append('current_safety:'+name)
            humidity = current.get('humidity'); fc = current.get('field_capacity')
            if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v)
                   for v in (humidity,fc)) or not 0 < humidity < fc <= 100:
                reasons.append('wet_or_invalid_current_state')
            else:
                if abs(humidity-float(state['soil']['humidity_percent'])) > policy.max_humidity_drift:
                    reasons.append('state_changed')
            recent=[]
            for item in history:
                timestamp=float(item['claimed_at']); value=float(item['seconds'])
                if not math.isfinite(timestamp) or not math.isfinite(value) or value<0 or timestamp>now.timestamp():
                    raise ValueError('invalid reservation history')
                if now.timestamp()-timestamp < 86400:recent.append(item)
            if any(item['strategy_id']==strategy_id for item in history):reasons.append('already_consumed')
            if sum(float(item['seconds']) for item in recent)+seconds > policy.max_seconds_24h:
                reasons.append('daily_budget')
            if any(float(item['seconds']) > 0 and now.timestamp()-float(item['claimed_at']) < policy.min_interval_seconds for item in recent):
                reasons.append('minimum_interval')
    except (KeyError, TypeError, ValueError, OverflowError):
        reasons.append('invalid_proposal_or_safety_facts')
    return ProposalCheck(not reasons, seconds, strategy_id, strategy_hash, tuple(sorted(set(reasons))))


class ProposalClaims:
    """Caller holds Phase3's process lock across history/check/claim/actuation.

    Exclusive create also prevents the same Strategy being replayed on restart.
    Failed/unknown actions remain reserved; a crash never causes a retry.
    """
    def __init__(self, directory):self.directory=Path(directory)

    def history(self):
        if not self.directory.exists():return []
        return [json.loads(path.read_text(encoding='utf-8')) for path in self.directory.glob('*.json')]

    def claim(self, result: ProposalCheck, now: datetime):
        import uuid
        if not result.accepted:raise ValueError('cannot claim rejected proposal')
        if str(uuid.UUID(result.strategy_id)) != result.strategy_id:raise ValueError('invalid strategy id')
        self.directory.mkdir(parents=True, exist_ok=True)
        path=self.directory/(result.strategy_id+'.json')
        with path.open('x',encoding='utf-8') as output:
            json.dump(dict(strategy_id=result.strategy_id, strategy_sha256=result.strategy_sha256,
                claimed_at=now.timestamp(),seconds=result.seconds,status='reserved'),output)
            output.flush();os.fsync(output.fileno())
        return path
