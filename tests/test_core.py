"""条件求值器与规则包的失败安全单测。"""

import json
import unittest
from pathlib import Path

from app.conditions import evaluate
from app.registry import RuleRegistry
from app.eligibility import evaluate_eligibility
from app.dosing import build_plan

ROOT = Path(__file__).resolve().parent.parent


class ConditionsTest(unittest.TestCase):
    def test_comparisons(self):
        ctx = {"a": {"b": 10}, "xs": ["x", "y"]}
        self.assertTrue(evaluate({"op": ">=", "path": "a.b", "value": 10}, ctx))
        self.assertFalse(evaluate({"op": ">", "path": "a.b", "value": 10}, ctx))
        self.assertTrue(evaluate({"op": "contains", "path": "xs", "value": "y"}, ctx))
        self.assertTrue(evaluate({"op": "contains_any", "path": "xs", "value": ["q", "x"]}, ctx))

    def test_missing_is_false_for_comparisons(self):
        # 缺失数据绝不参与肯定判断。
        self.assertFalse(evaluate({"op": ">=", "path": "a.b", "value": 1}, {}))
        self.assertFalse(evaluate({"op": "==", "path": "a.b", "value": None}, {}))
        self.assertFalse(evaluate({"op": "contains", "path": "xs", "value": "x"}, {}))

    def test_missing_op_detects_absence_and_null(self):
        self.assertTrue(evaluate({"op": "missing", "path": "a.b"}, {}))
        self.assertTrue(evaluate({"op": "missing", "path": "a.b"}, {"a": {"b": None}}))
        self.assertFalse(evaluate({"op": "missing", "path": "a.b"}, {"a": {"b": 0}}))

    def test_and_or_not_and_shorthand(self):
        ctx = {"v": 5}
        self.assertTrue(evaluate({"all": [{"op": ">", "path": "v", "value": 4}]}, ctx))
        self.assertTrue(evaluate({"any": [{"op": ">", "path": "v", "value": 9},
                                          {"op": "==", "path": "v", "value": 5}]}, ctx))
        self.assertFalse(evaluate({"op": "not", "node": {"op": "==", "path": "v", "value": 5}}, ctx))


class EligibilityTest(unittest.TestCase):
    def setUp(self):
        self.r = RuleRegistry(ROOT / "rules")
        self.ci = self.r.latest("contraindications")
        self.do = self.r.latest("dosing")

    def base(self, **over):
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

    def test_clean_case_eligible(self):
        res = evaluate_eligibility(self.base(), self.ci)
        self.assertEqual(res["outcome"], "eligible")

    def test_acute_joint_and_cardiopulmonary_are_absolute(self):
        res = evaluate_eligibility(self.base(joint_injury=True), self.ci)
        self.assertEqual(res["outcome"], "contraindicated")
        self.assertIn("CI_ACUTE_JOINT_INJURY", res["absolute_hits"])
        res = evaluate_eligibility(self.base(diagnoses=["acute_mi"]), self.ci)
        self.assertIn("CI_ACUTE_CARDIOPULMONARY", res["absolute_hits"])

    def test_uncontrolled_bp_thresholds(self):
        res = evaluate_eligibility(
            self.base(recent_metrics={**self.base()["recent_metrics"], "resting_sbp_mmhg": 160}), self.ci)
        self.assertEqual(res["outcome"], "contraindicated")
        self.assertIn("CI_UNCONTROLLED_HYPERTENSION", res["absolute_hits"])

    def test_glucose_instability_both_directions(self):
        hi = evaluate_eligibility(
            self.base(recent_metrics={**self.base()["recent_metrics"], "fasting_glucose_mmol_l": 17}), self.ci)
        self.assertIn("CI_UNSTABLE_GLUCOSE", hi["absolute_hits"])
        lo = evaluate_eligibility(
            self.base(recent_metrics={**self.base()["recent_metrics"], "fasting_glucose_mmol_l": 3.5}), self.ci)
        self.assertIn("CI_UNSTABLE_GLUCOSE", lo["absolute_hits"])

    def test_missing_permission_and_metrics_need_review(self):
        a = self.base(clinical_permission="not_recorded")
        res = evaluate_eligibility(a, self.ci)
        self.assertEqual(res["outcome"], "needs_review")
        self.assertIn("REVIEW_MISSING_PERMISSION", res["review_hits"])
        a2 = self.base()
        del a2["age"]
        self.assertIn("REVIEW_MISSING_AGE", evaluate_eligibility(a2, self.ci)["review_hits"])

    def test_diabetes_without_glucose_metrics_needs_review(self):
        a = self.base(diagnoses=["diabetes"])
        a["recent_metrics"].pop("fasting_glucose_mmol_l")
        a["recent_metrics"].pop("hba1c_pct", None)
        res = evaluate_eligibility(a, self.ci)
        self.assertEqual(res["outcome"], "needs_review")
        self.assertIn("REVIEW_MISSING_GLUCOSE", res["review_hits"])

    def test_relative_ci_adds_modifier_only(self):
        res = evaluate_eligibility(self.base(recent_musculoskeletal=True), self.ci)
        self.assertEqual(res["outcome"], "eligible")
        self.assertIn("deconditioned_start", res["dose_modifiers"])

    def test_override_within_validity_lifts_rule(self):
        a = self.base(joint_injury=True)
        overrides = {"CI_ACUTE_JOINT_INJURY": {
            "reason": "已由骨科与心内科联合会诊确认可监督下活动",
            "authority": "attending_physician",
            "valid_until": "2026-10-08T00:00:00+08:00",
        }}
        res = evaluate_eligibility(a, self.ci, overrides)
        self.assertEqual(res["outcome"], "eligible")
        self.assertIn("CI_ACUTE_JOINT_INJURY", res["overridden_codes"])
        self.assertTrue(res["requires_physician_supervision"])

    def test_plan_dosing_and_beta_blocker(self):
        plan = build_plan(self.base(), self.do)
        self.assertEqual(plan["pace_kmh"], [4.0, 5.0])
        self.assertEqual(plan["hr_zone"]["bpm"], [108, 126])  # 70 + f*(220-55-70=95)
        bb = build_plan(self.base(medications=["beta_blocker"]), self.do)
        self.assertIsNone(bb["hr_zone"])
        self.assertIn("RPE", bb["intensity_method_note"])

    def test_deconditioned_plan_is_deloaded(self):
        plan = build_plan(self.base(recent_musculoskeletal=True), self.do, ["deconditioned_start"])
        self.assertEqual(plan["pace_kmh"], [3.5, 4.5])
        self.assertEqual(plan["minutes"], [8, 12])

    def test_missing_baseline_blocks_plan(self):
        a = self.base()
        a["activity_baseline"] = "unknown"
        with self.assertRaises(ValueError):
            build_plan(a, self.do)

    def test_rule_files_parse_and_self_identify(self):
        for ruleset in ("contraindications", "dosing", "monitoring"):
            for p in (ROOT / "rules" / ruleset).glob("*.json"):
                data = json.loads(p.read_text(encoding="utf-8"))
                self.assertEqual(data["ruleset"], ruleset)
                self.assertEqual(data["rules_version"], p.stem)


if __name__ == "__main__":
    unittest.main()
