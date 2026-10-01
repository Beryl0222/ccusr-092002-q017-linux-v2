"""处方剂量生成：配速、心率/RPE 区间、时长、频次、热身放松、递增安排。"""

from typing import Optional

BASELINES = ("sedentary", "lightly_active", "regularly_active")


def _row(table: list, baseline: str) -> dict:
    for row in table:
        if row["baseline"] == baseline:
            return row
    raise ValueError(f"未知活动基础: {baseline}")


def _uses_hr_suppressing_meds(assessment: dict, dosing_rules: dict) -> bool:
    meds = set(assessment.get("medications") or [])
    return bool(meds & set(dosing_rules["intensity"].get("hr_suppressing_medications", [])))


def hr_zone(assessment: dict, dosing_rules: dict) -> Optional[dict]:
    """按 HRR 公式计算数值心率区间；信息不足或使用抑心率药物时返回 None。"""
    intensity = dosing_rules["intensity"]
    if _uses_hr_suppressing_meds(assessment, dosing_rules):
        return None
    metrics = assessment.get("recent_metrics") or {}
    age = assessment.get("age")
    resting_hr = metrics.get("resting_hr_bpm")
    if not isinstance(age, (int, float)) or not isinstance(resting_hr, (int, float)):
        return None
    lo_f, hi_f = intensity["hrr_fraction"]

    def target(frac: float) -> int:
        return round(resting_hr + frac * (220 - age - resting_hr))

    return {
        "method": "hrr",
        "bpm": [target(lo_f), target(hi_f)],
        "rpe": intensity["rpe_target"],
        "talk_test": intensity["talk_test"],
    }


def build_plan(assessment: dict, dosing_rules: dict, dose_modifiers: list | None = None) -> dict:
    baseline = assessment.get("activity_baseline")
    if baseline not in BASELINES:
        raise ValueError(f"活动基础缺失或非法: {baseline!r}，需要人工补录")

    pace = _row(dosing_rules["pace_table"], baseline)
    volume = _row(dosing_rules["volume_table"], baseline)
    progression = dosing_rules["progression"]

    plan = {
        "baseline": baseline,
        "pace_kmh": list(pace["pace_kmh"]),
        "pace_comment": pace.get("comment"),
        "minutes": list(volume["minutes"]),
        "weekly_sessions": (
            list(volume["weekly_sessions"])
            if isinstance(volume["weekly_sessions"], list)
            else [volume["weekly_sessions"], volume["weekly_sessions"]]
        ),
        "warmup_minutes": volume["warmup_minutes"],
        "cooldown_minutes": volume["cooldown_minutes"],
        "hr_zone": hr_zone(assessment, dosing_rules),
        "intensity_method_note": (
            "使用抑制心率反应药物，按 RPE 11-13 与说话测试控强度，不设数值心率区间"
            if _uses_hr_suppressing_meds(assessment, dosing_rules)
            else (
                "年龄或静息心率缺失，暂按 RPE 11-13 与说话测试控强度，需补录后复核心率区间"
                if hr_zone(assessment, dosing_rules) is None
                else "按 HRR 40%-59% + RPE 11-13 + 说话测试双重控强度"
            )
        ),
        "progression": {
            "min_weeks_per_step": progression["min_weeks_per_step"],
            "duration_increase_cap_pct_per_week": progression["duration_increase_cap_pct_per_week"],
            "pace_increase_kmh_per_step": progression["pace_increase_kmh_per_step"],
            "require_full_adherence": progression["require_full_adherence"],
        },
        "safety_notes": list(dosing_rules.get("safety_notes", [])),
        "dose_modifiers": list(dose_modifiers or []),
        "physician_supervised_start": False,
    }

    if "deconditioned_start" in (dose_modifiers or []):
        dd = progression["deload_on_relative_ci"]
        plan.update(
            {
                "pace_kmh": list(dd["pace_kmh"]),
                "minutes": list(dd["minutes"]),
                "weekly_sessions": [dd["weekly_sessions"], dd["weekly_sessions"]],
            }
        )
        plan["progression"]["min_weeks_per_step"] = dd["min_weeks_per_step"]
        plan["safety_notes"].insert(
            0, "近期肌肉骨骼问题：按降阶方案起步，关节疼痛加重立即停止并就医评估。"
        )

    return plan
