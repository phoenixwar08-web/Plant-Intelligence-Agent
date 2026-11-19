"""Hardware-free tests of semantic admission retirement and Phase3 intake."""
import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from services.soil3.cloud_trigger.evaluator_v1 import evaluate_trigger
from test_cloud_trigger_shadow import complete_state
from services.soil3.cloud_trigger.context_v1 import build_decision_context, semantic_projection


class AdmissionModeTests(unittest.TestCase):
    def context(self):
        state=complete_state();state['safety']['flags']['pending_soak']=None
        return build_decision_context(state, phase3_system_state={}, deny_age_seconds=18000)

    def test_disabled_semantic_filter_calls_on_unchanged_context(self):
        context = self.context()
        result = evaluate_trigger(context, semantic_projection(context), deny_age_seconds=18000,
                                  semantic_filter_enabled=False)
        self.assertEqual('CALL_QWEN', result.decision)
        self.assertIn('semantic_filter_disabled', result.reason_codes)

    def test_disabled_filter_does_not_disable_stale_or_danger_checks(self):
        for mutate in [lambda c: c['data_quality'].update(soil_age_sec=18001),
                       lambda c: c['safety_flags'].update(sensor_fault='active')]:
            context = self.context(); mutate(context)
            self.assertEqual('LOCAL_SAFETY_ONLY', evaluate_trigger(
                context, None, deny_age_seconds=18000, semantic_filter_enabled=False).decision)

    def test_disabled_filter_allows_analysis_during_normal_cooldown(self):
        context = self.context()
        context['safety_flags']['dynamic_cooldown'] = 'active'
        self.assertEqual('CALL_QWEN', evaluate_trigger(
            context, None, deny_age_seconds=18000, semantic_filter_enabled=False).decision)


class ProposalPolicyTests(unittest.TestCase):
    def setUp(self):
        from services.soil3.phase3 import model_proposal as proposal
        from test_controlled_phase3_execution import state_snapshot, strategy_for
        from services.soil3.cloud_gate.gate_v2 import GatePolicy, evaluate_gate_v2
        from services.soil3.runner.runner_v1 import RunnerStore, DryRunRunner
        self.module = proposal
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = datetime(2026, 9, 21, 8, 0, 10, tzinfo=timezone.utc)
        self.state = state_snapshot(); self.state['soil']['humidity_percent'] = 34.
        self.strategy = strategy_for(self.state)
        self.gate = evaluate_gate_v2(self.state, self.strategy,
            GatePolicy(warning_age_seconds=900, deny_age_seconds=18000,
                       window_seconds=86400, max_exploration_water_seconds=0), False)
        self.runner = DryRunRunner(RunnerStore(self.root/'runner')).run(self.strategy, self.state)
        self.bundle = proposal.make_proposal(self.state, self.strategy, self.gate, self.runner)
        self.policy = proposal.ProposalPolicy(min_seconds=3, max_seconds=30,
            max_seconds_24h=12, max_age_seconds=300, max_humidity_drift=1,
            min_interval_seconds=3600)
        self.current = dict(humidity=34., field_capacity=45., sensor_stale=False,
            pump_active=False, pending_soak=False, sensor_fault=False,
            water_delivery_suspect=False, reservoir_empty_suspect=False,
            low_wet_recovery_suspect=False, predictor_circuit_open=False,
            cloud_protection=False, recent_response_guard=False)

    def check(self, bundle=None, current=None, history=None):
        return self.module.check_proposal(bundle or self.bundle, self.policy,
            self.current if current is None else current, self.now, history or [])

    def test_valid_model_duration_is_not_replaced_by_native_candidate(self):
        result = self.check()
        self.assertTrue(result.accepted, result.reasons)
        self.assertEqual(5, result.seconds)

    def test_bound_chain_rejects_tampered_seconds(self):
        bundle = copy.deepcopy(self.bundle); bundle['strategy']['actions'][0]['pump_seconds'] = 7
        self.assertFalse(self.check(bundle).accepted)

    def test_freshness_and_live_reading_drift_are_checked(self):
        self.now = datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc)
        self.assertFalse(self.check().accepted)
        self.now = datetime(2026, 9, 21, 8, 0, 10, tzinfo=timezone.utc)
        self.current['humidity'] = 36
        self.assertIn('state_changed', self.check().reasons)

    def test_faults_soak_active_pump_and_wet_state_block_before_execution(self):
        for key in ['sensor_stale','sensor_fault','pump_active','pending_soak',
                    'water_delivery_suspect','reservoir_empty_suspect','low_wet_recovery_suspect',
                    'predictor_circuit_open','cloud_protection','recent_response_guard']:
            current = dict(self.current); current[key] = True
            self.assertFalse(self.check(current=current).accepted, key)
        current = dict(self.current, humidity=45.)
        self.assertFalse(self.check(current=current).accepted)

    def test_budget_includes_failed_claims_and_minimum_spacing(self):
        old = {'claimed_at': self.now.timestamp()-7200, 'seconds': 8, 'strategy_id': 'old'}
        self.assertIn('daily_budget', self.check(history=[old]).reasons)
        old['seconds']=3; old['claimed_at']=self.now.timestamp()-10
        self.assertIn('minimum_interval', self.check(history=[old]).reasons)

    def test_native_or_emergency_watering_is_also_checked(self):
        current=dict(self.current,last_water_at=self.now.timestamp()-30)
        self.assertIn('recent_actual_watering', self.check(current=current).reasons)

    def test_claim_is_durable_and_at_most_once(self):
        store=self.module.ProposalClaims(self.root/'claims')
        store.claim(self.check(), self.now)
        with self.assertRaises(FileExistsError):store.claim(self.check(), self.now)
        self.assertEqual(1,len(store.history()))

    def test_unsupported_multi_water_and_wait_are_not_silently_flattened(self):
        from services.soil3.cloud_strategy.validator import fingerprint
        bundle=copy.deepcopy(self.bundle)
        bundle['strategy']['actions'].insert(1,{'action_id':'a3','type':'wait','seconds':60})
        self.assertFalse(self.check(bundle).accepted)

    def test_unknown_current_safety_is_not_treated_as_clear(self):
        current=dict(self.current);current.pop('sensor_fault')
        self.assertFalse(self.check(current=current).accepted)

    def test_claimed_observation_does_not_delay_later_water(self):
        history=[{'strategy_id':'old-observation','claimed_at':self.now.timestamp()-1,'seconds':0}]
        self.assertTrue(self.check(history=history).accepted)

    def test_runtime_switch_is_explicit_and_backward_compatible(self):
        from services.soil3.agent_runtime.runtime_v1 import RuntimeConfig
        value=json.loads((Path(__file__).resolve().parents[1]/'config/soil3_agent_runtime.example.json').read_text())
        value.pop('trigger_enabled',None)
        self.assertTrue(RuntimeConfig.from_dict(value).trigger_enabled)
        value['trigger_enabled']=False
        self.assertFalse(RuntimeConfig.from_dict(value).trigger_enabled)
        value['trigger_enabled']='false'
        with self.assertRaises(ValueError):RuntimeConfig.from_dict(value)

    def model_cycle(self, *, crash=False, fault=False):
        from contextlib import nullcontext
        from types import SimpleNamespace
        from unittest.mock import Mock
        import logging
        source=self.root/'proposal.json';source.write_text(json.dumps(self.bundle),encoding='utf-8')
        limits={'MODEL_PROPOSAL_PATH':str(source),'WATER_SEC_MIN':3,'WATER_SEC_MAX_HARD':30,
                'MODEL_PROPOSAL_BUDGET_24H_SEC':12,'MODEL_PROPOSAL_MAX_AGE_SEC':300,
                'MODEL_PROPOSAL_MAX_HUMIDITY_DRIFT':1,'SOAK_WAIT_SEC':3600}
        reading=SimpleNamespace(humidity=34.,sensor_stale_hard=False)
        state={k:v for k,v in self.current.items() if k.endswith('suspect') or k in ['sensor_fault','pump_active']}
        state['sensor_fault']={'active':fault}
        state.update(predictor_circuit={'state':'CLOSED'},cloud_protection=False,recent_response_guard=False)
        namespace=dict(Path=Path, FILE_PATHS={'SYSTEM_STATE':self.root/'system_state.json'},
            file_lock=lambda *a,**k:nullcontext(),json=json,
            datetime=SimpleNamespace(now=lambda tz:self.now),timezone=timezone,
            _load_system_state=lambda:state,logger=logging.getLogger('test'),
            ActionPlan=lambda label,seconds:SimpleNamespace(label=label,water_sec=seconds),
            save_json_locked=lambda path,value:path.write_text(json.dumps(value),encoding='utf-8'))
        method=Phase3IntegrationTests().method('DecisionBrain','_run_model_cycle',namespace)
        brain=SimpleNamespace(cfg=SimpleNamespace(FC=45.,get_constant=limits.get),_pending_soak=None,
            sensor=SimpleNamespace(read=Mock(return_value=reading)),actuator=SimpleNamespace(execute_pump=Mock()),
            _finish_result=lambda **kwargs:SimpleNamespace(**kwargs))
        if crash:brain.actuator.execute_pump.side_effect=RuntimeError('unknown execution outcome')
        return method,brain,reading

    def test_successful_model_cycle_and_restart_replay(self):
        method,brain,reading=self.model_cycle()
        result=method(brain,reading,'ordinary')
        self.assertEqual(5.,result.action_sec)
        self.assertEqual(5.,brain.actuator.execute_pump.call_args.args[0])
        brain._pending_soak=None
        result=method(brain,reading,'ordinary')
        self.assertIn('already_consumed',result.notes)
        brain.actuator.execute_pump.assert_called_once()

    def test_execution_failure_consumes_proposal_and_budget(self):
        method,brain,reading=self.model_cycle(crash=True)
        with self.assertRaises(RuntimeError):method(brain,reading,'ordinary')
        history=self.module.ProposalClaims(self.root/'model_proposal_claims').history()
        self.assertEqual('reserved',history[0]['status'])
        self.assertEqual(5.,history[0]['seconds'])
        brain.actuator.execute_pump.side_effect=None
        result=method(brain,reading,'ordinary')
        self.assertIn('already_consumed',result.notes)
        brain.actuator.execute_pump.assert_called_once()

    def test_fault_activated_after_publication_blocks_consumption(self):
        method,brain,reading=self.model_cycle(fault=True)
        result=method(brain,reading,'ordinary')
        self.assertIn('sensor_fault',result.notes)
        brain.actuator.execute_pump.assert_not_called()

    def test_controlled_v1_cannot_execute_an_unrelated_latest_model_proposal(self):
        method,brain,reading=self.model_cycle()
        brain.actuator._bound_feedback_receipt=('controlled-store','controlled-action')
        result=method(brain,reading,'ordinary')
        self.assertIn('controlled_v1_cannot_consume_model_proposal',result.notes)
        brain.actuator.execute_pump.assert_not_called()


class Phase3IntegrationTests(unittest.TestCase):
    def method(self, class_name, method_name, namespace):
        import ast
        path=Path(__file__).resolve().parents[1]/'services/soil3/phase3/decision_brain.py'
        tree=ast.parse(path.read_text(encoding='utf-8'))
        cls=next(x for x in tree.body if isinstance(x,ast.ClassDef) and x.name==class_name)
        fn=next(x for x in cls.body if isinstance(x,ast.FunctionDef) and x.name==method_name)
        module=ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),fn],type_ignores=[])
        exec(compile(ast.fix_missing_locations(module),str(path),'exec'),namespace)
        return namespace[method_name]

    def test_actuator_rejects_invalid_doses_before_publish(self):
        import math
        from types import SimpleNamespace
        from unittest.mock import Mock
        actuator=SimpleNamespace(cfg=SimpleNamespace(get_constant=lambda key:30),_publish_pump=Mock())
        method=self.method('ActuatorLayer','_activate_pump',dict(math=math,PumpExecutionError=ValueError))
        for dose in [True,-1,float('nan'),float('inf'),31,'5']:
            with self.subTest(dose=dose),self.assertRaises(ValueError):method(actuator,dose)
        actuator._publish_pump.assert_not_called()

    def test_ambiguous_on_failure_still_attempts_off(self):
        import math
        from types import SimpleNamespace
        from unittest.mock import Mock
        from services.soil3.phase3.model_proposal import PumpBudget
        import time
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        now = time.time()
        PumpBudget(root/'watering_budget.sqlite').bootstrap(
            now, now-86400, [], evidence='verified-test-empty-history')
        actuator=SimpleNamespace(cfg=SimpleNamespace(get_constant=lambda key:30),
            _publish_pump=Mock(side_effect=RuntimeError('publish uncertain')),_force_pump_off=Mock())
        method=self.method('ActuatorLayer','_activate_pump',dict(math=math,
            PumpExecutionError=ValueError,logger=Mock(),Path=Path,
            FILE_PATHS={'SYSTEM_STATE':root/'system_state.json'},time=time))
        with self.assertRaises(ValueError):method(actuator,5)
        actuator._force_pump_off.assert_called_once()

    def test_proposal_mode_reaches_ai_before_safe_sleep_and_style(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from enum import Enum,auto
        class Zone(Enum):SENSOR_STALE=auto();SAFE_SLEEP=auto();EMERGENCY=auto();BATTLE_ZONE=auto()
        reading=SimpleNamespace(humidity=44)
        brain=SimpleNamespace(_pending_soak=None,cfg=SimpleNamespace(get_constant=lambda key:key == "MODEL_PROPOSAL_ENABLED"),
            sensor=SimpleNamespace(read=Mock(return_value=reading)),
            _backfill_pending_h12=Mock(),physics=SimpleNamespace(calc_water_ceiling=lambda h:1,assess_zone=lambda r:Zone.SAFE_SLEEP),
            _run_model_cycle=Mock(return_value='model_result'),_style_experiment=Mock(),_finish_result=Mock())
        method=self.method('DecisionBrain','run_cycle',dict(ZoneStatus=Zone))
        self.assertEqual('model_result',method(brain))
        brain._style_experiment.assert_not_called()

    def test_rejected_or_missing_proposal_never_falls_back_to_native_water(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from contextlib import nullcontext
        import logging
        reading=SimpleNamespace(humidity=34)
        brain=SimpleNamespace(cfg=SimpleNamespace(get_constant=lambda key:None),
            _finish_result=Mock(return_value='observe'), actuator=Mock())
        method=self.method('DecisionBrain','_run_model_cycle',dict(
            Path=Path, FILE_PATHS={'SYSTEM_STATE':Path('.test-tmp')/'system_state.json'},
            file_lock=lambda *a,**k:nullcontext(), logger=logging.getLogger('test'),
            ActionPlan=lambda label,seconds:SimpleNamespace(label=label,water_sec=seconds)))
        self.assertEqual('observe',method(brain,reading,'ordinary'))
        brain.actuator.execute_pump.assert_not_called()


class GateAdvisoryTests(unittest.TestCase):
    def test_native_preferences_are_advisory_but_faults_still_block(self):
        from test_controlled_phase3_execution import state_snapshot,strategy_for
        from services.soil3.cloud_gate.gate_v2 import GatePolicy,evaluate_gate_v2
        state=state_snapshot();flags=state['safety']['flags']
        flags['dynamic_cooldown']={'cooldown_sec':21600}
        flags['watering_trigger_guard']={'active':True}
        flags['hard_safety_low_guard']={'effective_low':27.,'updated_at':1790000000.}
        policy=GatePolicy(900,18000,86400,0,native_preferences_advisory=True)
        strategy=strategy_for(state)
        result=evaluate_gate_v2(state,strategy,policy,False,phase3_system_state={})
        self.assertIn(result['decision'],['allow','allow_with_warning'],result['reason_codes'])
        flags['sensor_fault']={'active':True};strategy=strategy_for(state)
        result=evaluate_gate_v2(state,strategy,policy,False,phase3_system_state={})
        self.assertEqual('deny',result['decision'])
        self.assertIn('safety_flag_active:sensor_fault',result['reason_codes'])

    def test_publication_requires_live_provider_and_verified_gate_chain(self):
        from unittest import mock
        from test_soil3_agent_runtime import PipelineTests
        from services.soil3.agent_runtime import runtime_v1 as runtime
        from services.soil3.cloud_trigger import TriggerDecision
        fixture=PipelineTests()
        for live,admitted in [(False,True),(True,False),(True,True)]:
            with self.subTest(live=live,admitted=admitted),tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                value=(fixture.qwen_runtime_config_value(root) if live else fixture.runtime_config_value(root))
                config=runtime.RuntimeConfig.from_dict(value)
                def chain(*,state,**kwargs):
                    strategy=runtime.build_offline_fixture(state)
                    strategy['model'].update(provider='qwen_dashscope',name='test-provider-model')
                    return dict(cloud={'provider':'qwen_dashscope','model':'test-provider-model'},
                        validation={'accepted':True,'reason_codes':[],'strategy':strategy})
                gate=(fixture.admitted_gate if admitted else evaluate_denied_gate)
                with mock.patch.object(runtime,'build_health_snapshot',return_value=fixture.health_snapshot()), \
                     mock.patch.object(runtime,'evaluate_trigger',return_value=TriggerDecision('CALL_QWEN',('fixture',))), \
                     mock.patch.object(runtime,'run_chain',side_effect=chain), \
                     mock.patch.object(runtime,'evaluate_gate_v2',side_effect=gate):
                    record=runtime.run_pipeline(config)
                self.assertEqual(live and admitted,(config.runtime_root/'model_proposals/latest.json').exists())
                self.assertEqual({'physical_actions_performed':False,'phase3_called':False},record['execution'])


def evaluate_denied_gate(**kwargs):
    from services.soil3.cloud_gate.gate_v2 import evaluate_gate_v2
    return evaluate_gate_v2(**kwargs)


if __name__=='__main__':unittest.main()
