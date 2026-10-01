"""监测折叠：偏离、暂停、急症、缺口、乱序、覆盖到期。"""

import unittest
from pathlib import Path

from app import monitor
from app.registry import RuleRegistry

ROOT = Path(__file__).resolve().parent.parent
TZ = "+08:00"


def ev(eid, etype, t, payload=None, late=False):
    from app import timeutil
    e = {
        "event_id": eid,
        "case_id": "c",
        "event_type": etype,
        "event_time": timeutil.parse(f"2026-10-02T{t}{TZ}"),
        "ingested_at": timeutil.parse(f"2026-10-02T{t}{TZ}"),
        "actor_ref": "x",
        "payload": payload or {},
        "late": late,
    }
    return e


def decision(eid="d0", t="06:00:00", plan=None, versions=None):
    plan = plan or {
        "baseline": "sedentary",
        "pace_kmh": [4.0, 5.0],
        "minutes": [10, 20],
        "weekly_sessions": [3, 3],
        "hr_zone": {"method": "hrr", "bpm": [100, 120], "rpe": [11, 13]},
    }
    return ev(eid, "prescription_decided", t, {
        "outcome": "eligible",
        "plan_snapshot": {"plan": plan, "rules_versions": versions or {"monitoring": "1.0.0"}},
    })


class MonitorFoldTest(unittest.TestCase):
    def setUp(self):
        self.registry = RuleRegistry(ROOT / "rules")
        self.rules = self.registry.latest("monitoring")

    def fold(self, events, as_of=None):
        events = sorted(events, key=lambda e: (e["event_time"], e["event_id"]))
        return monitor.fold(events, self.registry, self.rules, as_of=as_of)

    def test_no_plan_is_not_evaluated(self):
        st = self.fold([ev("s", "symptom_reported", "07:00:00", {"code": "mild_soreness"})])
        self.assertEqual(monitor.day_state(st), "not_evaluated")

    def test_allowed_day(self):
        st = self.fold([decision(),
                        ev("s1", "session_started", "07:00:00"),
                        ev("h1", "device_reading", "07:03:00", {"metric": "heart_rate_bpm", "value": 110}),
                        ev("h2", "device_reading", "07:08:00", {"metric": "heart_rate_bpm", "value": 111}),
                        ev("h3", "device_reading", "07:13:00", {"metric": "heart_rate_bpm", "value": 109}),
                        ev("h4", "device_reading", "07:17:00", {"metric": "heart_rate_bpm", "value": 112}),
                        ev("e1", "session_ended", "07:18:00", {"duration_minutes": 15})])
        self.assertEqual(monitor.day_state(st), "allowed")

    def test_first_hr_excursion_caution_then_consecutive_suspends(self):
        events = [decision(), ev("s1", "session_started", "07:00:00"),
                  ev("h1", "device_reading", "07:02:00", {"metric": "heart_rate_bpm", "value": 130})]
        st = self.fold(events)
        self.assertEqual(monitor.day_state(st), "allowed")  # 单次超标仅 caution
        events.append(ev("h2", "device_reading", "07:04:00", {"metric": "heart_rate_bpm", "value": 131}))
        st = self.fold(events)
        self.assertEqual(monitor.day_state(st), "suspended")

    def test_emergency_symptom_gives_care_guidance_not_diagnosis(self):
        st = self.fold([decision(),
                        ev("p1", "symptom_reported", "07:05:00", {"code": "chest_pain"})])
        self.assertEqual(monitor.day_state(st), "escalated")
        f = [f for f in st.findings if f["severity"] == "emergency"][0]
        self.assertEqual(f["code"], "EMERGENCY_SYMPTOM")
        self.assertIn("就医", f["guidance"])
        self.assertNotIn("诊断", f["guidance"])  # 只提示就医，不自动诊断
        for banned in ("心梗", "心肌梗死", "冠心病"):
            self.assertNotIn(banned, f["guidance"])

    def test_reported_severity_escalates(self):
        # 锐痛 mild 本身 caution；患者自报 severe -> 至少 suspend。
        st = self.fold([decision(), ev("p", "symptom_reported", "07:00:00",
                                       {"code": "sharp_muscle_pain", "severity": "severe"})])
        self.assertEqual(monitor.day_state(st), "suspended")

    def test_unknown_symptom_code_is_safe_suspend_and_review(self):
        st = self.fold([decision(), ev("p", "symptom_reported", "07:00:00",
                                       {"code": "weird_tail_feeling"})])
        f = st.findings[0]
        self.assertEqual(f["code"], "UNKNOWN_SIGNAL")
        self.assertEqual(monitor.day_state(st), "suspended")

    def test_data_gap_during_session_suspends(self):
        st = self.fold([
            decision(),
            ev("s1", "session_started", "07:00:00"),
            ev("h1", "device_reading", "07:02:00", {"metric": "heart_rate_bpm", "value": 110}),
            ev("h2", "device_reading", "07:12:00", {"metric": "heart_rate_bpm", "value": 111}),
        ])
        gap = [f for f in st.findings if f["code"] == "DATA_GAP"]
        self.assertTrue(gap)
        self.assertEqual(monitor.day_state(st), "suspended")
        self.assertEqual(gap[0]["at"].isoformat(), "2026-10-02T07:07:00+08:00")

    def test_silence_after_session_start_is_gap(self):
        st = self.fold([
            decision(),
            ev("s1", "session_started", "07:00:00"),
        ], as_of=__import__("app").timeutil.parse("2026-10-02T07:10:00+08:00"))
        self.assertTrue(any(f["code"] == "DATA_GAP" for f in st.findings))
        self.assertEqual(monitor.day_state(st), "suspended")

    def test_questionable_reading_counts_as_present_but_not_zone(self):
        st = self.fold([
            decision(),
            ev("s1", "session_started", "07:00:00"),
            ev("h1", "device_reading", "07:04:00",
               {"metric": "heart_rate_bpm", "value": 999, "quality": "questionable"}),
        ])
        # 可疑值不触发区间偏离；4 分钟尚在 300s 缺口阈值内。
        self.assertFalse(any(f["code"].startswith("HR_") for f in st.findings))
        self.assertEqual(monitor.day_state(st), "allowed")

    def test_orphan_end_is_data_gap(self):
        st = self.fold([decision(), ev("e", "session_ended", "07:20:00")])
        self.assertTrue(any(f["code"] == "DATA_GAP" for f in st.findings))
        self.assertEqual(monitor.day_state(st), "suspended")

    def test_late_reading_is_flagged_but_history_fold_is_deterministic(self):
        events = [
            decision(),
            ev("s1", "session_started", "07:00:00"),
            ev("h1", "device_reading", "07:03:00", {"metric": "heart_rate_bpm", "value": 110}),
        ]
        st1 = self.fold(events)
        events.append(ev("h0", "device_reading", "07:01:00",
                         {"metric": "heart_rate_bpm", "value": 112}, late=True))
        st2 = self.fold(events)
        self.assertTrue(any(f["code"] == "LATE_DATA" for f in st2.findings))
        # 晚到的正常数据不改写安全结论。
        self.assertEqual(monitor.day_state(st1), monitor.day_state(st2))

    def test_override_expiry_fails_safe(self):
        events = [
            decision(t="06:00:00"),
            ev("ov", "override_granted", "06:30:00", {
                "rule_code": "CI_ACUTE_JOINT_INJURY",
                "reason": "限期监督", "authority": "cardiologist",
                "valid_from": "2026-10-02T06:30:00+08:00",
                "valid_until": "2026-10-03T06:30:00+08:00",
            }),
        ]
        # 决策并未依赖该覆盖（decided 早于覆盖），used_override_codes 为空：
        # 不应凭空产生到期发现。
        st = self.fold(events, as_of=__import__("app").timeutil.parse("2026-10-04T00:00:00+08:00"))
        self.assertFalse(any(f["code"] == "OVERRIDE_EXPIRED" for f in st.findings))

    def test_review_resolve_does_not_auto_resume(self):
        events = [
            decision(),
            ev("p", "symptom_reported", "07:05:00", {"code": "joint_pain"}),
            ev("r", "review_resolved", "08:00:00",
               {"finding_event_ids": ["fnd:p:1"], "resolution": "keep_suspended",
                "note": "建议观察"}),
        ]
        st = self.fold(events)
        # 复核关闭了发现，但没有显式 resume，仍不得放行。
        self.assertEqual(monitor.day_state(st), "suspended")
        events.append(ev("rs", "prescription_resumed", "09:00:00", {"note": "复诊后恢复"}))
        st2 = self.fold(events)
        self.assertEqual(monitor.day_state(st2), "allowed")

    def test_pause_then_new_eligible_decision_resumes(self):
        st = self.fold([
            decision(),
            ev("p", "prescription_paused", "07:10:00", {"trigger_codes": ["X"]}),
            decision("d1", "08:00:00"),
        ])
        self.assertFalse(st.paused)
        self.assertEqual(monitor.day_state(st), "allowed")

    def test_beta_blocker_plan_hr_not_judged(self):
        plan = {"baseline": "sedentary", "pace_kmh": [4, 5], "minutes": [10, 20],
                "weekly_sessions": [3, 3], "hr_zone": None}
        st = self.fold([
            decision(plan=plan),
            ev("s1", "session_started", "07:00:00"),
            ev("h1", "device_reading", "07:02:00", {"metric": "heart_rate_bpm", "value": 150}),
            ev("h2", "device_reading", "07:04:00", {"metric": "heart_rate_bpm", "value": 151}),
        ])
        self.assertFalse(any(f["code"].startswith("HR_") for f in st.findings))

    def test_reported_moderate_turns_info_symptom_into_review(self):
        st = self.fold([decision(), ev("p", "symptom_reported", "07:00:00",
                                       {"code": "mild_soreness", "severity": "moderate"})])
        self.assertTrue(any(
            f["code"] == "MANUAL_REVIEW_REQUIRED" and f["severity"] == "caution"
            for f in st.findings))

    def test_late_session_end_after_pause_does_not_add_orphan_gap(self):
        st = self.fold([
            decision(),
            ev("s1", "session_started", "07:00:00"),
            ev("h1", "device_reading", "07:02:00", {"metric": "heart_rate_bpm", "value": 110}),
            ev("p", "prescription_paused", "07:05:00", {"trigger_codes": ["X"]}),
            ev("e", "session_ended", "07:30:00", {"duration_minutes": 30}),
        ])
        self.assertFalse(any(f["finding_id"].endswith(":orphan") for f in st.findings))


if __name__ == "__main__":
    unittest.main()
