"""HTTP 端到端：真实线程服务器 + urllib，覆盖主要用例与错误映射。"""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import service as service_module
from app.facade import PrescriptionService
from app.ledger import EventStore
from app.registry import RuleRegistry

ROOT = Path(__file__).resolve().parent.parent


class HttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        service_module._SERVICE = PrescriptionService(
            EventStore(self.tmp.name), RuleRegistry(ROOT / "rules"))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), service_module.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def call(self, method, path, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_health_and_rules(self):
        status, body = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["service"], "exercise-prescription-safety")
        status, body = self.call("GET", "/v1/rules")
        self.assertEqual(status, 200)
        self.assertIn("1.0.0", body["rules"]["contraindications"])

    def test_full_safety_flow(self):
        assessment = {
            "age": 60, "activity_baseline": "sedentary",
            "clinical_permission": "granted",
            "recent_metrics": {"resting_sbp_mmhg": 150, "resting_dbp_mmhg": 94,
                               "resting_hr_bpm": 72},
            "medications": [], "diagnoses": []}
        status, opened = self.call("POST", "/v1/cases", {
            "patient_ref": "PSEUDO-W", "actor_ref": "dr.li",
            "at": "2026-10-01T08:00:00+08:00", "assessment": assessment})
        self.assertEqual(status, 201)
        cid = opened["case_id"]

        status, dec = self.call("POST", f"/v1/cases/{cid}/decisions",
                                {"actor_ref": "dr.li", "at": "2026-10-01T08:05:00+08:00"})
        self.assertEqual(status, 201)
        self.assertEqual(dec["payload"]["outcome"], "eligible")

        # 急症 -> escalated，只给就医提示
        status, sig = self.call("POST", f"/v1/cases/{cid}/events", {
            "actor_ref": "watch", "event_type": "symptom_reported",
            "event_time": "2026-10-02T07:10:00+08:00", "event_id": "p1",
            "payload": {"code": "syncope"}})
        self.assertEqual(status, 201)
        self.assertTrue(sig["seek_emergency_care"])
        self.assertEqual(sig["day_state"], "escalated")

        # 历史重放：10-01 仍 allowed
        status, tl = self.call("GET", f"/v1/cases/{cid}/timeline")
        self.assertEqual(status, 200)
        states = {d["day"]: d["state"] for d in tl["days"]}
        self.assertEqual(states["2026-10-01"], "allowed")
        self.assertEqual(states["2026-10-02"], "escalated")

        # 没有医生理由不能恢复
        status, err = self.call("POST", f"/v1/cases/{cid}/resume", {"note": " "})
        self.assertEqual(status, 400)
        status, _ = self.call("POST", f"/v1/cases/{cid}/resume",
                              {"note": "急诊评估为体位性低血压，已纠正，恢复处方",
                               "actor_ref": "dr.li", "at": "2026-10-03T09:00:00+08:00"})
        self.assertEqual(status, 201)
        status, tl = self.call("GET", f"/v1/cases/{cid}/timeline")
        self.assertEqual(tl["current_state"], "allowed")

    def test_missing_fields_fail_safe(self):
        status, opened = self.call("POST", "/v1/cases", {
            "patient_ref": "PSEUDO-X", "actor_ref": "dr.li",
            "at": "2026-10-01T08:00:00+08:00",
            "assessment": {"activity_baseline": "sedentary", "recent_metrics": {}}})
        cid = opened["case_id"]
        status, dec = self.call("POST", f"/v1/cases/{cid}/decisions",
                                {"actor_ref": "dr.li", "at": "2026-10-01T08:05:00+08:00"})
        self.assertEqual(dec["payload"]["outcome"], "needs_review")

    def test_override_must_be_documented(self):
        status, opened = self.call("POST", "/v1/cases", {
            "patient_ref": "PSEUDO-Y", "actor_ref": "dr.li",
            "at": "2026-10-01T08:00:00+08:00",
            "assessment": {"age": 50, "activity_baseline": "sedentary",
                           "clinical_permission": "granted", "joint_injury": True,
                           "recent_metrics": {"resting_sbp_mmhg": 120,
                                              "resting_dbp_mmhg": 78,
                                              "resting_hr_bpm": 70}}})
        cid = opened["case_id"]
        status, body = self.call("POST", f"/v1/cases/{cid}/overrides", {
            "rule_code": "CI_ACUTE_JOINT_INJURY", "reason": "",
            "authority": "attending_physician",
            "valid_until": "2026-10-08T00:00:00+08:00", "actor_ref": "dr.wang"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_request")

    def test_unknown_case_404_and_bad_json(self):
        status, body = self.call("GET", "/v1/cases/nope/timeline")
        self.assertEqual(status, 404)
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/cases", data=b"{not json",
            method="POST", headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("应当拒绝非法 JSON")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)


if __name__ == "__main__":
    unittest.main()
