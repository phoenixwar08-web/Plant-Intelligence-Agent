import copy
import unittest

from services.soil3.cloud_strategy.watering_decision import validate_output, build_input, adapt_output, prepare_input


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.context = build_input(
            {'observed_at': '2026-11-16T00:00:00Z', 'soil': {'humidity_percent': 32},
             'air': {}, 'trends': {}, 'irrigation': {}, 'data_quality': {}},
            {'decision_id': 'test', 'care_process_id': 'p', 'deadline_at': 300,
             'input': {'script_reference': {'suggested_seconds': 5},
                       'execution_constraints': {'min_seconds': 3, 'max_seconds': 30,
                                                'min_check_seconds': 300, 'max_check_seconds': 3600}}})
        self.output = dict(action='water', watering_seconds=5,
            reason_summary='土壤读数持续下降，建议小剂量补水。',
            evidence=[{'field': 'current_observation.sensor.humidity_percent', 'finding': '当前读数32%'}],
            uncertainties=['缺少本轮视觉'], next_check_after_seconds=1800)

    def test_dense_vision_keeps_decision_evidence(self):
        self.context['current_observation']['vision'] = {
            'facts': [{f'field_{i}': i for i in range(200)}]
        }
        self.context['script_reference']['suggested_action'] = 'water'
        self.context['current_episode']['response_so_far'] = {'humidity_delta_pp': 2.7}
        context = prepare_input(self.context)
        self.assertEqual(192, len(context['decision_context']['evidence_fields']))
        for field in ('script_reference.suggested_seconds',
                      'script_reference.suggested_action',
                      'current_episode.response_so_far.humidity_delta_pp'):
            output = dict(self.output, evidence=[{'field': field, 'finding': 'Observed fact'}])
            validate_output(output, context)

    def test_legal_output_preserves_seconds_and_unknown_estimates(self):
        self.assertEqual(self.output, validate_output(self.output, self.context))
        adapted = adapt_output(self.output)
        self.assertEqual(5, adapted['actions'][0]['pump_seconds'])
        self.assertIsNone(adapted['confidence'])
        self.assertIsNone(adapted['expected_outcome']['soil_moisture'])

    def test_illegal_numbers_and_actions_are_rejected(self):
        for seconds in (True, '5', -1, float('nan'), float('inf'), 31, 2):
            value = copy.deepcopy(self.output); value['watering_seconds'] = seconds
            with self.assertRaises(ValueError): validate_output(value, self.context)
        value = dict(self.output, action='observe')
        with self.assertRaises(ValueError): validate_output(value, self.context)

    def test_evidence_must_reference_available_input_and_no_extra_fields(self):
        for field in ('future.health', 'current_observation.vision'):
            value = copy.deepcopy(self.output); value['evidence'][0]['field'] = field
            with self.assertRaises(ValueError): validate_output(value, self.context)
        with self.assertRaises(ValueError): validate_output(dict(self.output, thoughts='hidden'), self.context)

    def test_missing_vision_is_explicit_and_recheck_is_bounded(self):
        self.assertIsNone(self.context['current_observation']['vision'])
        with self.assertRaises(ValueError):
            validate_output(dict(self.output, next_check_after_seconds=1), self.context)

    def test_unavailable_history_is_null_not_evidence_of_no_watering(self):
        self.assertIsNone(self.context['recent_history']['watering_windows'])
        self.assertIsNone(self.context['current_episode']['executed_actions'])
        value = copy.deepcopy(self.output)
        value['evidence'][0]['field'] = 'recent_history.watering_windows'
        with self.assertRaises(ValueError): validate_output(value, self.context)

    def test_control_reference_is_present_but_not_a_botanical_claim(self):
        state = {'safety': {'target_low': 33, 'field_capacity': 45}, 'irrigation': {'last_water_at': None}}
        context = build_input(state, {'decision_id':'x','care_process_id':'p','deadline_at':300,
                              'input': {'execution_constraints': self.context['execution_constraints']}})
        self.assertEqual(33, context['execution_constraints']['control_reference']['target_low'])
        self.assertEqual('uncalibrated_reading', context['current_observation']['measurement_limits']['ec_raw'])

    def test_unbound_off_and_requested_seconds_are_not_a_completed_action(self):
        context = build_input({}, {'decision_id':'x','care_process_id':'p','deadline_at':300,
                              'input': {'execution_constraints': self.context['execution_constraints']}},
                              system_state={'pump_last_off_at':100, 'pump_last_command_sec':6})
        command = context['recent_history']['controller_command_state']
        self.assertEqual('not_verified_same_action', command['association'])
        self.assertNotIn('finished_at_epoch_seconds', command)
        self.assertEqual(6, command['last_requested_seconds'])

    def test_input_lists_real_evidence_fields_and_excludes_missing_history(self):
        fields = self.context['decision_context']['evidence_fields']
        self.assertIn('current_observation.sensor.humidity_percent', fields)
        self.assertNotIn('recent_history.watering_windows', fields)

    def test_numeric_list_reference_resolves_actual_input_not_an_invented_field(self):
        state = {'soil': {'humidity_percent':32}}
        context = build_input(state, {'decision_id':'x','care_process_id':'p','deadline_at':300,
            'input': {'execution_constraints':self.context['execution_constraints']}},
            recent_history={'sensor_series':[{'humidity':33}], 'watering_windows':None})
        output = copy.deepcopy(self.output)
        output['evidence'][0]['field'] = 'recent_history.sensor_series.0.humidity'
        self.assertEqual(output, validate_output(output, context))

    def test_unknown_evidence_error_identifies_the_bounded_field_path(self):
        output = copy.deepcopy(self.output)
        output['evidence'][0]['field'] = 'current_observation.sensor.fake_moisture'
        with self.assertRaisesRegex(ValueError, 'current_observation.sensor.fake_moisture'):
            validate_output(output, self.context)

    def test_observe_does_not_create_water_action(self):
        value = dict(self.output, action='observe', watering_seconds=0)
        self.assertEqual('observe', adapt_output(validate_output(value, self.context))['actions'][0]['type'])

    def test_new_prompt_unknown_estimates_pass_formal_validator_but_legacy_does_not(self):
        from test_cloud_strategy import sample_state, valid_strategy
        from services.soil3.cloud_strategy.validator import StrategyValidator
        state = sample_state()
        strategy = valid_strategy(state)
        strategy.update(adapt_output(self.output))
        strategy['model']['prompt_version'] = 'watering-decision-prompt.v1'
        validator = StrategyValidator()
        self.assertTrue(validator.validate(strategy, state).accepted)
        strategy['model']['prompt_version'] = 'strategy-prompt.v2'
        self.assertFalse(validator.validate(strategy, state).accepted)
