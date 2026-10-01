"""核对服务身份和领域样例。"""

import json
import unittest
from pathlib import Path

from service import SERVICE_ID, health_payload


class ContractTest(unittest.TestCase):
    def test_service_identity(self):
        self.assertEqual(health_payload()["service"], SERVICE_ID)

    def test_domain_sample(self):
        data = json.loads(Path("contracts/prescription_case.json").read_text(encoding="utf-8"))
        self.assertEqual(data["service"], SERVICE_ID)
        self.assertTrue(data["sample"])

    def test_event_semantics_contract(self):
        data = json.loads(Path("contracts/prescription_case.json").read_text(encoding="utf-8"))
        sem = data["event_semantics"]
        for etype in ("case_opened", "prescription_decided", "override_granted",
                      "plan_adjusted", "prescription_paused", "symptom_reported",
                      "device_reading", "monitor_finding", "review_resolved"):
            self.assertIn(etype, sem["event_types"])
        self.assertIn("missing_is_unsafe", sem["missing_and_out_of_order"])
        self.assertEqual(
            sem["payload_shapes"]["decision"]["outcome"],
            "eligible | contraindicated | needs_review")
        for code in ("EMERGENCY_SYMPTOM", "DATA_GAP", "UNKNOWN_SIGNAL", "OVERRIDE_EXPIRED"):
            self.assertIn(code, data["vocabulary"]["finding_codes"])


if __name__ == "__main__":
    unittest.main()
