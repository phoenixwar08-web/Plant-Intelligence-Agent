import copy
import os
import tempfile
import unittest

import torch

from phase2_predictor.config import DEFAULT_CONFIG
from phase2_predictor.quality import _append
from phase2_predictor.watering_models import WateringModelRuntime
from phase2_predictor.watering_samples import build_dose_response_samples


def sample_rows(config, complete=True):
    sequence_length = int(config["model"]["sequence_length"])
    rows = []
    for index in range(sequence_length + 4):
        phase = "NATURAL"
        seconds = 0.0
        if index == sequence_length:
            phase = "WATERING_ACTIVE"
            seconds = 3.0
        elif index in (sequence_length + 1, sequence_length + 2):
            phase = "RESPONSE_RISING"
        elif index == sequence_length + 3 and not complete:
            phase = "RESPONSE_RISING"
        humidity = 30.0 + max(0, index - sequence_length) * 2.0
        metadata = {
            "time": str(index), "humidity": humidity,
            "temperature": 25.0, "light": 1000.0, "ec": 400.0,
            "watered": int(seconds > 0), "water_seconds": seconds,
            "learned_phase": phase,
            "segment_id": "segment-1" if phase != "NATURAL" else None,
            "training_eligible": True, "training_label": "normal_response",
        }
        features = [0.25, 0.5, humidity / 100.0, 0.4, 0.0, seconds / 120.0, 0.0, 0.0, 0.0]
        rows.append((float(index * 300), features, humidity, metadata))
    return rows


class PeakRecalibrationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        self.config["model"]["sequence_length"] = 4
        self.config["model"]["d_model"] = 16
        self.config["model"]["nhead"] = 4
        self.config["model"]["num_layers"] = 1
        self.config["model"]["dim_feedforward"] = 32
        self.config["paths"]["shadow_weights"] = os.path.join(self.folder.name, "shadow.pth")

    def tearDown(self):
        self.folder.cleanup()

    def test_dose_labels_require_a_naturally_closed_segment(self):
        self.assertEqual(len(build_dose_response_samples(sample_rows(self.config, True), self.config)), 1)
        self.assertEqual(len(build_dose_response_samples(sample_rows(self.config, False), self.config)), 0)

    def test_peak_fit_preserves_encoder_and_trajectory_head(self):
        runtime = WateringModelRuntime(self.config, self.config["paths"]["shadow_weights"])
        sequence = [[0.25, 0.5, 0.3, 0.4, 0.0, 0.025, 0.0, 0.0, 0.0]] * 4
        before = {
            name: value.detach().clone()
            for name, value in runtime.model.state_dict().items()
        }
        initial_mae = runtime.evaluate_peaks([(sequence, 30.0)])
        result = runtime.fit_peak_head(
            [(sequence, 30.0)], [(sequence, 30.0)],
            epochs=80, patience=15, learning_rate=0.01,
            gradient_clip=1.0, batch_size=1,
        )
        after = runtime.model.state_dict()
        for name, value in before.items():
            if not name.startswith("peak_head."):
                self.assertTrue(torch.equal(value, after[name]), name)
        self.assertLessEqual(result["best_validation_mae"], initial_mae)

    def test_quality_issue_append_is_idempotent(self):
        path = os.path.join(self.folder.name, "quality.csv")
        row = {"time": "t", "reason": "gap", "humidity": 30, "water_seconds": 0}
        _append(path, row)
        _append(path, row)
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(len(handle.readlines()), 2)


if __name__ == "__main__":
    unittest.main()
