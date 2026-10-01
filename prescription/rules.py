"""版本化禁忌规则包与运动剂量计算。

规则包一经发布即不可变：只允许新增版本，所有决定都引用 rule_pack_id。
筛查结果分三级：ABSOLUTE（绝对禁忌，系统强制阻断，不可覆盖）、
RELATIVE（相对禁忌，默认暂缓，可凭专科许可或医生覆盖放行）、
ADAPTIVE（自适应因素，不禁忌但必须调剂量与监测）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Optional

ABSOLUTE = "ABSOLUTE"
RELATIVE = "RELATIVE"
ADAPTIVE = "ADAPTIVE"

LEVELS = (ABSOLUTE, RELATIVE, ADAPTIVE)

# 决策码
NOT_PRESCRIBABLE = "NOT_PRESCRIBABLE"
NEEDS_INFO = "NEEDS_INFO"          # 资料/指标不足，不能开具
RELATIVE_BLOCKED = "RELATIVE_BLOCKED"
PRESCRIBABLE = "PRESCRIBABLE"


@dataclass(frozen=True)
class Finding:
    rule_code: str
    level: str
    title: str
    detail: str
    required_specialty: Optional[str] = None


@dataclass(frozen=True)
class ContraRule:
    code: str
    level: str
    title: str
    matches: Callable[[dict], Optional[str]]   # 返回命中说明，未命中返回 None
    required_specialty: Optional[str] = None


@dataclass(frozen=True)
class RulePack:
    pack_id: str
    version: str
    issued_on: str
    rules: tuple[ContraRule, ...]
    # 指标有效期（天），超期视为陈旧，必须复测
    metric_max_age_days: int
    # 日决策监测阈值
    thresholds: dict[str, Any]
    # 剂量基线
    dose_defaults: dict[str, Any]
    notes: str = ""

    def screen(self, assessment: dict) -> list[Finding]:
        """按包内规则顺序筛查，返回全部命中项（不因绝对禁忌而提前退出）。"""
        findings: list[Finding] = []
        for rule in self.rules:
            detail = rule.matches(assessment)
            if detail is not None:
                findings.append(
                    Finding(
                        rule_code=rule.code,
                        level=rule.level,
                        title=rule.title,
                        detail=detail,
                        required_specialty=rule.required_specialty,
                    )
                )
        return findings


# ---------------------------------------------------------------------------
# 评估视图辅助
# ---------------------------------------------------------------------------

def _metrics(a: dict) -> dict:
    return a.get("recent_metrics") or {}


def _metric(a: dict, key: str) -> Optional[float]:
    v = _metrics(a).get(key)
    return float(v) if isinstance(v, (int, float)) else None


def _diagnoses(a: dict) -> list[str]:
    return list(a.get("diagnoses") or [])


def _diagnosis_entry(a: dict, code: str) -> Optional[dict]:
    for d in a.get("diagnoses") or []:
        if isinstance(d, dict) and d.get("code") == code:
            return d
    return None


def _has_diagnosis(a: dict, code: str) -> bool:
    return code in _diagnoses(a) or _diagnosis_entry(a, code) is not None


def _med_categories(a: dict) -> set[str]:
    cats: set[str] = set()
    for m in a.get("medications") or []:
        if isinstance(m, dict) and m.get("category"):
            cats.add(str(m["category"]))
    return cats


def _baseline(a: dict) -> str:
    b = a.get("activity_baseline")
    if isinstance(b, dict):
        return str(b.get("level") or "unknown")
    return str(b or "unknown")


def _metric_age_days(a: dict, today: Optional[date]) -> Optional[int]:
    measured_on = _metrics(a).get("measured_on") or a.get("measured_on")
    if not measured_on or today is None:
        return None
    try:
        y, m, d = (int(x) for x in str(measured_on)[:10].split("-"))
        return (today - date(y, m, d)).days
    except (ValueError, TypeError):
        return None


REQUIRED_ASSESSMENT_FIELDS = (
    "diagnoses",
    "recent_metrics",
    "medications",
    "activity_baseline",
    "permission_opinion",
)

REQUIRED_METRICS = ("systolic_mmhg", "diastolic_mmhg", "resting_hr_bpm")


def missing_assessment(assessment: dict) -> list[str]:
    """录入完整性检查；指标字段本身的缺失另行返回。"""
    missing = [f for f in REQUIRED_ASSESSMENT_FIELDS if f not in assessment]
    m = _metrics(assessment)
    for k in REQUIRED_METRICS:
        if not isinstance(m.get(k), (int, float)):
            missing.append(f"recent_metrics.{k}")
    if not m.get("measured_on") and not assessment.get("measured_on"):
        missing.append("recent_metrics.measured_on")
    if not isinstance(assessment.get("age"), int):
        missing.append("age")
    return missing


# ---------------------------------------------------------------------------
# 规则谓词 —— 命中返回说明字符串，未命中返回 None
# ---------------------------------------------------------------------------

def _p_acute_coronary(a: dict):
    if _has_diagnosis(a, "unstable_angina"):
        return "诊断含不稳定型心绞痛"
    if _has_diagnosis(a, "acute_coronary_syndrome"):
        return "诊断含急性冠脉综合征"
    return None


def _p_decompensated_hf(a: dict):
    e = _diagnosis_entry(a, "heart_failure")
    if e and str(e.get("stage", "")).upper() in {"DECOMPENSATED", "NYHA_IV"}:
        return f"心力衰竭分期 {e.get('stage')}（失代偿/IV 级）"
    if _has_diagnosis(a, "decompensated_heart_failure"):
        return "诊断含失代偿性心力衰竭"
    return None


def _p_recent_cv_event(a: dict):
    e = _diagnosis_entry(a, "recent_cardiovascular_event")
    if e is None:
        return None
    onset = e.get("event_date")
    days: Optional[int]
    try:
        y, m, d = (int(x) for x in str(onset)[:10].split("-"))
        # 以规则包内置"评估日"无法取得，交给阈值比较：这里仅在有 event_age_days 时判定
        days = e.get("event_age_days")
        if days is None and isinstance(a.get("as_of_date"), str):
            ay, am, ad = (int(x) for x in str(a["as_of_date"])[:10].split("-"))
            days = (date(ay, am, ad) - date(y, m, d)).days
    except (ValueError, TypeError):
        days = e.get("event_age_days")
    if days is None:
        return "近期心血管事件但缺少发病日期，按未稳定处理"
    if days < 42:
        return f"心血管事件后仅 {days} 天（<42 天），处于急性期"
    return None


def _p_severe_hypertension(a: dict):
    sbp, dbp = _metric(a, "systolic_mmhg"), _metric(a, "diastolic_mmhg")
    if sbp is not None and sbp >= 180:
        return f"静息收缩压 {sbp:g} mmHg ≥ 180"
    if dbp is not None and dbp >= 110:
        return f"静息舒张压 {dbp:g} mmHg ≥ 110"
    return None


def _p_acute_infection(a: dict):
    if _has_diagnosis(a, "acute_infection"):
        return "诊断含急性感染（发热期）"
    temp = _metric(a, "temperature_c")
    if temp is not None and temp >= 38.0:
        return f"体温 {temp:g}℃ ≥ 38℃"
    return None


def _p_acute_vascular(a: dict):
    for code, label in (
        ("acute_pe_or_dvt", "急性肺栓塞/深静脉血栓"),
        ("aortic_dissection", "主动脉夹层"),
        ("acute_myocarditis", "急性心肌炎"),
        ("acute_pericarditis", "急性心包炎"),
        ("acute_endocarditis", "急性心内膜炎"),
    ):
        if _has_diagnosis(a, code):
            return f"诊断含{label}"
    return None


def _p_severe_valve(a: dict):
    if _has_diagnosis(a, "severe_symptomatic_aortic_stenosis"):
        return "诊断含重度有症状主动脉瓣狭窄"
    return None


def _p_acute_joint_injury(a: dict):
    # 与契约样例字段 joint_injury 对齐：true 表示严重/急性关节损伤未稳定
    if a.get("joint_injury") is True:
        return "存在严重关节损伤（未稳定）"
    if _has_diagnosis(a, "acute_severe_joint_injury"):
        return "诊断含急性严重关节损伤"
    if _has_diagnosis(a, "acute_exercise_injury"):
        return "诊断含急性运动损伤"
    return None


def _p_elevated_bp_relative(a: dict):
    sbp, dbp = _metric(a, "systolic_mmhg"), _metric(a, "diastolic_mmhg")
    hi_sbp = sbp is not None and 160 <= sbp < 180
    hi_dbp = dbp is not None and 100 <= dbp < 110
    if hi_sbp or hi_dbp:
        return f"静息血压 {sbp:g}/{dbp:g} mmHg 处于 160-179/100-109 区间"
    return None


def _p_diabetes_unstable(a: dict):
    e = _diagnosis_entry(a, "diabetes")
    has_dm = _has_diagnosis(a, "diabetes")
    if not (has_dm or e):
        return None
    hba1c = _metric(a, "hba1c_percent") or (e or {}).get("hba1c_percent")
    glucose = _metric(a, "fasting_glucose_mmol_l")
    recent_hypo = bool((e or {}).get("recent_hypoglycemia"))
    reasons = []
    if isinstance(hba1c, (int, float)) and float(hba1c) >= 9.0:
        reasons.append(f"HbA1c {hba1c:g}% ≥ 9%")
    if glucose is not None and not (4.4 <= glucose <= 13.9):
        reasons.append(f"空腹血糖 {glucose:g} mmol/L 超出 4.4-13.9")
    if recent_hypo:
        reasons.append("近期发生过低血糖")
    if (e or {}).get("hypoglycemia_unawareness"):
        reasons.append("存在低血糖感知减退")
    return "糖尿病控制不稳：" + "、".join(reasons) if reasons else None


def _p_stable_cardiac_no_clearance(a: dict):
    codes = {
        "stable_coronary_disease": "稳定性冠心病",
        "compensated_heart_failure": "代偿性心力衰竭",
        "arrhythmia": "心律失常",
    }
    for code, label in codes.items():
        if _has_diagnosis(a, code) and not _permission_specialty(a, "cardiology"):
            return f"{label}但缺少心内科许可意见"
    e = _diagnosis_entry(a, "recent_cardiovascular_event")
    if e is not None and not _p_recent_cv_event(a) and not _permission_specialty(a, "cardiology"):
        return "心血管事件已过急性期但缺少心内科复训许可"
    return None


def _p_severe_osteoarthritis(a: dict):
    if _has_diagnosis(a, "severe_osteoarthritis") and not _permission_specialty(a, "orthopedics"):
        return "重度骨关节炎但缺少骨科许可意见"
    return None


def _permission_specialty(a: dict, specialty: str) -> bool:
    op = a.get("permission_opinion")
    if isinstance(op, list):
        return any(
            isinstance(x, dict)
            and x.get("decision") == "approved"
            and x.get("specialty") == specialty
            for x in op
        )
    if isinstance(op, dict):
        return op.get("decision") == "approved" and op.get("specialty") == specialty
    return False


def _p_controlled_hypertension(a: dict):
    if _has_diagnosis(a, "hypertension_controlled"):
        return "高血压（控制中）：延长热身、降低心率上限"
    sbp, dbp = _metric(a, "systolic_mmhg"), _metric(a, "diastolic_mmhg")
    if sbp is not None and dbp is not None and 140 <= sbp < 160 and 90 <= dbp < 100:
        return f"血压 {sbp:g}/{dbp:g} mmHg 偏高（1 级）：温和降档"
    return None


def _p_diabetes_controlled(a: dict):
    e = _diagnosis_entry(a, "diabetes")
    if _has_diagnosis(a, "diabetes") or e:
        # 控制不稳由 RELATIVE 规则负责；到这里仅给出适应建议
        return "糖尿病：运动前后需测血糖、随身携带碳水"
    return None


def _p_beta_blocker(a: dict):
    if "beta_blocker" in _med_categories(a):
        return "使用 β 受体阻滞剂：心率区间失真，改用 RPE 与说话测试"
    return None


def _p_sedentary(a: dict):
    if _baseline(a) in {"none", "sedentary", "unknown"}:
        return "活动基础不足：从最低剂量起步"
    return None


def _p_older_adult(a: dict):
    age = a.get("age")
    if isinstance(age, int) and age >= 65:
        return f"年龄 {age} 岁：延长热身、放慢递增"
    return None


# ---------------------------------------------------------------------------
# 规则包 v1.0.0（超慢跑社区安全基线）
# ---------------------------------------------------------------------------

V1 = RulePack(
    pack_id="slow-jogging-safety",
    version="1.0.0",
    issued_on="2026-10-01",
    metric_max_age_days=14,
    thresholds={
        # 运动前生命体征闸门
        "preexercise_sbp": [90, 160],
        "preexercise_dbp": [60, 100],
        "preexercise_hr": [40, 100],
        "glucose_hard": [4.4, 16.7],          # mmol/L，越界不得开跑
        "glucose_dm_caution": [5.6, 13.9],    # 糖尿病患者建议区间
        # 运动中
        "hr_over_limit_samples": 3,           # 连续 3 个样本越限 -> PAUSE
        "sbp_exercise_max": 220,
        "sbp_drop_from_rest": 20,             # 收缩压较静息下降幅度
        "pace_tolerance_kmh": 0.5,            # 超过区间上限 0.5 km/h 计偏离
        "deviation_review_after": 2,          # 当日偏离累计 2 次 -> 人工复核
    },
    dose_defaults={
        "pace_kmh_range": [4.0, 6.0],
        "minutes_range": [20, 30],
        "weekly_sessions_range": [3, 5],
        "warmup_min": 5,
        "cooldown_min": 5,
        "hrr_pct_range": [40, 60],            # 超慢跑取 HRR 40%-60%
        "rpe_range": [11, 13],                # 说话能成句
        "progression_pct_week": 10,           # 周时长/里程增幅上限
        "adaptive_progression_pct_week": 5,
        "adaptive_pace_kmh_range": [3.0, 4.5],
        "adaptive_minutes_range": [10, 15],
        "adaptive_warmup_min": 10,
    },
    rules=(
        # ABSOLUTE —— 系统强制阻断，医生覆盖无效
        ContraRule("ABS_CV_UNSTABLE", ABSOLUTE, "急性/不稳定心血管疾病", _p_acute_coronary, "cardiology"),
        ContraRule("ABS_HF_DECOMP", ABSOLUTE, "失代偿性心力衰竭", _p_decompensated_hf, "cardiology"),
        ContraRule("ABS_CV_EVENT_RECENT", ABSOLUTE, "心血管事件急性期（6 周内）", _p_recent_cv_event, "cardiology"),
        ContraRule("ABS_BP_SEVERE", ABSOLUTE, "未控制的重度高血压（≥180/110）", _p_severe_hypertension),
        ContraRule("ABS_ACUTE_INFECTION", ABSOLUTE, "急性感染/发热", _p_acute_infection),
        ContraRule("ABS_ACUTE_VASCULAR", ABSOLUTE, "急性血管事件/心脏炎症", _p_acute_vascular, "cardiology"),
        ContraRule("ABS_VALVE_SEVERE", ABSOLUTE, "重度有症状瓣膜病", _p_severe_valve, "cardiology"),
        ContraRule("ABS_JOINT_ACUTE", ABSOLUTE, "严重/急性关节损伤或急性运动损伤", _p_acute_joint_injury, "orthopedics"),
        # RELATIVE —— 默认暂缓，需专科许可或具权限医生覆盖
        ContraRule("REL_BP_ELEVATED", RELATIVE, "血压明显升高（160-179/100-109）", _p_elevated_bp_relative),
        ContraRule("REL_DM_UNSTABLE", RELATIVE, "糖尿病控制不稳", _p_diabetes_unstable, "endocrinology"),
        ContraRule("REL_CARDIAC_NO_CLEARANCE", RELATIVE, "心脏病情稳定但缺少许可", _p_stable_cardiac_no_clearance, "cardiology"),
        ContraRule("REL_OA_SEVERE", RELATIVE, "重度骨关节炎缺少骨科许可", _p_severe_osteoarthritis, "orthopedics"),
        # ADAPTIVE —— 不阻断，调整剂量
        ContraRule("ADAPT_HTN", ADAPTIVE, "高血压（控制中）", _p_controlled_hypertension),
        ContraRule("ADAPT_DM", ADAPTIVE, "糖尿病（适应监测）", _p_diabetes_controlled),
        ContraRule("ADAPT_BETA_BLOCKER", ADAPTIVE, "β 受体阻滞剂用药", _p_beta_blocker),
        ContraRule("ADAPT_SEDENTARY", ADAPTIVE, "活动基础不足", _p_sedentary),
        ContraRule("ADAPT_OLDER", ADAPTIVE, "高龄", _p_older_adult),
    ),
    notes="超慢跑社区安全基线 v1：绝对禁忌强制阻断；相对禁忌须许可或覆盖；自适应因素下调起步剂量。",
)

PACKS: dict[str, RulePack] = {f"{V1.pack_id}@{V1.version}": V1}
DEFAULT_PACK_ID = f"{V1.pack_id}@{V1.version}"


def get_pack(pack_id: str) -> RulePack:
    if pack_id not in PACKS:
        raise KeyError(f"未知或已停用的规则包: {pack_id}")
    return PACKS[pack_id]


# ---------------------------------------------------------------------------
# 筛查结论与剂量
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Screening:
    pack_id: str
    findings: tuple[Finding, ...]
    stale_metrics: bool
    missing: tuple[str, ...]

    @property
    def absolute(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.level == ABSOLUTE)

    @property
    def relative(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.level == RELATIVE)

    @property
    def adaptive(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.level == ADAPTIVE)


def screen(assessment: dict, pack_id: str = DEFAULT_PACK_ID, today: Optional[date] = None) -> Screening:
    pack = get_pack(pack_id)
    findings = pack.screen(assessment)
    age = _metric_age_days(assessment, today)
    stale = age is not None and age > pack.metric_max_age_days
    return Screening(
        pack_id=pack_id,
        findings=tuple(findings),
        stale_metrics=stale,
        missing=tuple(missing_assessment(assessment)),
    )


def relative_unresolved(screening: Screening, assessment: dict) -> tuple[Finding, ...]:
    """相对禁忌中未被专科许可意见消解的项。"""
    unresolved = []
    for f in screening.relative:
        if f.required_specialty and _permission_specialty(assessment, f.required_specialty):
            continue
        unresolved.append(f)
    return tuple(unresolved)


def compute_dose(
    assessment: dict,
    screening: Screening,
    overridden_codes: frozenset[str] = frozenset(),
) -> dict:
    """根据筛查命中（含已生效覆盖）计算剂量；覆盖只解除阻断，不撤销自适应。"""
    pack = get_pack(screening.pack_id)
    d = dict(pack.dose_defaults)
    active_adaptive = {f.rule_code for f in screening.adaptive}
    # 被覆盖的相对禁忌仍要按高风险人群降档
    risky_relative = {
        f.rule_code for f in screening.relative
    } - set(overridden_codes)
    downgrade = bool(active_adaptive) or bool(risky_relative) or bool(overridden_codes)

    pace = list(d["pace_kmh_range"])
    minutes = list(d["minutes_range"])
    warmup = d["warmup_min"]
    prog = d["progression_pct_week"]
    monitoring: list[str] = []
    cautions: list[str] = []

    if downgrade:
        pace = list(d["adaptive_pace_kmh_range"])
        minutes = list(d["adaptive_minutes_range"])
        warmup = d["adaptive_warmup_min"]
        prog = d["adaptive_progression_pct_week"]
    if "ADAPT_OLDER" in active_adaptive:
        warmup = max(warmup, 10)
    if "ADAPT_HTN" in active_adaptive:
        monitoring.append("运动前后测血压；血压 ≥160/100 当日不开跑")
    if "ADAPT_DM" in active_adaptive:
        monitoring.append("运动前后测血糖；随身携带碳水；避免空腹运动")
    if "ADAPT_BETA_BLOCKER" in active_adaptive:
        monitoring.append("按 RPE 11-13 与说话测试控强度，心率仅作参考")
        cautions.append("β 阻滞剂使用者不使用心率上限作为唯一暂停依据")
    if overridden_codes:
        cautions.append(
            "存在医生覆盖的相对禁忌：" + "、".join(sorted(overridden_codes)) + "；首次疗程须现场观察"
        )
        monitoring.append("覆盖期内前 2 周由医务人员现场带教")

    # 心率区间：HRR 法（需要年龄与静息心率），否则退化为 RPE
    hr_zone: dict[str, Any]
    age = assessment.get("age")
    rhr = _metric(assessment, "resting_hr_bpm")
    if isinstance(age, int) and isinstance(rhr, (int, float)):
        lo, hi = d["hrr_pct_range"]
        hr_min = round(rhr + (220 - age - rhr) * lo / 100)
        hr_max = round(rhr + (220 - age - rhr) * hi / 100)
        hr_zone = {"method": "hrr", "bpm_range": [hr_min, hr_max], "rpe_range": d["rpe_range"]}
    else:
        hr_zone = {"method": "rpe_only", "rpe_range": d["rpe_range"], "speech_test": "能说完整句子"}

    return {
        "pace_kmh_range": pace,
        "hr_zone": hr_zone,
        # 区间是安全包络，标量是当前处方目标（从区间下限起步，按递增规则上调）
        "minutes_range": minutes,
        "minutes": minutes[0],
        "weekly_sessions_range": list(d["weekly_sessions_range"]),
        "weekly_sessions": list(d["weekly_sessions_range"])[0],
        "warmup_min": warmup,
        "cooldown_min": d["cooldown_min"],
        "progression_pct_week": prog,
        "progression_rule": (
            "每周总时长增幅不超过上限；出现暂停/复核或未完成上周计划的一周不得加量；"
            "先加时长，再提频次，最后才提速"
        ),
        "monitoring": monitoring,
        "cautions": cautions,
        "downgraded": downgrade,
    }
