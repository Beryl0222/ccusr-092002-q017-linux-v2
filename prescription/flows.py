"""应用流程：开具/调整/暂停处方、医生覆盖、事件接入与按日完整重放。

所有流程只做两件事：做确定性判断 + 往追加账本写记录。当前状态由账本折叠得到，
因此服务重启后状态一致，且任意历史日期都能用当时的输入完整重放。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

from .engine import DayDecision, evaluate_day
from .events import parse_ts
from .events import validate_envelope
from .rules import (
    ABSOLUTE,
    RELATIVE,
    Screening,
    compute_dose,
    get_pack,
    relative_unresolved,
    screen,
)
from .store import AppendOnlyStore

AUTHORITIES = ("ATTENDING", "SPECIALIST")
OVERRIDE_MAX_DAYS = 30

# 开具结论码
NOT_PRESCRIBABLE = "NOT_PRESCRIBABLE"
NEEDS_INFO = "NEEDS_INFO"
RELATIVE_BLOCKED = "RELATIVE_BLOCKED"
PRESCRIBABLE = "PRESCRIBABLE"

NO_INCREASE_AFTER = {"PAUSE", "EMERGENCY_ADVICE", "ESCALATE", "WITHHOLD"}


@dataclass
class Result:
    ok: bool
    code: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "code": self.code, "message": self.message, "data": self.data}


def _parse_day(value: str) -> date:
    return date.fromisoformat(value)


def _finding_dict(f) -> dict:
    return {
        "rule_code": f.rule_code,
        "level": f.level,
        "title": f.title,
        "detail": f.detail,
        "required_specialty": f.required_specialty,
    }


def _screening_dict(s: Screening) -> dict:
    return {
        "pack_id": s.pack_id,
        "stale_metrics": s.stale_metrics,
        "missing": list(s.missing),
        "findings": [_finding_dict(f) for f in s.findings],
        "absolute": [f.rule_code for f in s.absolute],
        "relative": [f.rule_code for f in s.relative],
        "adaptive": [f.rule_code for f in s.adaptive],
    }


def _biz_ts(today: date, ts: Optional[str]) -> str:
    """业务记录默认落在业务自然日 00:00 UTC，使按日重放不依赖机器时钟。"""
    return ts or f"{today.isoformat()}T00:00:00+00:00"


class SafetyService:
    def __init__(self, store: AppendOnlyStore, pack_id: Optional[str] = None):
        self.store = store
        self.pack_id = pack_id or _default_pack_id()

    # ------------------------------------------------------------------
    # 账本折叠：从只追加记录恢复每位患者的当前状态
    # ------------------------------------------------------------------
    def _fold(self, patient_ref: str) -> dict:
        state: dict[str, Any] = {
            "assessment": None, "assessment_ts": None,
            "prescriptions": [], "overrides": [], "events": [],
        }
        for e in self.store.read(patient_ref):
            p = e["payload"]
            kind = e["type"]
            if kind == "assessment_recorded":
                state["assessment"] = p["assessment"]
                state["assessment_ts"] = e["ts"]
            elif kind in {"prescription_created", "prescription_adjusted", "prescription_suspended"}:
                state["prescriptions"].append({**p["after"], "_ts": e["ts"]})
            elif kind == "override_granted":
                state["overrides"].append({**p["override"], "_ts": e["ts"]})
            elif kind == "event_received":
                state["events"].append({"entry_ts": e["ts"], **p})
        return state

    def _active_rx(self, state: dict, on_date: Optional[date] = None) -> Optional[dict]:
        rx = None
        for candidate in state["prescriptions"]:
            if on_date is not None and candidate["_ts"][:10] > on_date.isoformat():
                continue
            rx = candidate
        if rx and rx.get("status") == "active":
            return rx
        return None

    def _active_overrides(self, state: dict, on_date: date) -> list[dict]:
        return [
            o for o in state["overrides"]
            if o["_ts"][:10] <= on_date.isoformat()
            and o["valid_until"] >= on_date.isoformat()
            and o.get("status", "active") == "active"
        ]

    # ------------------------------------------------------------------
    # 1) 医务人员录入评估
    # ------------------------------------------------------------------
    def record_assessment(self, patient_ref: str, assessment: dict, clinician_id: str,
                          today: date, *, ts: Optional[str] = None) -> Result:
        if not clinician_id:
            return Result(False, "BAD_INPUT", "缺少 clinician_id")
        if not isinstance(assessment, dict):
            return Result(False, "BAD_INPUT", "assessment 必须是对象")
        s = screen(assessment, pack_id=self.pack_id, today=today)
        payload = {
            "assessment": assessment,
            "recorded_by": clinician_id,
            "screening": _screening_dict(s),
        }
        self.store.append("assessment_recorded", patient_ref, payload, ts=_biz_ts(today, ts))
        auto = self._auto_suspend_on_new_screening(patient_ref, assessment, s, clinician_id, today, ts)
        data = {"screening": _screening_dict(s)}
        if auto is not None:
            data["auto_suspended"] = auto
        return Result(True, "ASSESSMENT_RECORDED", "评估已录入并完成禁忌筛查", data)

    def _auto_suspend_on_new_screening(self, patient_ref: str, assessment: dict,
                                       s: Screening, clinician_id: str, today: date,
                                       ts: Optional[str]) -> Optional[dict]:
        """复评若使生效处方不再安全，系统强制暂停并留下版本，不依赖人工记得操作。"""
        state = self._fold(patient_ref)
        current = self._active_rx(state, today)
        if current is None:
            return None
        reasons: list[str] = []
        if s.absolute:
            reasons.append("复评出现绝对禁忌：" + "、".join(f.rule_code for f in s.absolute))
        overrides = self._active_overrides(state, today)
        overridden = self._valid_overridden_codes(s, overrides)
        unresolved = [f for f in relative_unresolved(s, assessment) if f.rule_code not in overridden]
        if unresolved:
            reasons.append("复评出现未消解的相对禁忌：" + "、".join(f.rule_code for f in unresolved))
        if not reasons:
            return None
        before = {k: v for k, v in current.items() if not k.startswith("_")}
        after = {
            **before,
            "version": self._next_rx_version(patient_ref),
            "status": "suspended",
            "created_by": "SYSTEM",
            "created_on": today.isoformat(),
            "supersedes": current["version"],
            "auto_suspend_reasons": reasons,
        }
        self.store.append("prescription_suspended", patient_ref, {
            "before": before, "after": after,
            "reason": "系统安全联动（复评触发）：" + "；".join(reasons),
            "triggered_by_clinician": clinician_id,
        }, ts=_biz_ts(today, ts))
        return {"version": after["version"], "reasons": reasons}

    def latest_screening(self, patient_ref: str, today: date) -> Optional[tuple[dict, Screening]]:
        state = self._fold(patient_ref)
        if state["assessment"] is None:
            return None
        s = screen(state["assessment"], pack_id=self.pack_id, today=today)
        return state["assessment"], s

    # ------------------------------------------------------------------
    # 2) 开具：版本化禁忌规则先决定能否开具
    # ------------------------------------------------------------------
    def prescribe(self, patient_ref: str, clinician_id: str, today: date,
                  *, ts: Optional[str] = None) -> Result:
        if not clinician_id:
            return Result(False, "BAD_INPUT", "缺少 clinician_id")
        folded = self.latest_screening(patient_ref, today)
        if folded is None:
            return Result(False, NEEDS_INFO, "尚未录入评估资料")
        assessment, s = folded

        if s.missing:
            return Result(False, NEEDS_INFO, "评估资料不完整，不能开具",
                          {"missing": list(s.missing), "screening": _screening_dict(s)})
        if s.stale_metrics:
            return Result(False, NEEDS_INFO,
                          f"指标已超过规则包规定的 {get_pack(self.pack_id).metric_max_age_days} 天，需复测后才能开具",
                          {"screening": _screening_dict(s)})
        if s.absolute:
            # 系统强制阻断：不产生任何处方版本
            return Result(False, NOT_PRESCRIBABLE,
                          "存在绝对禁忌，系统强制阻断，不能开具超慢跑处方（医生覆盖无效）",
                          {"findings": [_finding_dict(f) for f in s.absolute],
                           "screening": _screening_dict(s)})

        overrides = self._active_overrides(self._fold(patient_ref), today)
        overridden_codes = self._valid_overridden_codes(s, overrides)
        unresolved = [f for f in relative_unresolved(s, assessment)
                      if f.rule_code not in overridden_codes]
        if unresolved:
            return Result(False, RELATIVE_BLOCKED, "存在未消解的相对禁忌，需专科许可或医生覆盖",
                          {"findings": [_finding_dict(f) for f in unresolved],
                           "valid_overrides": sorted(overridden_codes),
                           "screening": _screening_dict(s)})

        dose = compute_dose(assessment, s, frozenset(overridden_codes))
        version = self._next_rx_version(patient_ref)
        rx = {
            "version": version,
            "status": "active",
            "rule_pack_id": self.pack_id,
            "dose": dose,
            "screening": _screening_dict(s),
            "overridden_codes": sorted(overridden_codes),
            "created_by": clinician_id,
            "created_on": today.isoformat(),
            "supersedes": None,
        }
        self.store.append("prescription_created", patient_ref, {
            "before": None, "after": rx, "reason": "首次开具",
        }, ts=_biz_ts(today, ts))
        return Result(True, PRESCRIBABLE, "可开具，已生成处方版本",
                      {"prescription": rx, "plan": _plan_view(dose)})

    # ------------------------------------------------------------------
    # 3) 调整：每次调整都留下前后版本；加量受递增规则约束
    # ------------------------------------------------------------------
    def adjust(self, patient_ref: str, clinician_id: str, changes: dict, reason: str,
               today: date, *, ts: Optional[str] = None) -> Result:
        if not reason or not reason.strip():
            return Result(False, "BAD_INPUT", "调整必须写明原因")
        state = self._fold(patient_ref)
        current = self._active_rx(state, today)
        if current is None:
            return Result(False, "NO_ACTIVE_RX", "当日无生效处方，不能调整；请先开具")

        folded = self.latest_screening(patient_ref, today)
        assessment, s = folded if folded else (None, None)
        overrides = self._active_overrides(state, today)
        overridden_codes = self._valid_overridden_codes(s, overrides) if s else frozenset()
        envelope = compute_dose(assessment, s, overridden_codes) if s else current["dose"]

        allowed_keys = {"pace_kmh_range", "minutes", "weekly_sessions"}
        bad = set(changes) - allowed_keys
        if bad:
            return Result(False, "BAD_INPUT", f"不允许调整的字段：{sorted(bad)}")

        new_dose = self._apply_changes(current["dose"], changes)
        err = self._within_envelope(new_dose, envelope)
        if err:
            return Result(False, "OUTSIDE_SAFE_ENVELOPE", err,
                          {"safe_envelope": {
                              "pace_kmh_range": envelope["pace_kmh_range"],
                              "minutes_range": envelope["minutes_range"],
                              "weekly_sessions_range": envelope["weekly_sessions_range"],
                          }})

        increasing = self._is_increase(current["dose"], new_dose)
        if increasing:
            # 先查最硬的约束：出现暂停/升级/数据不足的一周不得加量
            block_day = self._recent_unsafe_day(patient_ref, today)
            if block_day is not None:
                return Result(False, "PROGRESSION_BLOCKED",
                              f"近 7 天内 {block_day} 出现暂停/升级/数据不足，本周不得加量",
                              {"blocked_day": block_day})
            order_err = self._progression_order_ok(current["dose"], new_dose, changes)
            if order_err:
                return Result(False, "PROGRESSION_ORDER", order_err)
            rate_err = self._progression_rate_ok(current["dose"], new_dose, envelope)
            if rate_err:
                return Result(False, "PROGRESSION_TOO_FAST", rate_err)

        new_version = self._next_rx_version(patient_ref)
        after = {
            **{k: v for k, v in current.items() if not k.startswith("_")},
            "version": new_version,
            "dose": new_dose,
            "created_by": clinician_id,
            "created_on": today.isoformat(),
            "supersedes": current["version"],
        }
        self.store.append("prescription_adjusted", patient_ref, {
            "before": {k: v for k, v in current.items() if not k.startswith("_")},
            "after": after,
            "changes": changes,
            "reason": reason,
            "overridden_codes": sorted(overridden_codes),
        }, ts=_biz_ts(today, ts))
        return Result(True, "RX_ADJUSTED", f"已由 {current['version']} 调整为 {after['version']}",
                      {"before_version": current["version"], "after": after,
                       "plan": _plan_view(new_dose)})

    def suspend(self, patient_ref: str, clinician_id: str, reason: str, today: date,
                *, ts: Optional[str] = None) -> Result:
        if not reason or not reason.strip():
            return Result(False, "BAD_INPUT", "暂停必须写明原因")
        state = self._fold(patient_ref)
        current = self._active_rx(state, today)
        if current is None:
            return Result(False, "NO_ACTIVE_RX", "当日无生效处方")
        before = {k: v for k, v in current.items() if not k.startswith("_")}
        after = {**before,
                 "version": self._next_rx_version(patient_ref),
                 "status": "suspended",
                 "created_by": clinician_id,
                 "created_on": today.isoformat(),
                 "supersedes": current["version"]}
        self.store.append("prescription_suspended", patient_ref,
                          {"before": before, "after": after, "reason": reason},
                          ts=_biz_ts(today, ts))
        return Result(True, "RX_SUSPENDED", f"处方已暂停（{after['version']}）",
                      {"before_version": before["version"], "after": after})

    # ------------------------------------------------------------------
    # 4) 医生覆盖：理由 + 权限 + 有效期；ABSOLUTE 不可覆盖，到期自动失效
    # ------------------------------------------------------------------
    def grant_override(self, patient_ref: str, clinician_id: str, authority: str,
                       rule_codes: list[str], reason: str, valid_until: str,
                       today: date, *, ts: Optional[str] = None) -> Result:
        if authority not in AUTHORITIES:
            return Result(False, "BAD_AUTHORITY", f"权限级别必须是 {AUTHORITIES} 之一")
        if not reason or not reason.strip():
            return Result(False, "BAD_INPUT", "覆盖必须写明临床理由")
        if not isinstance(rule_codes, list) or not rule_codes:
            return Result(False, "BAD_INPUT", "必须点名被覆盖的规则代码")
        try:
            until = _parse_day(valid_until)
        except ValueError:
            return Result(False, "BAD_INPUT", "valid_until 必须是 YYYY-MM-DD")
        if until < today:
            return Result(False, "BAD_INPUT", "有效期不能早于今天")
        if (until - today).days > OVERRIDE_MAX_DAYS:
            return Result(False, "BAD_INPUT", f"覆盖有效期不得超过 {OVERRIDE_MAX_DAYS} 天")

        folded = self.latest_screening(patient_ref, today)
        if folded is None:
            return Result(False, NEEDS_INFO, "尚未录入评估资料")
        assessment, s = folded
        hit = {f.rule_code: f for f in s.findings}
        pack = get_pack(self.pack_id)
        known = {r.code: r for r in pack.rules}

        for code in rule_codes:
            rule = known.get(code)
            if rule is None:
                return Result(False, "UNKNOWN_RULE", f"规则 {code} 不在规则包 {self.pack_id} 中")
            if rule.level == ABSOLUTE:
                return Result(False, "ABSOLUTE_NOT_OVERRIDABLE",
                              f"{code} 属于绝对禁忌，系统强制阻断，任何权限都不能覆盖")
            if rule.level != RELATIVE:
                return Result(False, "NOT_OVERRIDABLE",
                              f"{code} 是自适应规则，无需覆盖（剂量已自动调整）")
            if code not in hit:
                return Result(False, "RULE_NOT_APPLICABLE",
                              f"当前筛查未命中 {code}，不能对未触发的规则做覆盖")
            if rule.required_specialty and authority != "SPECIALIST":
                return Result(False, "INSUFFICIENT_AUTHORITY",
                              f"{code} 需要 {rule.required_specialty} 专科的 SPECIALIST 权限")

        override = {
            "id": f"ov-{self.store.size + 1:04d}",
            "clinician_id": clinician_id,
            "authority": authority,
            "rule_codes": sorted(rule_codes),
            "reason": reason.strip(),
            "valid_from": today.isoformat(),
            "valid_until": until.isoformat(),
            "granted_on": today.isoformat(),
            "status": "active",
            "rule_pack_id": self.pack_id,
        }
        self.store.append("override_granted", patient_ref, {"override": override},
                          ts=_biz_ts(today, ts))
        return Result(True, "OVERRIDE_GRANTED", "覆盖已记录，到期自动失效",
                      {"override": override})

    # ------------------------------------------------------------------
    # 5) 事件接入：原始事件一律入账（含被拒事件），决策时 fail-closed
    # ------------------------------------------------------------------
    def ingest_event(self, raw_event: dict, *, ts: Optional[str] = None) -> Result:
        ref = raw_event.get("patient_ref") if isinstance(raw_event, dict) else None
        if not isinstance(ref, str) or not ref:
            # 无法归属患者的事件也不能静默丢弃
            self.store.append(
                "event_received", "UNASSIGNABLE",
                {"raw": raw_event, "accepted": False, "reject_reason": "缺少 patient_ref"}, ts=ts)
            return Result(False, "BAD_INPUT", "事件缺少 patient_ref")

        _, flags = validate_envelope(raw_event)
        payload = {
            "raw": raw_event,
            "accepted": not flags,
            "quality_flags": [{"code": f.code, "event_id": f.event_id, "detail": f.detail}
                              for f in flags],
        }
        self.store.append("event_received", ref, payload, ts=ts)
        if flags:
            # 仍然入账留痕，但明确告知调用方该事件不可信、当日会故障关闭
            return Result(False, "REJECTED_EVENT",
                          "事件未通过校验，已留痕；当日决策将按不安全处理",
                          {"quality_flags": payload["quality_flags"]})
        return Result(True, "EVENT_RECORDED", "事件已入账，等待按日重放决策",
                      {"event_id": raw_event.get("event_id")})

    def record_manual_review(self, patient_ref: str, day: str, clinician_id: str,
                             action: str, note: str, *, ts: Optional[str] = None) -> Result:
        if action not in {"resume", "suspend", "adjust", "no_action"}:
            return Result(False, "BAD_INPUT", "action 必须是 resume/suspend/adjust/no_action")
        _parse_day(day)
        self.store.append("manual_review", patient_ref, {
            "day": day, "clinician_id": clinician_id, "action": action, "note": note,
        }, ts=ts or f"{day}T00:00:00+00:00")
        return Result(True, "REVIEW_RECORDED", "人工复核结论已留痕",
                      {"day": day, "action": action})

    # ------------------------------------------------------------------
    # 6) 按日完整重放
    # ------------------------------------------------------------------
    def replay_day(self, patient_ref: str, day: str) -> dict[str, Any]:
        on_date = _parse_day(day)
        state = self._fold(patient_ref)
        rx = self._active_rx(state, on_date)
        overrides = self._active_overrides(state, on_date)

        raw_events: list[dict] = []
        rejected_flags: list[dict] = []
        rejected_count = 0
        for item in state["events"]:
            raw = item.get("raw")
            event_day = None
            if isinstance(raw, dict):
                parsed = parse_ts(raw.get("ts"))
                event_day = parsed.date().isoformat() if parsed else None
            # ts 无法解析的事件归到账本到达日：数据那天到达，就必须影响那天的结论
            if event_day is None and item.get("entry_ts"):
                event_day = item["entry_ts"][:10]
            if event_day != day:
                continue
            # 同日事件保持账本追加顺序——顺序本身就是重放输入
            if item.get("accepted") and isinstance(raw, dict) and event_day is not None:
                raw_events.append(raw)
            else:
                rejected_count += 1
                rejected_flags.extend(item.get("quality_flags") or [
                    {"code": "UNPARSEABLE_EVENT", "event_id": None,
                     "detail": "事件时间戳无法解析，无法纳入当日重放"}])

        decision: DayDecision = evaluate_day(
            patient_ref, day,
            rx if rx is not None else None,
            raw_events,
            pack_id=self.pack_id,
            active_overrides=overrides,
        )
        if rejected_count:
            decision.quality_flags.extend(rejected_flags)
            decision.withhold_reasons.append(
                f"当日有 {rejected_count} 条事件未通过校验，故障关闭（缺数据绝不等于安全）")
            decision.review_reasons.append("存在被拒绝的事件，需人工核对原始数据")
            decision.approved = False
            from .engine import _finalize
            _finalize(decision)
        return {
            "patient_ref": patient_ref,
            "day": day,
            "inputs": {
                "assessment_ts": state["assessment_ts"][:10] if state["assessment_ts"] else None,
                "prescription_version": rx["version"] if rx else None,
                "active_overrides": [
                    {"id": o["id"], "rule_codes": o["rule_codes"],
                     "clinician_id": o["clinician_id"], "authority": o["authority"],
                     "valid_until": o["valid_until"]}
                    for o in overrides
                ],
                "event_count": len(raw_events),
                "rejected_event_count": rejected_count,
            },
            "decision": decision.to_dict(),
        }

    def history(self, patient_ref: str) -> dict:
        state = self._fold(patient_ref)
        return {
            "patient_ref": patient_ref,
            "prescription_versions": [
                {k: v for k, v in rx.items() if k != "_ts"} for rx in state["prescriptions"]
            ],
            "overrides": [{k: v for k, v in o.items() if k != "_ts"} for o in state["overrides"]],
            "ledger_entries": len(self.store.read(patient_ref)),
        }

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def _next_rx_version(self, patient_ref: str) -> str:
        return f"rx-{len(self._fold(patient_ref)['prescriptions']) + 1}"

    @staticmethod
    def _valid_overridden_codes(s: Screening, overrides: list[dict]) -> frozenset[str]:
        hit_relative = {f.rule_code for f in s.relative}
        codes: set[str] = set()
        for o in overrides:
            if o.get("rule_pack_id") and o["rule_pack_id"] != s.pack_id:
                continue
            codes.update(c for c in o["rule_codes"] if c in hit_relative)
        return frozenset(codes)

    @staticmethod
    def _apply_changes(dose: dict, changes: dict) -> dict:
        new_dose = {k: (list(v) if isinstance(v, list) else v) for k, v in dose.items()}
        if "pace_kmh_range" in changes:
            new_dose["pace_kmh_range"] = [float(x) for x in changes["pace_kmh_range"]]
        if "minutes" in changes:
            new_dose["minutes"] = int(changes["minutes"])
        if "weekly_sessions" in changes:
            new_dose["weekly_sessions"] = int(changes["weekly_sessions"])
        return new_dose

    @staticmethod
    def _within_envelope(new_dose: dict, envelope: dict) -> Optional[str]:
        # 安全包络只强制"上界"：更慢、更短、更少永远不增加风险，允许医生保守下调。
        p_lo, p_hi = new_dose["pace_kmh_range"]
        if not 0 < p_lo <= p_hi:
            return "配速区间必须为正数且下界不大于上界"
        if p_hi > envelope["pace_kmh_range"][1] + 1e-9:
            e_hi = envelope["pace_kmh_range"][1]
            return f"配速上限 {p_hi:g} km/h 超出安全上限 {e_hi:g}"
        minutes = new_dose["minutes"]
        if not 1 <= minutes:
            return "时长必须至少 1 分钟（如需停止活动应使用暂停）"
        if minutes > envelope["minutes_range"][1] + 1e-9:
            return f"时长 {minutes:g} 分钟超出安全上限 {envelope['minutes_range'][1]}"
        sessions = new_dose["weekly_sessions"]
        if not 1 <= sessions:
            return "每周频次必须至少 1 次（如需停止活动应使用暂停）"
        if sessions > envelope["weekly_sessions_range"][1] + 1e-9:
            return f"周频次 {sessions:g} 超出安全上限 {envelope['weekly_sessions_range'][1]}"
        return None

    @staticmethod
    def _is_increase(old_dose: dict, new_dose: dict) -> bool:
        return (
            new_dose["minutes"] > old_dose["minutes"]
            or new_dose["weekly_sessions"] > old_dose["weekly_sessions"]
            or new_dose["pace_kmh_range"][1] > old_dose["pace_kmh_range"][1] + 1e-9
        )

    @staticmethod
    def _progression_order_ok(old_dose: dict, new_dose: dict, changes: dict) -> Optional[str]:
        """递增顺序：先时长、再频次、最后提速；同一次调整只能加一个维度。"""
        min_up = new_dose["minutes"] > old_dose["minutes"]
        sess_up = new_dose["weekly_sessions"] > old_dose["weekly_sessions"]
        pace_up = new_dose["pace_kmh_range"][1] > old_dose["pace_kmh_range"][1] + 1e-9
        if sum((min_up, sess_up, pace_up)) > 1:
            return "一次调整只能增加一个维度（先时长，再频次，最后提速）"
        if sess_up and old_dose["minutes"] < old_dose["minutes_range"][1]:
            return f"时长尚未达到安全上限 {old_dose['minutes_range'][1]} 分钟前，不得增加频次"
        if pace_up and (
            old_dose["minutes"] < old_dose["minutes_range"][1]
            or old_dose["weekly_sessions"] < old_dose["weekly_sessions_range"][1]
        ):
            return "时长与频次均达到安全上限前，不得提高配速"
        if pace_up and "pace_kmh_range" not in changes:
            return "配速上调必须显式给出 pace_kmh_range"
        return None

    @staticmethod
    def _progression_rate_ok(old_dose: dict, new_dose: dict, envelope: dict) -> Optional[str]:
        pct = new_dose.get("progression_pct_week", envelope["progression_pct_week"]) / 100.0
        old_min = old_dose["minutes"]
        new_min = new_dose["minutes"]
        if new_min > old_min:
            cap = old_min * (1 + pct)
            if new_min > cap + 1e-9:
                return (f"时长由 {old_min} 分钟增至 {new_min:g}，超过每周 "
                        f"{pct * 100:g}% 增幅上限（本次最多 {cap:.1f} 分钟）")
        if new_dose["weekly_sessions"] - old_dose["weekly_sessions"] > 1:
            return "每周频次每次至多增加 1 次"
        return None

    def _recent_unsafe_day(self, patient_ref: str, today: date) -> Optional[str]:
        from datetime import timedelta
        state = self._fold(patient_ref)
        days: set[str] = set()
        for item in state["events"]:
            raw = item.get("raw")
            parsed = parse_ts(raw.get("ts")) if isinstance(raw, dict) else None
            if parsed:
                days.add(parsed.date().isoformat())
            elif item.get("entry_ts"):
                days.add(item["entry_ts"][:10])
        for offset in range(1, 8):
            d = (today - timedelta(days=offset)).isoformat()
            if d in days:
                decision = self.replay_day(patient_ref, d)["decision"]["decision"]
                if decision in NO_INCREASE_AFTER:
                    return d
        return None


def _plan_view(dose: dict) -> dict:
    return {
        "pace_kmh_range": dose["pace_kmh_range"],
        "hr_zone": dose["hr_zone"],
        "minutes": dose.get("minutes", dose["minutes_range"][0]),
        "minutes_range": dose["minutes_range"],
        "weekly_sessions": dose.get("weekly_sessions", dose["weekly_sessions_range"][0]),
        "weekly_sessions_range": dose["weekly_sessions_range"],
        "warmup_min": dose["warmup_min"],
        "cooldown_min": dose["cooldown_min"],
        "progression_pct_week": dose["progression_pct_week"],
        "progression_rule": dose["progression_rule"],
        "monitoring": dose["monitoring"],
        "cautions": dose["cautions"],
    }


def _default_pack_id() -> str:
    from .rules import DEFAULT_PACK_ID
    return DEFAULT_PACK_ID
