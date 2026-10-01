"""版本化禁忌规则包与剂量计算测试。"""

import unittest
from datetime import date

from prescription.rules import (
    ABSOLUTE,
    ADAPTIVE,
    DEFAULT_PACK_ID,
    RELATIVE,
    compute_dose,
    get_pack,
    relative_unresolved,
    screen,
)

TODAY = date(2026, 10, 1)


def base_assessment(**over):
    a = {
        "age": 52,
        "diagnoses": [],
        "recent_metrics": {
            "systolic_mmhg": 124,
            "diastolic_mmhg": 78,
            "resting_hr_bpm": 68,
            "measured_on": "2026-09-28",
        },
        "medications": [],
        "activity_baseline": {"level": "light"},
        "permission_opinion": [],
    }
    a.update(over)
    return a


class ScreeningTest(unittest.TestCase):
    def test_clean_assessment_has_no_contraindications(self):
        s = screen(base_assessment(), today=TODAY)
        self.assertEqual(s.absolute, ())
        self.assertEqual(s.relative, ())
        self.assertFalse(s.stale_metrics)
        self.assertEqual(s.missing, ())

    def test_absolute_contraindications_are_system_blocks(self):
        cases = {
            "unstable_angina": "ABS_CV_UNSTABLE",
            "acute_coronary_syndrome": "ABS_CV_UNSTABLE",
            "decompensated_heart_failure": "ABS_HF_DECOMP",
            "acute_pe_or_dvt": "ABS_ACUTE_VASCULAR",
            "acute_severe_joint_injury": "ABS_JOINT_ACUTE",
            "acute_exercise_injury": "ABS_JOINT_ACUTE",
        }
        for dx, code in cases.items():
            with self.subTest(diagnosis=dx):
                s = screen(base_assessment(diagnoses=[dx]), today=TODAY)
                self.assertIn(code, [f.rule_code for f in s.absolute])

    def test_severe_hypertension_is_absolute(self):
        s = screen(base_assessment(
            recent_metrics={**base_assessment()["recent_metrics"],
                            "systolic_mmhg": 182, "diastolic_mmhg": 95}),
            today=TODAY)
        self.assertTrue(any(f.rule_code == "ABS_BP_SEVERE" for f in s.absolute))

    def test_elevated_bp_is_relative(self):
        s = screen(base_assessment(
            recent_metrics={**base_assessment()["recent_metrics"],
                            "systolic_mmhg": 165, "diastolic_mmhg": 95}),
            today=TODAY)
        self.assertTrue(any(f.rule_code == "REL_BP_ELEVATED" for f in s.relative))

    def test_stable_cardiac_disease_needs_cardiology_clearance(self):
        s = screen(base_assessment(diagnoses=["stable_coronary_disease"]), today=TODAY)
        blocked = [f for f in relative_unresolved(s, base_assessment(diagnoses=["stable_coronary_disease"]))]
        self.assertTrue(any(f.rule_code == "REL_CARDIAC_NO_CLEARANCE" for f in blocked))
        cleared = base_assessment(
            diagnoses=["stable_coronary_disease"],
            permission_opinion=[{"specialty": "cardiology", "decision": "approved"}])
        s2 = screen(cleared, today=TODAY)
        self.assertFalse(
            [f for f in relative_unresolved(s2, cleared) if f.rule_code == "REL_CARDIAC_NO_CLEARANCE"])

    def test_unstable_diabetes_is_relative(self):
        s = screen(base_assessment(diagnoses=[{"code": "diabetes", "hba1c_percent": 9.6}]), today=TODAY)
        self.assertTrue(any(f.rule_code == "REL_DM_UNSTABLE" for f in s.relative))

    def test_metrics_older_than_pack_window_are_stale(self):
        old = base_assessment(
            recent_metrics={**base_assessment()["recent_metrics"], "measured_on": "2026-09-01"})
        self.assertTrue(screen(old, today=TODAY).stale_metrics)

    def test_missing_fields_are_listed(self):
        s = screen({}, today=TODAY)
        self.assertIn("diagnoses", s.missing)
        self.assertIn("recent_metrics.systolic_mmhg", s.missing)
        self.assertIn("age", s.missing)

    def test_sedentary_older_adult_is_adaptive(self):
        a = base_assessment(age=70, activity_baseline={"level": "sedentary"})
        s = screen(a, today=TODAY)
        codes = {f.rule_code for f in s.adaptive}
        self.assertIn("ADAPT_OLDER", codes)
        self.assertIn("ADAPT_SEDENTARY", codes)

    def test_beta_blocker_switches_hr_guidance(self):
        a = base_assessment(medications=[{"name": "x", "category": "beta_blocker"}])
        s = screen(a, today=TODAY)
        self.assertTrue(any(f.rule_code == "ADAPT_BETA_BLOCKER" for f in s.adaptive))


class DoseTest(unittest.TestCase):
    def test_standard_dose_for_low_risk_person(self):
        a = base_assessment()
        dose = compute_dose(a, screen(a, today=TODAY))
        self.assertEqual(dose["pace_kmh_range"], [4.0, 6.0])
        self.assertEqual(dose["hr_zone"]["method"], "hrr")
        self.assertEqual(dose["minutes"], 20)
        self.assertEqual(dose["weekly_sessions"], 3)
        self.assertFalse(dose["downgraded"])

    def test_adaptive_dose_is_conservative(self):
        a = base_assessment(age=70, activity_baseline={"level": "sedentary"})
        dose = compute_dose(a, screen(a, today=TODAY))
        self.assertTrue(dose["downgraded"])
        self.assertEqual(dose["pace_kmh_range"], [3.0, 4.5])
        self.assertLessEqual(dose["minutes"], 15)
        self.assertGreaterEqual(dose["warmup_min"], 10)
        self.assertEqual(dose["progression_pct_week"], 5)

    def test_overridden_relative_still_downgrades_and_flags_observation(self):
        a = base_assessment(diagnoses=[{"code": "diabetes", "hba1c_percent": 9.6}])
        s = screen(a, today=TODAY)
        dose = compute_dose(a, s, frozenset({"REL_DM_UNSTABLE"}))
        self.assertTrue(dose["downgraded"])
        self.assertTrue(any("覆盖" in c for c in dose["cautions"]))

    def test_rule_pack_is_immutable_and_versioned(self):
        pack = get_pack(DEFAULT_PACK_ID)
        with self.assertRaises(Exception):
            pack.version = "9.9.9"  # type: ignore[misc]
        self.assertEqual(DEFAULT_PACK_ID, f"{pack.pack_id}@{pack.version}")

    def test_levels_are_exhaustive(self):
        pack = get_pack(DEFAULT_PACK_ID)
        for rule in pack.rules:
            self.assertIn(rule.level, (ABSOLUTE, RELATIVE, ADAPTIVE))


if __name__ == "__main__":
    unittest.main()
