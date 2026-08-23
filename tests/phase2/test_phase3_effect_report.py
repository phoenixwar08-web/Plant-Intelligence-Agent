import copy
import csv
import json
import os
import tempfile
import unittest

from phase2_predictor.config import DEFAULT_CONFIG
from phase2_predictor.phase3_effect_report import build_phase3_response_effect_report


class Phase3EffectReportTests(unittest.TestCase):
    def test_formal_and_shadow_h12_share_one_real_observation(self):
        with tempfile.TemporaryDirectory() as folder:
            config = copy.deepcopy(DEFAULT_CONFIG)
            queue = os.path.join(folder, "queue.json")
            report = os.path.join(folder, "effect.csv")
            config["paths"]["phase3_response_predictions"] = queue
            config["paths"]["phase3_response_effect_12h"] = report
            with open(queue, "w", encoding="utf-8") as handle:
                json.dump([{
                    "request_id": "soil3-a", "device_code": "soil3",
                    "source_timestamp": 1000.0, "source_humidity": 34.0,
                    "selected_label": "aggressive", "water_sec": 3.0,
                    "zone": "high", "formal_h12": 36.0, "shadow_h12": 35.0,
                }], handle)
            actual_timestamp = 1000.0 + 12 * 3600 + 60
            rows = [(actual_timestamp, [0.0] * 9, 34.5, {})]

            result = build_phase3_response_effect_report(config, rows)

            self.assertEqual(result["phase3_response_completed"], 2)
            with open(report, encoding="utf-8", newline="") as handle:
                values = list(csv.DictReader(handle))
            self.assertEqual(len(values), 2)
            self.assertEqual({row["source"] for row in values}, {"phase3_response"})
            self.assertEqual(
                {row["model"] for row in values},
                {"phase3_response_formal", "phase3_response_shadow"},
            )
            errors = {row["model"]: float(row["absolute_error"]) for row in values}
            self.assertEqual(errors["phase3_response_formal"], 1.5)
            self.assertEqual(errors["phase3_response_shadow"], 0.5)

    def test_future_label_is_written_as_pending(self):
        with tempfile.TemporaryDirectory() as folder:
            config = copy.deepcopy(DEFAULT_CONFIG)
            config["paths"]["phase3_response_predictions"] = os.path.join(folder, "queue.json")
            config["paths"]["phase3_response_effect_12h"] = os.path.join(folder, "effect.csv")
            with open(config["paths"]["phase3_response_predictions"], "w", encoding="utf-8") as handle:
                json.dump([{
                    "request_id": "soil3-b", "source_timestamp": 1000.0,
                    "formal_h12": 36.0, "shadow_h12": 35.0,
                }], handle)
            result = build_phase3_response_effect_report(
                config, [(2000.0, [0.0] * 9, 34.0, {})]
            )
            self.assertEqual(result["phase3_response_pending"], 2)

    def test_observe_label_is_split_from_response_metrics(self):
        with tempfile.TemporaryDirectory() as folder:
            config = copy.deepcopy(DEFAULT_CONFIG)
            queue = os.path.join(folder, "queue.json")
            report = os.path.join(folder, "effect.csv")
            config["paths"]["phase3_response_predictions"] = queue
            config["paths"]["phase3_response_effect_12h"] = report
            with open(queue, "w", encoding="utf-8") as handle:
                json.dump([{
                    "request_id": "soil3-observe", "device_code": "soil3",
                    "source_timestamp": 1000.0, "source_humidity": 34.0,
                    "selected_label": "style_observe", "water_sec": 0.0,
                    "zone": "mid", "formal_h12": 31.0, "shadow_h12": 32.0,
                }], handle)
            actual_timestamp = 1000.0 + 12 * 3600 + 60

            result = build_phase3_response_effect_report(
                config, [(actual_timestamp, [0.0] * 9, 35.0, {})]
            )

            self.assertEqual(result["phase3_response_completed"], 0)
            self.assertEqual(result["phase3_observe_completed"], 2)
            with open(report, encoding="utf-8", newline="") as handle:
                values = list(csv.DictReader(handle))
            self.assertEqual({row["source"] for row in values}, {"phase3_observe"})

    def test_old_completed_queue_items_are_pruned(self):
        with tempfile.TemporaryDirectory() as folder:
            config = copy.deepcopy(DEFAULT_CONFIG)
            config["phase3_response_retention"] = {
                "queue_max_records": 2,
                "queue_retention_days": 1,
                "effect_max_rows": 10,
            }
            queue = os.path.join(folder, "queue.json")
            report = os.path.join(folder, "effect.csv")
            config["paths"]["phase3_response_predictions"] = queue
            config["paths"]["phase3_response_effect_12h"] = report
            with open(queue, "w", encoding="utf-8") as handle:
                json.dump([
                    {"request_id": "old", "source_timestamp": 1000.0, "formal_h12": 30.0},
                    {"request_id": "keep-complete", "source_timestamp": 100000.0, "formal_h12": 31.0},
                    {"request_id": "keep-pending", "source_timestamp": 180000.0, "formal_h12": 32.0},
                ], handle)
            result = build_phase3_response_effect_report(
                config, [(180000.0, [0.0] * 9, 34.0, {})]
            )
            with open(queue, encoding="utf-8") as handle:
                pruned = json.load(handle)
            self.assertEqual([item["request_id"] for item in pruned], ["keep-complete", "keep-pending"])
            self.assertEqual(result["phase3_response_forecasts"], 2)


if __name__ == "__main__":
    unittest.main()
