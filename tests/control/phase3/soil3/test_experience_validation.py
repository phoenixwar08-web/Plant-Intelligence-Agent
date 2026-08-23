import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLANT_AGENT_ROOT = ROOT.parents[1] / "plant_agent"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(PLANT_AGENT_ROOT))

from experience_validation import ExperienceValidationBridge  # noqa: E402


class Reading:
    humidity = 35.0
    sensor_stale = False
    sensor_stale_hard = False


class FakeRepository:
    def __init__(self, *, manual=False, probe=False):
        self.manual = manual
        self.probe = probe
        self.claims = []
        self.executions = []

    def recent_human_events(self, _device, _since):
        return self.manual, self.probe

    def claim_for_phase3(self, **kwargs):
        self.claims.append(kwargs)
        return {"trial_id": "VR-ABC", "candidate_id": "XP-ABC", "action_sec": kwargs["action_sec"]}

    def record_execution(self, trial_id, **kwargs):
        self.executions.append((trial_id, kwargs))

    def release_claim(self, trial_id):
        self.released = trial_id


class ExperienceValidationBridgeTests(unittest.TestCase):
    def test_claims_only_after_a_positive_local_action_has_passed_safety(self):
        repository = FakeRepository()
        bridge = ExperienceValidationBridge(repository=repository)

        claim = bridge.claim_if_eligible(Reading(), {}, action_sec=3.0, plan_label="local_plan")

        self.assertEqual(claim["trial_id"], "VR-ABC")
        self.assertEqual(len(repository.claims), 1)

    def test_does_not_claim_when_phase3_selected_observation_only(self):
        repository = FakeRepository()
        bridge = ExperienceValidationBridge(repository=repository)

        self.assertIsNone(bridge.claim_if_eligible(Reading(), {}, action_sec=0.0, plan_label="observe"))
        self.assertEqual(repository.claims, [])

    def test_does_not_claim_when_a_recent_manual_event_exists(self):
        repository = FakeRepository(manual=True)
        bridge = ExperienceValidationBridge(repository=repository)

        self.assertIsNone(bridge.claim_if_eligible(Reading(), {}, action_sec=3.0, plan_label="local_plan"))
        self.assertEqual(repository.claims, [])

    def test_records_execution_only_after_the_existing_pump_call_succeeds(self):
        repository = FakeRepository()
        bridge = ExperienceValidationBridge(repository=repository)
        claim = bridge.claim_if_eligible(Reading(), {}, action_sec=3.0, plan_label="local_plan")

        bridge.record_execution(claim, Reading(), action_sec=3.0, plan_label="local_plan")

        self.assertEqual(repository.executions[0][0], "VR-ABC")
        self.assertEqual(repository.executions[0][1]["action_sec"], 3.0)
