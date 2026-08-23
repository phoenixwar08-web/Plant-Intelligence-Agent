import copy
import os
import tempfile
import unittest
from unittest import mock

import torch

from phase2_predictor.config import DEFAULT_CONFIG
from phase2_predictor.learning import AdaptiveWateringLearner
from phase2_predictor.natural_training import NaturalTrainer
from phase2_predictor.service import PredictorService
from phase2_predictor.shadow_training import ShadowTrainer
from phase2_predictor.watering_models import WateringModelRuntime


class ConfigurableRecorder:
    def __init__(self):
        self.received = None

    def update_config(self, config):
        self.received = config

    def reload_if_changed(self):
        return False


class ConfigManagerStub:
    def __init__(self, config):
        self.config = config
        self.reloaded = False

    def reload_if_changed(self):
        self.reloaded = True

    def get(self):
        return self.config


class ConfigReloadTests(unittest.TestCase):
    def test_shadow_trainer_propagates_one_snapshot(self):
        trainer = ShadowTrainer.__new__(ShadowTrainer)
        trainer.runtime = ConfigurableRecorder()
        trainer.natural_trainer = ConfigurableRecorder()
        trainer.segmented = ConfigurableRecorder()
        trainer.phase_tracker = ConfigurableRecorder()
        config = copy.deepcopy(DEFAULT_CONFIG)

        trainer._apply_config(config)

        self.assertIs(trainer.config, config)
        self.assertIs(trainer.runtime.received, config)
        self.assertIs(trainer.natural_trainer.received, config)
        self.assertIs(trainer.segmented.received, config)
        self.assertIs(trainer.phase_tracker.received, config)

    def test_predictor_reload_uses_component_update_methods(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        service = PredictorService.__new__(PredictorService)
        service.config_manager = ConfigManagerStub(config)
        service.watering_runtime = ConfigurableRecorder()
        service.shadow_runtime = ConfigurableRecorder()
        service.natural_runtime = ConfigurableRecorder()
        service.learner = ConfigurableRecorder()
        service.calibration = ConfigurableRecorder()

        service._reload_config()

        self.assertTrue(service.config_manager.reloaded)
        self.assertIs(service.config, config)
        self.assertIs(service.watering_runtime.received, config)
        self.assertIs(service.shadow_runtime.received, config)
        self.assertIs(service.natural_runtime.received, config)
        self.assertIs(service.learner.received, config)
        self.assertIs(service.calibration.received, config)

    def test_runtime_updates_path_and_learning_rate(self):
        runtime = WateringModelRuntime.__new__(WateringModelRuntime)
        runtime.weight_path_key = "shadow_weights"
        runtime.optimizer = type("Optimizer", (), {"param_groups": [{"lr": 9.0}]})()
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["paths"]["shadow_weights"] = "/tmp/reloaded.pth"
        config["training"]["learning_rate"] = 0.0123

        runtime.update_config(config)

        self.assertIs(runtime.config, config)
        self.assertEqual(runtime.path, "/tmp/reloaded.pth")
        self.assertEqual(runtime.optimizer.param_groups[0]["lr"], 0.0123)

    def test_runtime_hot_reloads_only_after_weight_mtime_changes(self):
        runtime = WateringModelRuntime.__new__(WateringModelRuntime)
        runtime.path = __file__
        runtime.weights_mtime = os.path.getmtime(__file__)
        runtime.reload_weights = mock.Mock(return_value=True)
        self.assertFalse(runtime.reload_if_changed())
        runtime.weights_mtime = 0.0
        self.assertTrue(runtime.reload_if_changed())
        runtime.reload_weights.assert_called_once()

    def test_learner_updates_derived_state_path(self):
        learner = AdaptiveWateringLearner.__new__(AdaptiveWateringLearner)
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["paths"]["state"] = "/tmp/reloaded-state.json"

        learner.update_config(config)

        self.assertIs(learner.config, config)
        self.assertEqual(learner.state_path, "/tmp/reloaded-state.json")


class FailureRecoveryTests(unittest.TestCase):
    def test_corrupt_natural_state_uses_defaults(self):
        handle = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False)
        try:
            handle.write("{not valid json")
            handle.close()
            trainer = NaturalTrainer.__new__(NaturalTrainer)
            trainer.config = copy.deepcopy(DEFAULT_CONFIG)
            trainer.config["paths"]["natural_state"] = handle.name
            trainer.logger = mock.Mock()

            state = trainer._load_state()

            self.assertFalse(state["offline_bootstrap_complete"])
            self.assertEqual(state["trained_natural_windows"], 0)
            trainer.logger.exception.assert_called_once()
        finally:
            handle.close()
            os.unlink(handle.name)

    def test_model_error_falls_back_to_physical_prediction(self):
        service = PredictorService.__new__(PredictorService)
        service.config = copy.deepcopy(DEFAULT_CONFIG)
        service.logger = mock.Mock()
        service.learner = object()
        runtime = type("Runtime", (), {"status": "loaded"})()
        service.watering_runtime = runtime
        service.natural_runtime = runtime
        candidate = {"label": "water_3", "water_sec": 3.0}
        physical = {"trajectory": [1.0]}

        with mock.patch("phase2_predictor.service._model_prediction", side_effect=RuntimeError("broken model")), mock.patch("phase2_predictor.service._physical_prediction", return_value=physical):
            result, fallback = service._predict_candidate([], candidate, 12)

        self.assertIs(result, physical)
        self.assertTrue(fallback)
        service.logger.warning.assert_called_once()


class LossWeightTests(unittest.TestCase):
    def test_h12_point_weight_is_read_from_config(self):
        runtime = WateringModelRuntime.__new__(WateringModelRuntime)
        runtime.is_natural = False
        predictions = torch.zeros((1, 12), dtype=torch.float32)
        targets = torch.zeros((1, 12), dtype=torch.float32)
        targets[:, -1] = 1.0
        masks = torch.ones_like(predictions)

        low = copy.deepcopy(DEFAULT_CONFIG)
        low["loss_weights"]["trajectory_h12_point"] = 1.0
        runtime.config = low
        low_loss = runtime._trajectory_loss(predictions, targets, masks, focus_h12=True)

        high = copy.deepcopy(DEFAULT_CONFIG)
        high["loss_weights"]["trajectory_h12_point"] = 8.0
        runtime.config = high
        high_loss = runtime._trajectory_loss(predictions, targets, masks, focus_h12=True)

        self.assertGreater(float(high_loss), float(low_loss))

    def test_pickle_paths_are_not_in_active_default_config(self):
        self.assertNotIn("memory_bank", DEFAULT_CONFIG["paths"])
        self.assertNotIn("training_queue", DEFAULT_CONFIG["paths"])

    def test_all_loss_weights_have_defaults(self):
        expected = {"trajectory_h12_point", "trajectory", "peak", "h12", "consistency", "time", "smoothness", "natural_rise"}
        self.assertEqual(set(DEFAULT_CONFIG["loss_weights"]), expected)


if __name__ == "__main__":
    unittest.main()
