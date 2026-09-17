import hashlib
import importlib
import unittest


try:
    _contract = importlib.import_module("services.soil3.vision.vision_v1")
except ModuleNotFoundError:
    _contract = None


def vision_record(**overrides):
    record = {
        "schema_version": "vision.v1",
        "device_code": "soil3",
        "image_id": "3f275b48-b86a-4db5-b720-3820ed84bdce",
        "previous_image_id": None,
        "captured_at": "2026-09-17T00:00:00Z",
        "analyzed_at": "2026-09-17T00:00:01Z",
        "image_sha256": hashlib.sha256(b"image").hexdigest(),
        "image_path": "images/2026-09-17/3f275b48-b86a-4db5-b720-3820ed84bdce.jpg",
        "image_quality": "unusable",
        "leaf_droop": None,
        "leaf_spread": None,
        "wilting": None,
        "yellowing": None,
        "visible_damage": None,
        "overall_visual_state": "unavailable",
        "change_vs_previous": "unknown",
        "confidence": None,
        "model": {
            "provider": "qwen",
            "name": "qwen3-vl-flash",
            "prompt_version": "vision.v1",
        },
    }
    record.update(overrides)
    return record


class VisionV1Tests(unittest.TestCase):
    def test_unusable_image_is_a_valid_unknown_observation(self):
        self.assertIsNotNone(_contract, "services.soil3.vision.vision_v1 must define Vision V1")
        record = _contract.validate_vision_record(vision_record())
        self.assertEqual(record["image_quality"], "unusable")
        self.assertIsNone(record["wilting"])
        self.assertEqual(record["overall_visual_state"], "unavailable")

    def test_first_image_cannot_claim_change_without_a_previous_image(self):
        self.assertIsNotNone(_contract, "services.soil3.vision.vision_v1 must define Vision V1")
        with self.assertRaises(_contract.VisionValidationError):
            _contract.validate_vision_record(
                vision_record(change_vs_previous="worsened")
            )

    def test_confidence_must_be_a_finite_probability(self):
        self.assertIsNotNone(_contract, "services.soil3.vision.vision_v1 must define Vision V1")
        with self.assertRaises(_contract.VisionValidationError):
            _contract.validate_vision_record(
                vision_record(
                    image_quality="good",
                    leaf_droop="none",
                    leaf_spread="normal",
                    wilting=False,
                    yellowing="none",
                    visible_damage="none",
                    overall_visual_state="healthy",
                    confidence=1.01,
                )
            )

    def test_unusable_image_cannot_claim_wilting(self):
        self.assertIsNotNone(_contract, "services.soil3.vision.vision_v1 must define Vision V1")
        with self.assertRaises(_contract.VisionValidationError):
            _contract.validate_vision_record(vision_record(wilting=True))

    def test_rejects_an_unknown_field(self):
        self.assertIsNotNone(_contract, "services.soil3.vision.vision_v1 must define Vision V1")
        with self.assertRaises(_contract.VisionValidationError):
            _contract.validate_vision_record(vision_record(unreviewed_note="not allowed"))


if __name__ == "__main__":
    unittest.main()
