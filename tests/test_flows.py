"""应用流程测试：开具、版本化调整、医生覆盖、自动暂停与按日重放。"""

import tempfile
import unittest
from datetime import date
from pathlib import Path

from prescription.flows import (
    NEEDS_INFO,
    NOT_PRESCRIBABLE,
    PRESCRIBABLE,
    RELATIVE_BLOCKED,
    SafetyService,
)
from prescription.store import AppendOnlyStore, LedgerError

TODAY = date(2026, 10, 1)


def healthy_assessment(**over):
    a = {
        "age": 45,
        "diagnoses": [],
        "recent_metrics": {
            "systolic_mmhg": 122, "diastolic_mmhg": 78,
            "resting_hr_bpm": 66, "measured_on": "2026-09-28",
        },
        "medications": [],
        "activity_baseline": {"level": "light"},
        "permission_opinion": [],
    }
    a.update(over)
    return a


def day_events(patient, day, *, tail=None):
    def ev(seq, etype, clock, **data):
        e = {"event_id": f"{patient}-{seq}", "patient_ref": patient,
             "ts": f"{day}T{clock}", "seq": seq, "type": etype}
        e.update(data)
        return e

    events = [
        ev(1, "symptom_check", "07:00", symptoms=["none"]),
        ev(2, "vitals_reading", "07:05",
           systolic_mmhg=122, diastolic_mmhg=78, heart_rate_bpm=68, glucose_mmol_l=6.0),
        ev(3, "session_start", "07:10"),
    ]
    if tail is None:
        events.append(ev(4, "session_end", "07:35", duration_min=20))
    else:
        events.extend(tail(ev, start=4))
    return events


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AppendOnlyStore(Path(self.tmp.name) / "ledger.jsonl")
        self.svc = SafetyService(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def enroll(self, patient="P1", assessment=None):
        a = assessment or healthy_assessment()
        r = self.svc.record_assessment(patient, a, "dr-li", TODAY)
        self.assertTrue(r.ok)
        return a


class PrescribeTest(ServiceTestCase):
    def test_prescribe_creates_first_version_with_plan(self):
        self.enroll()
        r = self.svc.prescribe("P1", "dr-li", TODAY)
        self.assertEqual(r.code, PRESCRIBABLE)
        rx = r.data["prescription"]
        self.assertEqual(rx["version"], "rx-1")
        self.assertEqual(rx["supersedes"], None)
        self.assertEqual(rx["status"], "active")
        self.assertIn("hr_zone", r.data["plan"])
        self.assertEqual(rx["rule_pack_id"], self.svc.pack_id)

    def test_missing_assessment_blocks(self):
        r = self.svc.prescribe("nobody", "dr-li", TODAY)
        self.assertEqual(r.code, NEEDS_INFO)

    def test_incomplete_intake_blocks(self):
        self.svc.record_assessment("P1", {"age": 40}, "dr-li", TODAY)
        r = self.svc.prescribe("P1", "dr-li", TODAY)
        self.assertEqual(r.code, NEEDS_INFO)
        self.assertTrue(r.data["missing"])

    def test_stale_metrics_block_prescribing(self):
        self.enroll(assessment=healthy_assessment(
            recent_metrics={**healthy_assessment()["recent_metrics"],
                            "measured_on": "2026-09-01"}))
        r = self.svc.prescribe("P1", "dr-li", TODAY)
        self.assertEqual(r.code, NEEDS_INFO)
        self.assertIn("复测", r.message)

    def test_absolute_contraindication_is_not_prescribable_and_creates_no_version(self):
        self.enroll("P2", healthy_assessment(diagnoses=["unstable_angina"]))
        r = self.svc.prescribe("P2", "dr-li", TODAY)
        self.assertEqual(r.code, NOT_PRESCRIBABLE)
        self.assertEqual(self.svc.history("P2")["prescription_versions"], [])

    def test_relative_contraindication_blocks_until_clearance_or_override(self):
        self.enroll("P3", healthy_assessment(
            diagnoses=[{"code": "diabetes", "hba1c_percent": 9.6}]))
        self.assertEqual(self.svc.prescribe("P3", "dr-li", TODAY).code, RELATIVE_BLOCKED)
        ok = self.svc.grant_override(
            "P3", "dr-wang", "SPECIALIST", ["REL_DM_UNSTABLE"],
            "内分泌会诊中，降档起步并密切监测", "2026-10-14", TODAY)
        self.assertTrue(ok.ok)
        r = self.svc.prescribe("P3", "dr-wang", TODAY)
        self.assertEqual(r.code, PRESCRIBABLE)
        self.assertIn("REL_DM_UNSTABLE", r.data["prescription"]["overridden_codes"])
        self.assertTrue(r.data["prescription"]["dose"]["downgraded"])


class OverrideTest(ServiceTestCase):
    def test_absolute_rule_cannot_be_overridden(self):
        self.enroll("P2", healthy_assessment(diagnoses=["unstable_angina"]))
        r = self.svc.grant_override(
            "P2", "dr-wang", "SPECIALIST", ["ABS_CV_UNSTABLE"], "患者坚持", "2026-10-10", TODAY)
        self.assertEqual(r.code, "ABSOLUTE_NOT_OVERRIDABLE")

    def test_override_requires_reason_authority_and_validity(self):
        self.enroll("P3", healthy_assessment(
            diagnoses=[{"code": "diabetes", "hba1c_percent": 9.6}]))
        self.assertEqual(self.svc.grant_override(
            "P3", "x", "SPECIALIST", ["REL_DM_UNSTABLE"], "   ", "2026-10-10", TODAY).code,
            "BAD_INPUT")
        self.assertEqual(self.svc.grant_override(
            "P3", "x", "NURSE", ["REL_DM_UNSTABLE"], "理由", "2026-10-10", TODAY).code,
            "BAD_AUTHORITY")
        self.assertEqual(self.svc.grant_override(
            "P3", "x", "ATTENDING", ["REL_DM_UNSTABLE"], "理由", "2026-10-10", TODAY).code,
            "INSUFFICIENT_AUTHORITY")
        self.assertEqual(self.svc.grant_override(
            "P3", "x", "SPECIALIST", ["REL_DM_UNSTABLE"], "理由", "2025-01-01", TODAY).code,
            "BAD_INPUT")
        self.assertEqual(self.svc.grant_override(
            "P3", "x", "SPECIALIST", ["REL_DM_UNSTABLE"], "理由", "2026-12-31", TODAY).code,
            "BAD_INPUT")

    def test_override_for_rule_not_currently_hit_is_rejected(self):
        self.enroll()
        r = self.svc.grant_override(
            "P1", "dr-wang", "SPECIALIST", ["REL_DM_UNSTABLE"], "理由", "2026-10-10", TODAY)
        self.assertEqual(r.code, "RULE_NOT_APPLICABLE")

    def test_override_expires_and_prescription_auto_suspends(self):
        self.enroll("P5", healthy_assessment(
            diagnoses=[{"code": "diabetes", "hba1c_percent": 9.6}]))
        self.assertTrue(self.svc.grant_override(
            "P5", "dr-wang", "SPECIALIST", ["REL_DM_UNSTABLE"], "短期覆盖",
            "2026-10-05", TODAY).ok)
        self.assertEqual(self.svc.prescribe("P5", "dr-wang", TODAY).code, PRESCRIBABLE)
        # 覆盖到期后复评（病情依旧）-> 系统强制暂停
        r = self.svc.record_assessment(
            "P5", healthy_assessment(diagnoses=[{"code": "diabetes", "hba1c_percent": 9.6}]),
            "dr-li", date(2026, 10, 6))
        self.assertIsNotNone(r.data["auto_suspended"])
        versions = self.svc.history("P5")["prescription_versions"]
        self.assertEqual(versions[-1]["status"], "suspended")
        self.assertEqual(versions[-1]["created_by"], "SYSTEM")

    def test_override_is_scoped_to_named_rule_only(self):
        # 同时有两项相对禁忌，只覆盖一项不能开具
        self.enroll("P6", healthy_assessment(
            diagnoses=["stable_coronary_disease",
                       {"code": "diabetes", "hba1c_percent": 9.6}]))
        self.assertTrue(self.svc.grant_override(
            "P6", "dr-wang", "SPECIALIST", ["REL_DM_UNSTABLE"], "仅覆盖糖尿病项",
            "2026-10-14", TODAY).ok)
        r = self.svc.prescribe("P6", "dr-wang", TODAY)
        self.assertEqual(r.code, RELATIVE_BLOCKED)
        self.assertTrue(any(f["rule_code"] == "REL_CARDIAC_NO_CLEARANCE"
                            for f in r.data["findings"]))


class AdjustmentTest(ServiceTestCase):
    def _active_patient(self, patient="P1"):
        self.enroll(patient)
        self.assertTrue(self.svc.prescribe(patient, "dr-li", TODAY).ok)

    def test_adjustment_keeps_before_and_after_versions(self):
        self._active_patient()
        r = self.svc.adjust("P1", "dr-li", {"minutes": 15}, "首周保守起步", TODAY)
        self.assertTrue(r.ok)
        self.assertEqual(r.data["before_version"], "rx-1")
        self.assertEqual(r.data["after"]["version"], "rx-2")
        self.assertEqual(r.data["after"]["supersedes"], "rx-1")
        versions = self.svc.history("P1")["prescription_versions"]
        self.assertEqual([v["version"] for v in versions], ["rx-1", "rx-2"])
        self.assertEqual(versions[0]["dose"]["minutes"], 20)
        self.assertEqual(versions[1]["dose"]["minutes"], 15)

    def test_adjustment_requires_reason(self):
        self._active_patient()
        r = self.svc.adjust("P1", "dr-li", {"minutes": 21}, "  ", TODAY)
        self.assertEqual(r.code, "BAD_INPUT")

    def test_adjustment_cannot_exceed_safe_envelope(self):
        self._active_patient()
        r = self.svc.adjust("P1", "dr-li", {"pace_kmh_range": [8.0, 12.0]}, "想快跑", TODAY)
        self.assertEqual(r.code, "OUTSIDE_SAFE_ENVELOPE")

    def test_increase_blocked_after_unsafe_day(self):
        self._active_patient()
        day = "2026-10-02"
        events = day_events("P1", day, tail=lambda ev, start: [
            ev(start, "symptom_onset", "07:20", symptom="chest_pain")])
        for e in events:
            self.svc.ingest_event(e, ts=f"{day}T00:00:00+00:00")
        self.assertEqual(self.svc.replay_day("P1", day)["decision"]["decision"],
                         "EMERGENCY_ADVICE")
        self.svc.adjust("P1", "dr-li", {"minutes": 15}, "事后减量", date(2026, 10, 3))
        r = self.svc.adjust("P1", "dr-li", {"minutes": 16}, "想加回一分钟", date(2026, 10, 5))
        self.assertEqual(r.code, "PROGRESSION_BLOCKED")

    def test_progression_rate_limited_to_ten_percent(self):
        self._active_patient()
        # 20 -> 25 超过 +10%
        r = self.svc.adjust("P1", "dr-li", {"minutes": 25}, "加量过快", date(2026, 10, 9))
        self.assertEqual(r.code, "PROGRESSION_TOO_FAST")
        r2 = self.svc.adjust("P1", "dr-li", {"minutes": 22}, "按10%递增", date(2026, 10, 9))
        self.assertTrue(r2.ok)

    def test_progression_order_duration_then_frequency_then_pace(self):
        self._active_patient()
        r = self.svc.adjust(
            "P1", "dr-li", {"minutes": 22, "weekly_sessions": 4}, "两个维度一起加",
            date(2026, 10, 9))
        self.assertEqual(r.code, "PROGRESSION_ORDER")

    def test_suspend_requires_reason_and_creates_version(self):
        self._active_patient()
        r = self.svc.suspend("P1", "dr-li", "复查发现关节不适", TODAY)
        self.assertTrue(r.ok)
        self.assertEqual(self.svc.history("P1")["prescription_versions"][-1]["status"],
                         "suspended")


class ReplayAndLedgerTest(ServiceTestCase):
    def test_safe_day_replays_as_approved(self):
        self.enroll()
        self.assertTrue(self.svc.prescribe("P1", "dr-li", TODAY).ok)
        day = "2026-10-02"
        for e in day_events("P1", day):
            self.svc.ingest_event(e, ts=f"{day}T00:00:00+00:00")
        rep = self.svc.replay_day("P1", day)
        self.assertEqual(rep["decision"]["decision"], "APPROVE")
        self.assertEqual(rep["inputs"]["prescription_version"], "rx-1")
        self.assertEqual(rep["decision"]["rule_pack_id"], self.svc.pack_id)

    def test_replay_is_identical_after_reload(self):
        self.enroll()
        self.svc.prescribe("P1", "dr-li", TODAY)
        day = "2026-10-02"
        for e in day_events("P1", day):
            self.svc.ingest_event(e, ts=f"{day}T00:00:00+00:00")
        before = self.svc.replay_day("P1", day)
        reloaded = SafetyService(AppendOnlyStore(self.store.path))
        self.assertEqual(before, reloaded.replay_day("P1", day))

    def test_replay_uses_prescription_version_as_of_that_day(self):
        self.enroll()
        self.svc.prescribe("P1", "dr-li", TODAY)
        # 历史日期（处方建立之前）重放：无生效处方
        rep = self.svc.replay_day("P1", "2026-09-30")
        self.assertIsNone(rep["inputs"]["prescription_version"])

    def test_day_without_events_is_no_session(self):
        self.enroll()
        self.svc.prescribe("P1", "dr-li", TODAY)
        rep = self.svc.replay_day("P1", "2026-10-05")
        self.assertEqual(rep["decision"]["decision"], "NO_SESSION")

    def test_ledger_hash_chain_detects_tampering(self):
        self.enroll()
        path = self.store.path
        lines = path.read_text(encoding="utf-8").splitlines()
        import json
        obj = json.loads(lines[0])
        obj["payload"]["assessment"]["age"] = 99
        lines[0] = json.dumps(obj, ensure_ascii=False, sort_keys=True)
        tampered = Path(self.tmp.name) / "tampered.jsonl"
        tampered.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaises(LedgerError):
            AppendOnlyStore(tampered)
        self.assertEqual(self.store.verify()["hash_chain"], "ok")

    def test_unassignable_event_is_recorded_but_rejected(self):
        r = self.svc.ingest_event({"type": "hr_sample", "hr_bpm": 80})
        self.assertFalse(r.ok)
        self.assertTrue(self.store.read("UNASSIGNABLE"))

    def test_rejected_event_on_safe_day_still_fails_closed(self):
        self.enroll()
        self.svc.prescribe("P1", "dr-li", TODAY)
        day = "2026-10-02"
        for e in day_events("P1", day):
            self.assertTrue(self.svc.ingest_event(e, ts=f"{day}T00:00:00+00:00").ok)
        # 追加一条时间戳无法解析的"当日设备数据"——绝不能当作安全
        bad = {"event_id": "P1-bad", "patient_ref": "P1", "ts": "not-a-date",
               "seq": 5, "type": "hr_sample", "hr_bpm": 90}
        rejected = self.svc.ingest_event(bad, ts=f"{day}T08:00:00+00:00")
        self.assertEqual(rejected.code, "REJECTED_EVENT")
        rep = self.svc.replay_day("P1", day)
        self.assertEqual(rep["inputs"]["rejected_event_count"], 1)
        self.assertEqual(rep["decision"]["decision"], "WITHHOLD")
        self.assertTrue(rep["decision"]["review_required"])

    def test_malformed_event_is_kept_and_flags_day(self):
        self.enroll()
        self.svc.prescribe("P1", "dr-li", TODAY)
        day = "2026-10-02"
        for e in day_events("P1", day):
            self.svc.ingest_event(e, ts=f"{day}T00:00:00+00:00")
        r = self.svc.ingest_event(
            {"event_id": "P1-x", "patient_ref": "P1", "ts": f"{day}T08:00:00",
             "seq": 5, "type": "hr_sample", "hr_bpm": -3},
            ts=f"{day}T08:00:01+00:00")
        self.assertFalse(r.ok)
        rep = self.svc.replay_day("P1", day)
        self.assertNotEqual(rep["decision"]["decision"], "APPROVE")
        self.assertGreaterEqual(rep["inputs"]["rejected_event_count"], 1)


if __name__ == "__main__":
    unittest.main()
