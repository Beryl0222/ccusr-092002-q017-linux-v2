"""按自然日重放事件，给出可复算的安全决定。

任意一天的结果都由四样输入唯一决定：
  规则包版本 + 当日生效的处方版本 + 当日生效的医生覆盖 + 当日事件（账本追加顺序）
因此给定同样输入，重放结果逐字节一致。

决定优先级（高 -> 低）：
  EMERGENCY_ADVICE > PAUSE > WITHHOLD > ESCALATE > APPROVE > NO_SESSION
紧急症状只输出就医提示，不生成任何病因或诊断性结论。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .events import (
    CAUTION_SYMPTOMS,
    EMERGENCY_SYMPTOMS,
    PAUSE_SYMPTOMS,
    REQUIRED_STARTUP,
    Event,
    StreamValidator,
)
from .rules import RulePack, get_pack

# 最终决定码
APPROVE = "APPROVE"
WITHHOLD = "WITHHOLD"
PAUSE = "PAUSE"
ESCALATE = "ESCALATE"
EMERGENCY_ADVICE = "EMERGENCY_ADVICE"
NO_SESSION = "NO_SESSION"

EMERGENCY_TEXT = "立即停止活动、原地休息，请他人陪同尽快急诊或拨打 120；不要自行驾车就医。"


@dataclass
class TraceEntry:
    event_id: str
    seq: int
    type: str
    decision: str
    reasons: list[str] = field(default_factory=list)
    quality_flags: list[dict] = field(default_factory=list)


@dataclass
class DayDecision:
    patient_ref: str
    day: str
    rule_pack_id: str
    prescription_version: Optional[str]
    final: str
    approved: bool = False
    started: bool = False
    ended: bool = False
    duration_min: Optional[float] = None
    emergency_advice: Optional[str] = None
    emergency_symptoms: list[str] = field(default_factory=list)
    pause_reasons: list[str] = field(default_factory=list)
    withhold_reasons: list[str] = field(default_factory=list)
    review_reasons: list[str] = field(default_factory=list)
    deviations: list[dict] = field(default_factory=list)
    quality_flags: list[dict] = field(default_factory=list)
    trace: list[dict] = field(default_factory=list)

    @property
    def review_required(self) -> bool:
        return bool(self.review_reasons)

    def to_dict(self) -> dict[str, Any]:
        return {
            "patient_ref": self.patient_ref,
            "day": self.day,
            "rule_pack_id": self.rule_pack_id,
            "prescription_version": self.prescription_version,
            "decision": self.final,
            "approved": self.approved,
            "started": self.started,
            "ended": self.ended,
            "duration_min": self.duration_min,
            "emergency_advice": self.emergency_advice,
            "emergency_symptoms": list(self.emergency_symptoms),
            "pause_reasons": list(self.pause_reasons),
            "withhold_reasons": list(self.withhold_reasons),
            "review_required": self.review_required,
            "review_reasons": list(self.review_reasons),
            "deviations": list(self.deviations),
            "quality_flags": list(self.quality_flags),
            "trace": list(self.trace),
        }


def _hr_upper_limit(prescription: dict) -> Optional[int]:
    zone = (prescription.get("dose") or {}).get("hr_zone") or {}
    if zone.get("method") == "hrr" and zone.get("bpm_range"):
        try:
            return int(zone["bpm_range"][1])
        except (TypeError, ValueError, IndexError):
            return None
    return None


def evaluate_day(
    patient_ref: str,
    day: str,
    prescription: Optional[dict],
    raw_events: list[dict],
    *,
    pack_id: Optional[str] = None,
    active_overrides: Optional[list[dict]] = None,
) -> DayDecision:
    """重放某个自然日。

    prescription: 当日生效处方版本（含 dose），None 表示当日无生效处方。
    raw_events:   当日事件，顺序必须与账本追加顺序一致（重放的一部分）。
    """
    pack: RulePack = get_pack(pack_id or (prescription or {}).get("rule_pack_id") or _DEFAULT_PACK)
    active_overrides = active_overrides or []
    dec = DayDecision(
        patient_ref=patient_ref,
        day=day,
        rule_pack_id=pack.pack_id + "@" + pack.version,
        prescription_version=(prescription or {}).get("version"),
        final=NO_SESSION,
    )

    # 无生效处方：任何运动事件都不予获准（紧急症状仍必须提示就医）
    no_rx = prescription is None or prescription.get("status") not in {"active", "adjusting"}
    if no_rx:
        dec.withhold_reasons.append("当日无生效处方版本")

    validator = StreamValidator()
    startup_seen: dict[str, Event] = {}
    gate_passed = False
    hr_upper = None if prescription is None else _hr_upper_limit(prescription)
    hr_over_streak = 0
    rest_sbp: Optional[float] = None

    if not raw_events:
        dec.final = NO_SESSION
        dec.trace.append({
            "event_id": None, "decision": NO_SESSION,
            "reasons": ["当日账本无任何事件"], "quality_flags": [],
        })
        return _finalize(dec)

    for raw in raw_events:
        event, flags = validator.check(raw)
        if flags:
            qf = [{"code": f.code, "event_id": f.event_id, "detail": f.detail} for f in flags]
            dec.quality_flags.extend(qf)
            eid = raw.get("event_id") if isinstance(raw, dict) else None
            seq = raw.get("seq") if isinstance(raw, dict) else None
            etype = raw.get("type") if isinstance(raw, dict) else None
            # 缺数据/乱序绝不能当作安全：当日不再可能 APPROVE
            dec.withhold_reasons.append("数据质量问题，故障关闭：" + "；".join(f["detail"] for f in qf))
            dec.review_reasons.append("事件流存在缺失/乱序/非法载荷，需人工核对")
            dec.trace.append(TraceEntry(eid or "?", seq if isinstance(seq, int) else -1,
                                        str(etype), WITHHOLD,
                                        ["数据质量问题"], qf).__dict__)
            continue

        reasons: list[str] = []
        event_decision = APPROVE

        if event.type in REQUIRED_STARTUP:
            startup_seen[event.type] = event

        if event.type == "symptom_check":
            symptoms = event.data["symptoms"]
            emergency = [s for s in symptoms if s in EMERGENCY_SYMPTOMS]
            pausing = [s for s in symptoms if s in PAUSE_SYMPTOMS]
            caution = [s for s in symptoms if s in CAUTION_SYMPTOMS]
            if emergency:
                dec.emergency_symptoms.extend(emergency)
                dec.emergency_advice = EMERGENCY_TEXT
                dec.pause_reasons.append(f"运动前报告紧急症状：{','.join(emergency)}")
                reasons.append("紧急症状")
                event_decision = EMERGENCY_ADVICE
            elif pausing:
                dec.pause_reasons.append(f"运动前报告：{','.join(pausing)}")
                dec.review_reasons.append("运动前出现关节剧痛/急性损伤，需人工复核")
                event_decision = PAUSE
            elif caution:
                dec.withhold_reasons.append(f"运动前预警症状：{','.join(caution)}")
                dec.review_reasons.append("运动前预警症状需人工评估")
                event_decision = WITHHOLD

        elif event.type == "vitals_reading":
            if rest_sbp is None:
                rest_sbp = float(event.data["systolic_mmhg"])
            violations = _vitals_gate(event.data, pack, bool(active_overrides))
            if violations:
                dec.withhold_reasons.extend(violations)
                dec.review_reasons.append("运动前生命体征/血糖未过闸门，需临床确认")
                event_decision = WITHHOLD

        elif event.type == "session_start":
            missing_startup = [t for t in REQUIRED_STARTUP if t not in startup_seen]
            if missing_startup:
                dec.withhold_reasons.append(f"未完成前置检查即开始：缺 {','.join(missing_startup)}")
                dec.review_reasons.append("未完成症状/体征检查即开始活动")
                dec.pause_reasons.append("不安全开跑，立即停止")
                event_decision = PAUSE
            elif not _startup_passed(dec):
                dec.withhold_reasons.append("前置检查未通过即开始活动")
                dec.pause_reasons.append("闸门未放行，立即停止")
                event_decision = PAUSE
            elif no_rx:
                dec.pause_reasons.append("无生效处方，立即停止")
                event_decision = PAUSE
            else:
                dec.started = True
                event_decision = APPROVE

        elif event.type == "hr_sample":
            if not dec.started:
                dec.review_reasons.append("收到运动中心率样本但无 session_start，起止记录不完整")
            hr = float(event.data["hr_bpm"])
            if hr_upper is None:
                # β 阻滞剂等情形：心率不是硬闸门，但仍记录给复核视图
                reasons.append("心率区间不适用（以 RPE 为准），样本仅记录")
            elif hr > hr_upper:
                hr_over_streak += 1
                dec.deviations.append({"event_id": event.event_id, "kind": "hr_over_limit",
                                       "value": hr, "limit": hr_upper})
                reasons.append(f"心率 {hr:g} bpm 超上限 {hr_upper}")
                if hr_over_streak >= pack.thresholds["hr_over_limit_samples"]:
                    dec.pause_reasons.append(
                        f"连续 {hr_over_streak} 个心率样本超过上限 {hr_upper} bpm")
                    event_decision = PAUSE
                else:
                    event_decision = ESCALATE
            else:
                hr_over_streak = 0

        elif event.type == "pace_sample":
            if not dec.started:
                dec.review_reasons.append("收到运动中配速样本但无 session_start，起止记录不完整")
            pace = float(event.data["pace_kmh"])
            rng = (prescription or {}).get("dose", {}).get("pace_kmh_range")
            if rng:
                upper = float(rng[1]) + pack.thresholds["pace_tolerance_kmh"]
                if pace > upper:
                    dec.deviations.append({"event_id": event.event_id, "kind": "pace_too_fast",
                                           "value": pace, "limit": upper})
                    reasons.append(f"配速 {pace:g} km/h 快于允许上限 {upper:g}")
                    event_decision = ESCALATE

        elif event.type == "symptom_onset":
            symptom = event.data["symptom"]
            if symptom in EMERGENCY_SYMPTOMS:
                dec.emergency_symptoms.append(symptom)
                dec.emergency_advice = EMERGENCY_TEXT
                dec.pause_reasons.append(f"运动中出现紧急症状：{symptom}")
                event_decision = EMERGENCY_ADVICE
            else:
                dec.pause_reasons.append(f"运动中出现症状：{symptom}")
                dec.review_reasons.append(f"运动中出现 {symptom}，需人工复核")
                event_decision = PAUSE

        elif event.type == "pause_request":
            dec.pause_reasons.append(f"主动暂停：{event.data['reason']}")
            event_decision = PAUSE

        elif event.type == "session_end":
            if not dec.started:
                dec.withhold_reasons.append("收到 session_end 但无有效 session_start")
                dec.review_reasons.append("会话起止不完整，数据不可信")
                event_decision = WITHHOLD
            else:
                dec.ended = True
                dec.duration_min = float(event.data["duration_min"])
                event_decision = APPROVE

        # 前置闸门每步实时重算：后到的异常体征可以撤销先前的放行
        if all(t in startup_seen for t in REQUIRED_STARTUP):
            gate_passed = _startup_passed(dec)

        if len(dec.deviations) >= pack.thresholds["deviation_review_after"] and not any(
            r.startswith("偏离累计") for r in dec.review_reasons
        ):
            dec.review_reasons.append(
                f"偏离累计 {len(dec.deviations)} 次，达到人工复核阈值")

        dec.trace.append({
            "event_id": event.event_id,
            "seq": event.seq,
            "type": event.type,
            "decision": event_decision,
            "reasons": reasons,
            "quality_flags": [],
        })

    # 日终完整性：开跑了但没有 session_end -> 数据缺口，不能算安全完成
    if dec.started and not dec.ended:
        dec.withhold_reasons.append("日终缺少 session_end，活动数据不完整")
        dec.review_reasons.append("活动未正常结束，需确认人员安全并补录")

    # 前置检查从未齐全 -> 当日未获准（有事件但不能放行）
    if raw_events and not all(t in startup_seen for t in REQUIRED_STARTUP):
        missing = [t for t in REQUIRED_STARTUP if t not in startup_seen]
        dec.withhold_reasons.append(f"日终仍缺少前置事件：{','.join(missing)}")
        dec.review_reasons.append("前置检查数据缺失，按不安全处理")

    dec.approved = gate_passed
    return _finalize(dec)


def _startup_passed(dec: DayDecision) -> bool:
    """前置症状与体征闸门是否通过（依据已累积的暂停/ withhold 原因）。"""
    if dec.emergency_advice or dec.emergency_symptoms:
        return False
    return not dec.pause_reasons and not dec.withhold_reasons


_VITALS_RULES = (
    ("systolic_mmhg", "preexercise_sbp", "收缩压"),
    ("diastolic_mmhg", "preexercise_dbp", "舒张压"),
    ("heart_rate_bpm", "preexercise_hr", "心率"),
)


def _vitals_gate(data: dict, pack: RulePack, _has_override: bool) -> list[str]:
    violations: list[str] = []
    for field_name, threshold_key, label in _VITALS_RULES:
        lo, hi = pack.thresholds[threshold_key]
        value = float(data[field_name])
        if not lo <= value <= hi:
            violations.append(f"{label} {value:g} 超出闸门 [{lo}, {hi}]")
    glucose = float(data["glucose_mmol_l"])
    glo, ghi = pack.thresholds["glucose_hard"]
    if not glo <= glucose <= ghi:
        violations.append(f"血糖 {glucose:g} mmol/L 超出硬闸门 [{glo}, {ghi}]")
    return violations


_DEFAULT_PACK = "slow-jogging-safety@1.0.0"


def _finalize(dec: DayDecision) -> DayDecision:
    if dec.emergency_advice:
        dec.final = EMERGENCY_ADVICE
    elif dec.pause_reasons:
        dec.final = PAUSE
    elif dec.withhold_reasons:
        dec.final = WITHHOLD
    elif dec.review_reasons:
        dec.final = ESCALATE
    elif dec.approved and dec.started and dec.ended:
        dec.final = APPROVE
    else:
        dec.final = NO_SESSION
    return dec
