"""日决策引擎测试：获准、暂停、升级、紧急就医提示与数据故障关闭。"""

import unittest

from prescription.engine import (
    APPROVE,
    EMERGENCY_ADVICE,
    ESCALATE,
    NO_SESSION,
    PAUSE,
    WITHHOLD,
    evaluate_day,
)

P = "PSEUDO-001"
D = "2026-10-02"

RX = {
    "version": "rx-1",
    "status": "active",
    "rule_pack_id": "slow-jogging-safety@1.0.0",
    "dose": {
        "hr_zone": {"method": "hrr", "bpm_range": [100, 120]},
        "pace_kmh_range": [4.0, 6.0],
    },
}


def ev(seq, etype, clock, **data):
    base = {"event_id": f"e{seq}", "patient_ref": P, "ts": f"{D}T{clock}", "seq": seq,
            "type": etype}
    base.update(data)
    return base


def startup(seq_start=1):
    return [
        ev(seq_start, "symptom_check", "07:00", symptoms=["none"]),
        ev(seq_start + 1, "vitals_reading", "07:05",
           systolic_mmhg=128, diastolic_mmhg=80, heart_rate_bpm=72, glucose_mmol_l=6.1),
    ]


def safe_session(seq_start=1, minutes=25):
    return [
        *startup(seq_start),
        ev(seq_start + 2, "session_start", "07:10"),
        ev(seq_start + 3, "session_end", "07:40", duration_min=minutes),
    ]


class HappyPathTest(unittest.TestCase):
    def test_clean_day_is_approved(self):
        dec = evaluate_day(P, D, RX, safe_session())
        self.assertEqual(dec.final, APPROVE)
        self.assertTrue(dec.approved)
        self.assertEqual(dec.duration_min, 25)

    def test_startup_only_day_is_no_session_but_gate_passed(self):
        dec = evaluate_day(P, D, RX, startup())
        self.assertEqual(dec.final, NO_SESSION)
        self.assertTrue(dec.approved)

    def test_empty_day_is_no_session(self):
        dec = evaluate_day(P, D, RX, [])
        self.assertEqual(dec.final, NO_SESSION)
        self.assertFalse(dec.approved)

    def test_replay_is_deterministic(self):
        events = safe_session()
        self.assertEqual(evaluate_day(P, D, RX, events).to_dict(),
                         evaluate_day(P, D, RX, events).to_dict())


class EmergencyTest(unittest.TestCase):
    def test_pre_exercise_chest_pain_is_emergency_advice_only(self):
        events = [ev(1, "symptom_check", "07:00", symptoms=["chest_pain"])]
        dec = evaluate_day(P, D, RX, events)
        self.assertEqual(dec.final, EMERGENCY_ADVICE)
        self.assertIsNotNone(dec.emergency_advice)
        self.assertIn("120", dec.emergency_advice)
        # 只给就医提示，不给诊断
        self.assertNotIn("梗", dec.emergency_advice)
        self.assertNotIn("诊断", dec.emergency_advice)

    def test_in_exercise_syncope_pauses_and_advises(self):
        events = [
            *startup(),
            ev(3, "session_start", "07:10"),
            ev(4, "symptom_onset", "07:20", symptom="syncope"),
        ]
        dec = evaluate_day(P, D, RX, events)
        self.assertEqual(dec.final, EMERGENCY_ADVICE)
        self.assertTrue(dec.pause_reasons)
        self.assertEqual(dec.emergency_symptoms, ["syncope"])

    def test_emergency_works_even_without_prescription(self):
        dec = evaluate_day(P, D, None, [ev(1, "symptom_check", "07:00", symptoms=["chest_pain"])])
        self.assertEqual(dec.final, EMERGENCY_ADVICE)
        self.assertIsNotNone(dec.emergency_advice)


class PauseAndGateTest(unittest.TestCase):
    def test_joint_pain_before_exercise_pauses_and_flags_review(self):
        dec = evaluate_day(P, D, RX,
                           [ev(1, "symptom_check", "07:00", symptoms=["severe_joint_pain"])])
        self.assertEqual(dec.final, PAUSE)
        self.assertTrue(dec.review_required)

    def test_bp_gate_failure_then_start_pauses(self):
        events = [
            ev(1, "symptom_check", "07:00", symptoms=["none"]),
            ev(2, "vitals_reading", "07:05",
               systolic_mmhg=165, diastolic_mmhg=102, heart_rate_bpm=88, glucose_mmol_l=6.1),
            ev(3, "session_start", "07:10"),
        ]
        self.assertEqual(evaluate_day(P, D, RX, events).final, PAUSE)

    def test_glucose_hard_gate(self):
        events = [
            ev(1, "symptom_check", "07:00", symptoms=["none"]),
            ev(2, "vitals_reading", "07:05",
               systolic_mmhg=128, diastolic_mmhg=80, heart_rate_bpm=72, glucose_mmol_l=17.5),
            ev(3, "session_start", "07:10"),
        ]
        self.assertEqual(evaluate_day(P, D, RX, events).final, PAUSE)

    def test_starting_without_startup_events_pauses(self):
        dec = evaluate_day(P, D, RX, [ev(1, "session_start", "07:10")])
        self.assertEqual(dec.final, PAUSE)
        self.assertTrue(any("未完成前置检查" in r for r in dec.withhold_reasons))

    def test_start_without_active_prescription_pauses(self):
        dec = evaluate_day(P, D, None, safe_session())
        self.assertEqual(dec.final, PAUSE)

    def test_three_consecutive_hr_over_limit_pauses(self):
        events = [
            *startup(),
            ev(3, "session_start", "07:10"),
            ev(4, "hr_sample", "07:15", hr_bpm=125),
            ev(5, "hr_sample", "07:16", hr_bpm=126),
            ev(6, "hr_sample", "07:17", hr_bpm=127),
            ev(7, "session_end", "07:40", duration_min=25),
        ]
        dec = evaluate_day(P, D, RX, events)
        self.assertEqual(dec.final, PAUSE)
        self.assertEqual(len(dec.deviations), 3)

    def test_pause_request_pauses(self):
        events = [*startup(), ev(3, "pause_request", "07:11", reason="设备低电")]
        self.assertEqual(evaluate_day(P, D, RX, events).final, PAUSE)

    def test_session_start_without_end_is_withheld(self):
        events = [*startup(), ev(3, "session_start", "07:10"),
                  ev(4, "hr_sample", "07:15", hr_bpm=110)]
        self.assertEqual(evaluate_day(P, D, RX, events).final, WITHHOLD)

    def test_abnormal_vitals_arriving_between_gate_and_start_is_fail_closed(self):
        # 症状正常、第一次体征正常（闸门曾可过），第二次体征异常后才开跑 -> 必须 PAUSE
        events = [
            ev(1, "symptom_check", "07:00", symptoms=["none"]),
            ev(2, "vitals_reading", "07:05",
               systolic_mmhg=128, diastolic_mmhg=80, heart_rate_bpm=72, glucose_mmol_l=6.1),
            ev(3, "vitals_reading", "07:08",
               systolic_mmhg=170, diastolic_mmhg=105, heart_rate_bpm=90, glucose_mmol_l=6.1),
            ev(4, "session_start", "07:10"),
        ]
        dec = evaluate_day(P, D, RX, events)
        self.assertEqual(dec.final, PAUSE)
        self.assertFalse(dec.approved)


class DeviationAndReviewTest(unittest.TestCase):
    def test_single_hr_excursion_is_recorded_but_day_approved(self):
        events = [
            *startup(),
            ev(3, "session_start", "07:10"),
            ev(4, "hr_sample", "07:15", hr_bpm=125),
            ev(5, "session_end", "07:40", duration_min=25),
        ]
        dec = evaluate_day(P, D, RX, events)
        self.assertEqual(dec.final, APPROVE)
        self.assertEqual(len(dec.deviations), 1)

    def test_two_deviations_escalate_to_manual_review(self):
        events = [
            *startup(),
            ev(3, "session_start", "07:10"),
            ev(4, "hr_sample", "07:15", hr_bpm=125),
            ev(5, "pace_sample", "07:16", pace_kmh=9.0),
            ev(6, "session_end", "07:40", duration_min=25),
        ]
        dec = evaluate_day(P, D, RX, events)
        self.assertEqual(dec.final, ESCALATE)
        self.assertTrue(dec.review_required)


class DataQualityFailClosedTest(unittest.TestCase):
    def test_missing_vitals_at_end_of_day_is_withheld(self):
        dec = evaluate_day(P, D, RX, [ev(1, "symptom_check", "07:00", symptoms=["none"])])
        self.assertEqual(dec.final, WITHHOLD)
        self.assertTrue(dec.review_required)

    def test_out_of_order_day_is_withheld_or_paused_never_approved(self):
        events = [
            ev(1, "symptom_check", "07:00", symptoms=["none"]),
            ev(3, "vitals_reading", "07:05",
               systolic_mmhg=128, diastolic_mmhg=80, heart_rate_bpm=72, glucose_mmol_l=6.1),
            ev(2, "vitals_reading", "07:04",
               systolic_mmhg=120, diastolic_mmhg=80, heart_rate_bpm=70, glucose_mmol_l=6.0),
            ev(4, "session_start", "07:10"),
            ev(5, "session_end", "07:40", duration_min=25),
        ]
        dec = evaluate_day(P, D, RX, events)
        self.assertIn(dec.final, (WITHHOLD, PAUSE))
        codes = {f["code"] for f in dec.quality_flags}
        self.assertIn("OUT_OF_ORDER", codes)
        self.assertIn("GAP", codes)

    def test_unknown_event_type_withholds(self):
        dec = evaluate_day(P, D, RX, [*safe_session(),
                                      ev(6, "mystery", "07:41", foo=1)])
        self.assertEqual(dec.final, WITHHOLD)

    def test_malformed_value_withholds(self):
        events = [
            *startup(),
            ev(3, "session_start", "07:10"),
            ev(4, "hr_sample", "07:15", hr_bpm=-1),
            ev(5, "session_end", "07:40", duration_min=25),
        ]
        dec = evaluate_day(P, D, RX, events)
        self.assertEqual(dec.final, WITHHOLD)

    def test_trace_covers_every_event(self):
        events = safe_session()
        dec = evaluate_day(P, D, RX, events)
        self.assertEqual(len(dec.trace), len(events))
        for entry in dec.trace:
            self.assertIn("decision", entry)


if __name__ == "__main__":
    unittest.main()
