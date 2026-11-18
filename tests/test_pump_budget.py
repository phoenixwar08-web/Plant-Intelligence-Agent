"""No hardware: durable rolling budget shared by every Phase3 pump caller."""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from services.soil3.phase3.model_proposal import PumpBudget


class PumpBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'budget.sqlite'
        self.now = 1800000000.
        self.budget = PumpBudget(self.path)

    def seed(self, entries=()):
        self.budget.bootstrap(self.now, self.now - 86400, entries,
                              evidence='verified-test-history')

    def test_missing_history_never_means_zero(self):
        with self.assertRaises(ValueError):
            self.budget.reserve('ai', 3, 30, self.now)
        self.assertFalse(self.path.exists())

    def test_all_sources_share_limit_across_restart(self):
        self.seed()
        for source, seconds in [('script', 10), ('manual', 10), ('ai', 10)]:
            key = self.budget.reserve(source, seconds, 30, self.now)
            self.budget.complete(key, self.now + seconds)
            self.now += 60
        with self.assertRaises(ValueError):
            PumpBudget(self.path).reserve('fallback', 1, 30, self.now)

    def test_unknown_command_blocks_new_actions_even_after_window(self):
        self.seed()
        self.budget.reserve('ai', 3, 30, self.now)
        with self.assertRaises(ValueError):
            PumpBudget(self.path).reserve('script', 3, 30, self.now + 90000)

    def test_window_uses_completion_not_start_and_has_exact_boundary(self):
        self.seed()
        key = self.budget.reserve('script', 30, 30, self.now)
        self.budget.complete(key, self.now + 30)
        with self.assertRaises(ValueError):
            self.budget.reserve('ai', 1, 30, self.now + 86401)
        self.budget.reserve('ai', 30, 30, self.now + 86430)

    def test_seed_requires_full_window_and_no_silent_rebootstrap(self):
        with self.assertRaises(ValueError):
            self.budget.bootstrap(self.now, self.now - 1, (), evidence='short')
        self.seed([{'source': 'legacy', 'seconds': 29, 'completed_at': self.now - 100}])
        with self.assertRaises(ValueError):
            self.seed()
        with self.assertRaises(ValueError):
            self.budget.reserve('ai', 2, 30, self.now)

    def test_corruption_and_clock_rollback_fail_closed(self):
        self.seed()
        with self.assertRaises(ValueError):
            self.budget.reserve('ai', 1, 30, self.now - 1)
        self.path.write_bytes(b'broken')
        with self.assertRaises((ValueError, sqlite3.DatabaseError)):
            self.budget.reserve('ai', 1, 30, self.now)

    def test_invalid_values_do_not_reserve(self):
        self.seed()
        for value in [True, -1, 0, float('nan'), float('inf')]:
            with self.assertRaises(ValueError):
                self.budget.reserve('ai', value, 30, self.now)

    def test_actual_actuator_blocks_before_on_when_shared_budget_exhausted(self):
        import math
        from types import SimpleNamespace
        from unittest.mock import Mock
        from test_ai_phase3_policy import Phase3IntegrationTests
        self.seed([{'source': 'manual', 'seconds': 30, 'completed_at': self.now - 100}])
        limits = {'WATER_SEC_MAX_HARD': 30, 'SCRIPT_AI_ENABLED': False,
                  'MODEL_PROPOSAL_ENABLED': False, 'MODEL_PROPOSAL_BUDGET_24H_SEC': 30}
        actuator = SimpleNamespace(cfg=SimpleNamespace(get_constant=limits.get),
                                   _publish_pump=Mock(), _force_pump_off=Mock())
        method = Phase3IntegrationTests().method('ActuatorLayer', '_activate_pump', dict(
            math=math, PumpExecutionError=ValueError, logger=Mock(), Path=Path,
            FILE_PATHS={'SYSTEM_STATE': self.path.parent/'system_state.json'},
            time=SimpleNamespace(time=lambda: self.now)))
        # Actuator's canonical path is shared for every source.
        self.path.rename(self.path.parent/'watering_budget.sqlite')
        with self.assertRaises(ValueError):
            method(actuator, 3)
        actuator._publish_pump.assert_not_called()
        actuator._force_pump_off.assert_not_called()

    def test_missing_ledger_blocks_actual_actuator_before_on(self):
        import math
        from types import SimpleNamespace
        from unittest.mock import Mock
        from test_ai_phase3_policy import Phase3IntegrationTests
        actuator = SimpleNamespace(cfg=SimpleNamespace(get_constant=lambda name: 30),
                                   _publish_pump=Mock(), _force_pump_off=Mock())
        method = Phase3IntegrationTests().method('ActuatorLayer', '_activate_pump', dict(
            math=math, PumpExecutionError=ValueError, logger=Mock(), Path=Path,
            FILE_PATHS={'SYSTEM_STATE': self.path.parent/'system_state.json'},
            time=SimpleNamespace(time=lambda: self.now)))
        with self.assertRaises(ValueError):
            method(actuator, 3)
        actuator._publish_pump.assert_not_called()
        actuator._force_pump_off.assert_not_called()

    def test_failed_off_leaves_reservation_and_blocks_next_on(self):
        import math
        from types import SimpleNamespace
        from unittest.mock import Mock
        from test_ai_phase3_policy import Phase3IntegrationTests
        self.seed()
        self.path.rename(self.path.parent/'watering_budget.sqlite')
        actuator = SimpleNamespace(cfg=SimpleNamespace(get_constant=lambda name: 30),
            _publish_pump=Mock(), _force_pump_off=Mock(side_effect=RuntimeError('OFF failed')))
        method = Phase3IntegrationTests().method('ActuatorLayer', '_activate_pump', dict(
            math=math, PumpExecutionError=ValueError, logger=Mock(), Path=Path,
            FILE_PATHS={'SYSTEM_STATE': self.path.parent/'system_state.json'},
            time=SimpleNamespace(time=lambda: self.now, sleep=Mock()),
            _utc_timestamp=lambda: 'test', _update_system_state=Mock()))
        with self.assertRaises(ValueError):
            method(actuator, 5)
        actuator._publish_pump.assert_called_once_with('on')
        self.assertEqual(2, actuator._force_pump_off.call_count)
        actuator._force_pump_off.side_effect = None
        with self.assertRaises(ValueError):
            method(actuator, 5)
        actuator._publish_pump.assert_called_once()

    def test_off_publish_timeout_is_not_completed_budget(self):
        import math
        from types import SimpleNamespace, MethodType
        from unittest.mock import Mock
        from test_ai_phase3_policy import Phase3IntegrationTests
        self.seed()
        self.path.rename(self.path.parent/'watering_budget.sqlite')
        limits = {'WATER_SEC_MAX_HARD': 30, 'MODEL_PROPOSAL_BUDGET_24H_SEC': 30,
                  'PUMP_OFF_RETRY_COUNT': 1, 'PUMP_OFF_RETRY_INTERVAL_SEC': 0.1}
        def mqtt_reply(_topic, payload, **kwargs):
            return SimpleNamespace(rc=0, wait_for_publish=Mock(),
                                   is_published=lambda: payload == 'on')
        actuator = SimpleNamespace(cfg=SimpleNamespace(get_constant=limits.get),
            _mqtt_client=SimpleNamespace(publish=Mock(side_effect=mqtt_reply)),
            _mqtt_topic_pump='offline-test')
        namespace = dict(math=math, PumpExecutionError=ValueError, logger=Mock(), Path=Path,
            FILE_PATHS={'SYSTEM_STATE': self.path.parent/'system_state.json'},
            time=SimpleNamespace(time=lambda: self.now, sleep=Mock()),
            _utc_timestamp=lambda: 'test', _update_system_state=Mock())
        helper = Phase3IntegrationTests()
        for name in ['_publish_pump', '_force_pump_off']:
            setattr(actuator, name, MethodType(helper.method('ActuatorLayer', name, namespace), actuator))
        method = helper.method('ActuatorLayer', '_activate_pump', namespace)
        with self.assertRaises(ValueError):
            method(actuator, 5)
        with closing(sqlite3.connect(self.path.parent/'watering_budget.sqlite')) as db:
            self.assertEqual('reserved', db.execute('SELECT status FROM commands').fetchone()[0])
        calls = actuator._mqtt_client.publish.call_count
        with self.assertRaises(ValueError):
            method(actuator, 5)
        self.assertEqual(calls, actuator._mqtt_client.publish.call_count)

    def test_concurrent_reservations_have_only_one_winner(self):
        from concurrent.futures import ThreadPoolExecutor
        self.seed()
        def attempt(index):
            try:
                PumpBudget(self.path).reserve(str(index), 20, 30, self.now)
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(1, sum(pool.map(attempt, range(8))))

    def test_zero_observation_needs_no_budget_history(self):
        import math
        from types import SimpleNamespace
        from unittest.mock import Mock
        from test_ai_phase3_policy import Phase3IntegrationTests
        method = Phase3IntegrationTests().method('ActuatorLayer', '_activate_pump', dict(
            math=math, PumpExecutionError=ValueError, logger=Mock()))
        actuator = SimpleNamespace(cfg=SimpleNamespace(get_constant=lambda name: 30),
                                   _publish_pump=Mock())
        self.assertIsNone(method(actuator, 0))
        actuator._publish_pump.assert_not_called()

    def test_actuator_success_and_uncertain_failure_are_durable(self):
        import math
        from types import SimpleNamespace
        from unittest.mock import Mock
        from test_ai_phase3_policy import Phase3IntegrationTests
        self.seed()
        self.path.rename(self.path.parent/'watering_budget.sqlite')
        limits = {'WATER_SEC_MAX_HARD': 30, 'SCRIPT_AI_ENABLED': True,
                  'MODEL_PROPOSAL_BUDGET_24H_SEC': 30}
        actuator = SimpleNamespace(cfg=SimpleNamespace(get_constant=limits.get),
                                   _publish_pump=Mock(), _force_pump_off=Mock())
        method = Phase3IntegrationTests().method('ActuatorLayer', '_activate_pump', dict(
            math=math, PumpExecutionError=ValueError, logger=Mock(), Path=Path,
            FILE_PATHS={'SYSTEM_STATE': self.path.parent/'system_state.json'},
            time=SimpleNamespace(time=lambda: self.now, sleep=Mock()),
            _utc_timestamp=lambda: 'test', _update_system_state=Mock()))
        method(actuator, 20)
        actuator._publish_pump.assert_called_once_with('on')
        actuator._force_pump_off.assert_called_once()
        actuator._publish_pump.side_effect = RuntimeError('uncertain ON')
        with self.assertRaises(ValueError):
            method(actuator, 5)
        count = actuator._publish_pump.call_count
        actuator._publish_pump.side_effect = None
        with self.assertRaises(ValueError):
            method(actuator, 1)
        self.assertEqual(count, actuator._publish_pump.call_count)
        with closing(sqlite3.connect(self.path.parent/'watering_budget.sqlite')) as db:
            self.assertEqual([('completed', 20.), ('reserved', 5.)],
                db.execute('SELECT status,seconds FROM commands ORDER BY rowid').fetchall())


if __name__ == '__main__':
    unittest.main()
