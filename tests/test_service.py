"""服务门面：用例级行为与可重放保证。"""

import tempfile
import unittest
from pathlib import Path

from app.facade import PrescriptionService, ServiceError
from app.ledger import EventStore
from app.registry import RuleRegistry

ROOT = Path(__file__).resolve().parent.parent


def assessment(**over):
    a = {
        "age": 55,
        "activity_baseline": "sedentary",
        "clinical_permission": "granted",
        "recent_metrics": {
            "resting_sbp_mmhg": 128, "resting_dbp_mmhg": 80,
            "resting_hr_bpm": 70, "fasting_glucose_mmol_l": 6.0,
        },
        "medications": [],
        "diagnoses": [],
    }
    a.update(over)
    return a


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = PrescriptionService(EventStore(self.tmp.name), RuleRegistry(ROOT / "rules"))

    def tearDown(self):
        self.tmp.cleanup()

    def open_decide(self, a=None, cid="case-1", at="2026-10-01T08:00:00+08:00"):
        self.svc.open_case("PSEUDO-1", a or assessment(), "dr.li",
                           case_id=cid, at=at)
        return self.svc.decide(cid, "dr.li", at="2026-10-01T08:05:00+08:00")

    def test_open_and_eligible_decision_is_versioned(self):
        dec = self.open_decide()
        p = dec["payload"]
        self.assertEqual(p["outcome"], "eligible")
        self.assertEqual(p["rules_versions"]["contraindications"], "1.0.0")
        self.assertIsNotNone(p["plan_snapshot"]["plan"]["hr_zone"])

    def test_contraindicated_blocks_prescription(self):
        dec = self.open_decide(assessment(joint_injury=True))
        self.assertEqual(dec["payload"]["outcome"], "contraindicated")
        self.assertIsNone(dec["payload"]["plan"])
        tl = self.svc.timeline("case-1")
        self.assertEqual(tl["current_state"], "not_evaluated")

    def test_override_requires_reason_authority_and_expiry(self):
        self.open_decide(assessment(joint_injury=True))
        with self.assertRaises(ServiceError):
            self.svc.grant_override("case-1", "CI_ACUTE_JOINT_INJURY", "   ",
                                    "attending_physician", "2026-10-08T00:00:00+08:00", "dr.wang")
        with self.assertRaises(ServiceError):
            self.svc.grant_override("case-1", "CI_ACUTE_JOINT_INJURY", "理由",
                                    "nurse", "2026-10-08T00:00:00+08:00", "n1")
        with self.assertRaises(ServiceError):
            self.svc.grant_override("case-1", "CI_ACUTE_JOINT_INJURY", "理由",
                                    "attending_physician", "2026-09-01T00:00:00+08:00", "dr.wang")
        with self.assertRaises(ServiceError):
            self.svc.grant_override("case-1", "NOT_A_RULE", "理由",
                                    "attending_physician", "2026-10-08T00:00:00+08:00", "dr.wang")

    def test_override_then_eligible_then_expiry_fails_safe(self):
        self.open_decide(assessment(joint_injury=True))
        self.svc.grant_override(
            "case-1", "CI_ACUTE_JOINT_INJURY", "骨科会诊确认可监督下活动",
            "attending_physician", "2026-10-08T08:00:00+08:00", "dr.wang",
            at="2026-10-01T09:00:00+08:00")
        dec = self.svc.decide("case-1", "dr.wang", at="2026-10-01T09:10:00+08:00")
        self.assertEqual(dec["payload"]["outcome"], "eligible")
        self.assertTrue(dec["payload"]["plan"]["physician_supervised_start"])

        # 覆盖到期当天：复评命中原禁忌 -> 暂停。
        r = self.svc.reassess("case-1", "dr.li", at="2026-10-09T09:00:00+08:00")
        self.assertEqual(r["outcome"], "contraindicated")
        self.assertEqual(r["event"]["payload"]["trigger_codes"], ["CI_ACUTE_JOINT_INJURY"])

        tl = self.svc.timeline("case-1")
        states = {d["day"]: d["state"] for d in tl["days"]}
        self.assertEqual(states["2026-10-01"], "allowed")
        self.assertEqual(states["2026-10-09"], "suspended")

    def test_adjustment_keeps_before_and_after(self):
        self.open_decide()
        ev = self.svc.adjust_plan("case-1", {"minutes": [15, 25]}, "适应良好，小幅延长",
                                  "dr.li", at="2026-10-03T08:00:00+08:00")
        self.assertEqual(ev["payload"]["before"]["plan"]["minutes"], [10, 20])
        self.assertEqual(ev["payload"]["after"]["plan"]["minutes"], [15, 25])
        with self.assertRaises(ServiceError):
            self.svc.adjust_plan("case-1", {"minutes": [15, 25]}, "  ", "dr.li")
        with self.assertRaises(ServiceError):
            self.svc.adjust_plan("case-1", {"minutes": [15, 999]}, "超限", "dr.li")

    def test_resume_requires_note_and_reopens(self):
        dec = self.open_decide()
        self.svc.add_signal("case-1", "symptom_reported",
                            {"code": "joint_pain"}, "p",
                            "2026-10-02T07:05:00+08:00", event_id="p1")
        with self.assertRaises(ServiceError):
            self.svc.resume("case-1", "  ", "dr.li")
        self.svc.resume("case-1", "复诊确认无异常，恢复处方", "dr.li",
                        at="2026-10-03T09:00:00+08:00")
        tl = self.svc.timeline("case-1")
        self.assertEqual(tl["current_state"], "allowed")

    def test_emergency_signal_response_is_guidance_only(self):
        self.open_decide()
        r = self.svc.add_signal("case-1", "symptom_reported",
                                {"code": "chest_pain", "severity": "severe"}, "p",
                                "2026-10-02T07:05:00+08:00", event_id="p1")
        self.assertTrue(r["seek_emergency_care"])
        self.assertEqual(r["day_state"], "escalated")
        guidance = r["new_findings"][0]["guidance"]
        self.assertIn("急救", guidance)
        # 不自动诊断
        self.assertNotIn("诊断为", guidance)

    def test_missing_data_is_not_safe(self):
        dec = self.open_decide(assessment(
            clinical_permission="not_recorded",
            recent_metrics={}))
        self.assertEqual(dec["payload"]["outcome"], "needs_review")
        self.assertIn("REVIEW_MISSING_PERMISSION", dec["payload"]["basis_codes"])

    def test_idempotent_event_id(self):
        self.open_decide()
        kw = dict(event_type="symptom_reported", payload={"code": "mild_soreness"},
                  actor_ref="p", event_time="2026-10-02T07:00:00+08:00", event_id="dup1")
        self.svc.add_signal("case-1", **kw)
        with self.assertRaises(ServiceError) as cm:
            self.svc.add_signal("case-1", **kw)
        self.assertEqual(cm.exception.code, "duplicate_event")

    def test_late_data_marks_late_and_does_not_rewrite_decisions(self):
        self.open_decide()
        self.svc.add_signal("case-1", "session_started", {}, "dev",
                            "2026-10-02T07:00:00+08:00", event_id="s1")
        self.svc.add_signal("case-1", "device_reading",
                            {"metric": "heart_rate_bpm", "value": 110}, "dev",
                            "2026-10-02T07:02:00+08:00", event_id="h1",
                            ingested_at="2026-10-02T07:02:10+08:00")
        # 晚到：event_time 07:01，但 07:05 才收到
        r = self.svc.add_signal("case-1", "device_reading",
                                {"metric": "heart_rate_bpm", "value": 111}, "dev",
                                "2026-10-02T07:01:00+08:00", event_id="h0",
                                ingested_at="2026-10-02T07:05:00+08:00")
        self.assertTrue(r["event"]["late"])
        tl = self.svc.timeline("case-1")
        late_events = [e for e in tl["events"] if e.get("late")]
        self.assertEqual([e["event_id"] for e in late_events], ["h0"])

    def test_as_of_replay_reproduces_historical_day(self):
        self.open_decide()
        self.svc.add_signal("case-1", "symptom_reported", {"code": "chest_pain"}, "p",
                            "2026-10-02T07:05:00+08:00", event_id="p1")
        before = self.svc.timeline("case-1", as_of="2026-10-01T23:59:59+08:00")
        self.assertEqual([d["state"] for d in before["days"]], ["allowed"])
        after = self.svc.timeline("case-1")
        self.assertEqual([d["state"] for d in after["days"]], ["allowed", "escalated"])

    def test_replay_is_identical_from_fresh_service_instance(self):
        self.open_decide()
        for i in range(6):
            self.svc.add_signal(
                "case-1", "device_reading",
                {"metric": "heart_rate_bpm", "value": 140 + i}, "dev",
                f"2026-10-02T07:{10 + i * 2:02d}:00+08:00", event_id=f"h{i}")
        tl1 = self.svc.timeline("case-1")
        svc2 = PrescriptionService(EventStore(self.tmp.name), RuleRegistry(ROOT / "rules"))
        tl2 = svc2.timeline("case-1")
        key = lambda tl: [(d["day"], d["state"],
                           [(f["code"], f["severity"]) for f in d["open_findings"]])
                          for d in tl["days"]]
        self.assertEqual(key(tl1), key(tl2))
        self.assertEqual(len(tl1["events"]), len(tl2["events"]))

    def test_data_gap_persists_suspend_finding(self):
        self.open_decide()
        self.svc.add_signal("case-1", "session_started", {}, "dev",
                            "2026-10-02T07:00:00+08:00", event_id="s1")
        r = self.svc.add_signal("case-1", "device_reading",
                                {"metric": "heart_rate_bpm", "value": 110}, "dev",
                                "2026-10-02T07:02:00+08:00", event_id="h1")
        self.assertEqual(r["day_state"], "allowed")
        r = self.svc.add_signal("case-1", "device_reading",
                                {"metric": "heart_rate_bpm", "value": 111}, "dev",
                                "2026-10-02T07:12:00+08:00", event_id="h2")
        self.assertEqual(r["day_state"], "suspended")
        self.assertTrue(any(f["code"] == "DATA_GAP" for f in r["new_findings"]))
        # 持久化的 monitor_finding 审计事件存在
        raw = self.svc.store.load_raw("case-1")
        self.assertTrue(any(e["event_type"] == "monitor_finding"
                            and e["payload"]["code"] == "DATA_GAP" for e in raw))

    def test_manual_review_alone_does_not_resume(self):
        self.open_decide()
        r = self.svc.add_signal("case-1", "symptom_reported", {"code": "joint_pain"}, "p",
                                "2026-10-02T07:05:00+08:00", event_id="p1")
        fid = r["new_findings"][0]["finding_id"]
        rr = self.svc.add_signal(
            "case-1", "review_resolved",
            {"finding_event_ids": [fid], "resolution": "keep_suspended",
             "note": "建议继续观察，不恢复"},
            "dr.li", "2026-10-02T08:00:00+08:00", event_id="rv1")
        self.assertEqual(rr["day_state"], "suspended")

    def test_suspension_carries_across_eventless_days(self):
        self.open_decide()
        self.svc.add_signal("case-1", "symptom_reported", {"code": "chest_pain"}, "p",
                            "2026-10-02T07:05:00+08:00", event_id="p1")
        # 10-05 才有医生动作；中间无事件的日子仍是 escalated/suspended。
        self.svc.resume("case-1", "急诊评估无异常后恢复", "dr.li",
                        at="2026-10-05T09:00:00+08:00")
        tl = self.svc.timeline("case-1")
        states = {d["day"]: d["state"] for d in tl["days"]}
        self.assertEqual(states["2026-10-03"], "escalated")
        self.assertEqual(states["2026-10-04"], "escalated")
        self.assertEqual(states["2026-10-05"], "allowed")


if __name__ == "__main__":
    unittest.main()
