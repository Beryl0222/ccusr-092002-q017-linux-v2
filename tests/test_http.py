"""HTTP 接口端到端测试（真实监听本地端口，线程内运行）。"""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import service
from prescription.flows import NOT_PRESCRIBABLE
from prescription.store import AppendOnlyStore


class HttpTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        ledger = str(Path(self.tmp.name) / "ledger.jsonl")
        service.Handler.service = service.build_service(ledger)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), service.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def _request(self, method, path, payload=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def test_health(self):
        status, body = self._request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["service"], service.SERVICE_ID)

    def test_rule_pack_lists_levels(self):
        status, body = self._request("GET", "/v1/rule-packs")
        self.assertEqual(status, 200)
        rules = body["packs"][0]["rules"]
        self.assertTrue(any(r["level"] == "ABSOLUTE" for r in rules))

    def test_full_safe_flow_and_replay(self):
        assessment = {
            "age": 45, "diagnoses": [],
            "recent_metrics": {"systolic_mmhg": 122, "diastolic_mmhg": 78,
                               "resting_hr_bpm": 66, "measured_on": "2026-09-28"},
            "medications": [], "activity_baseline": {"level": "light"},
            "permission_opinion": [], "today": "2026-10-01",
        }
        status, body = self._request("POST", "/v1/patients/P1/assessments",
                                     {"assessment": assessment, "clinician_id": "dr-li",
                                      "today": "2026-10-01"})
        self.assertEqual(status, 200)
        status, body = self._request("POST", "/v1/patients/P1/prescriptions",
                                     {"clinician_id": "dr-li", "today": "2026-10-01"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["data"]["prescription"]["version"], "rx-1")

        day = "2026-10-02"
        for seq, etype, extra in [
            (1, "symptom_check", {"symptoms": ["none"]}),
            (2, "vitals_reading", {"systolic_mmhg": 122, "diastolic_mmhg": 78,
                                   "heart_rate_bpm": 68, "glucose_mmol_l": 6.0}),
            (3, "session_start", {}),
            (4, "session_end", {"duration_min": 20}),
        ]:
            event = {"event_id": f"P1-{seq}", "patient_ref": "P1",
                     "ts": f"{day}T07:{seq*5:02d}:00", "seq": seq, "type": etype, **extra}
            status, body = self._request("POST", "/v1/events", event)
            self.assertEqual(status, 200, body)

        status, body = self._request("GET", f"/v1/patients/P1/days/{day}")
        self.assertEqual(status, 200)
        self.assertEqual(body["decision"]["decision"], "APPROVE")
        self.assertEqual(body["inputs"]["prescription_version"], "rx-1")

    def test_absolute_contraindication_returns_403(self):
        assessment = {
            "age": 70, "diagnoses": ["unstable_angina"],
            "recent_metrics": {"systolic_mmhg": 130, "diastolic_mmhg": 80,
                               "resting_hr_bpm": 70, "measured_on": "2026-09-28"},
            "medications": [], "activity_baseline": {"level": "light"},
            "permission_opinion": [],
        }
        self._request("POST", "/v1/patients/P2/assessments",
                      {"assessment": assessment, "clinician_id": "dr-li",
                       "today": "2026-10-01"})
        status, body = self._request("POST", "/v1/patients/P2/prescriptions",
                                     {"clinician_id": "dr-li", "today": "2026-10-01"})
        self.assertEqual(status, 403)
        self.assertEqual(body["code"], NOT_PRESCRIBABLE)
        # 试图覆盖绝对禁忌 -> 403
        status, body = self._request("POST", "/v1/patients/P2/overrides", {
            "clinician_id": "dr-wang", "authority": "SPECIALIST",
            "rule_codes": ["ABS_CV_UNSTABLE"], "reason": "患者坚持",
            "valid_until": "2026-10-10", "today": "2026-10-01"})
        self.assertEqual(status, 403)
        self.assertEqual(body["code"], "ABSOLUTE_NOT_OVERRIDABLE")

    def test_emergency_event_replays_with_advice_only(self):
        assessment = {
            "age": 45, "diagnoses": [],
            "recent_metrics": {"systolic_mmhg": 122, "diastolic_mmhg": 78,
                               "resting_hr_bpm": 66, "measured_on": "2026-09-28"},
            "medications": [], "activity_baseline": {"level": "light"},
            "permission_opinion": [],
        }
        self._request("POST", "/v1/patients/P4/assessments",
                      {"assessment": assessment, "clinician_id": "dr-li",
                       "today": "2026-10-01"})
        self._request("POST", "/v1/patients/P4/prescriptions",
                      {"clinician_id": "dr-li", "today": "2026-10-01"})
        day = "2026-10-03"
        for event in [
            {"event_id": "P4-1", "patient_ref": "P4", "ts": f"{day}T07:00:00",
             "seq": 1, "type": "symptom_check", "symptoms": ["none"]},
            {"event_id": "P4-2", "patient_ref": "P4", "ts": f"{day}T07:05:00",
             "seq": 2, "type": "vitals_reading",
             "systolic_mmhg": 120, "diastolic_mmhg": 76,
             "heart_rate_bpm": 66, "glucose_mmol_l": 5.8},
            {"event_id": "P4-3", "patient_ref": "P4", "ts": f"{day}T07:10:00",
             "seq": 3, "type": "session_start"},
            {"event_id": "P4-4", "patient_ref": "P4", "ts": f"{day}T07:25:00",
             "seq": 4, "type": "symptom_onset", "symptom": "chest_pain"},
        ]:
            self._request("POST", "/v1/events", event)
        status, body = self._request("GET", f"/v1/patients/P4/days/{day}")
        self.assertEqual(status, 200)
        dec = body["decision"]
        self.assertEqual(dec["decision"], "EMERGENCY_ADVICE")
        self.assertIn("120", dec["emergency_advice"])

    def test_bad_json_is_rejected(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/events",
            data=b"{not-json", headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("应返回 400")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)

    def test_ledger_verify_endpoint(self):
        status, body = self._request("GET", "/v1/ledger/verify")
        self.assertEqual(status, 200)
        self.assertEqual(body["hash_chain"], "ok")


if __name__ == "__main__":
    unittest.main()
